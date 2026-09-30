"""Artifactory's wire contract, deletion scope, and failure behavior."""
import base64
import io
import json
import traceback
from urllib.error import HTTPError, URLError
from urllib.request import Request
from uuid import UUID

import pytest

from src.bl import cascade_adapters
from src.bl.registry_client import ArtifactoryRegistryClient, RegistryError, _NoRedirect

AUTOMATION_ID = UUID("11111111-1111-1111-1111-111111111111")
ROOT = f"automations/{AUTOMATION_ID}"
BASE = "https://registry.example/artifactory"


def listing(*names):
    return {
        "repo": "docker-local",
        "path": f"/{ROOT}",
        "children": [{"uri": f"/{name}", "folder": True} for name in names],
    }


class Response(io.BytesIO):
    def __init__(self, payload=b"", status=200):
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.status = status


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(opener, **overrides):
    options = dict(
        base_url=BASE,
        repository="docker-local",
        image_prefix="automations",
        token="secret-token",
        timeout_seconds=3,
        opener=opener,
    )
    return ArtifactoryRegistryClient(**{**options, **overrides})


def test_storage_listing_and_delete_use_distinct_endpoints_and_close_responses():
    inventory = Response(listing("sha256__old", "build-partial", "sha256__old"))
    deleted = Response(status=204)
    transport = Transport(inventory, deleted)
    registry = client(transport)
    entries = registry.list_image_entries(AUTOMATION_ID)
    registry.delete_image(AUTOMATION_ID, entries[0])

    assert entries == ["build-partial", "sha256__old"]
    get, delete = [r for r, _ in transport.requests]
    assert (get.method, get.full_url) == ("GET", f"{BASE}/api/storage/docker-local/{ROOT}")
    assert (delete.method, delete.full_url) == ("DELETE", f"{BASE}/docker-local/{ROOT}/build-partial")
    assert all(timeout == 3 for _, timeout in transport.requests)
    assert get.get_header("Authorization") == "Bearer secret-token"
    assert inventory.closed and deleted.closed


def test_partial_deletion_retry_converges_and_never_addresses_siblings():
    entries = {"old-tag", "partial-upload", "sha256__untagged"}
    calls = []
    failed = False

    def artifactory(request, timeout):
        nonlocal failed
        calls.append(request.full_url)
        if request.method == "GET":
            assert request.full_url == f"{BASE}/api/storage/docker-local/{ROOT}"
            return Response(listing(*entries))
        assert request.full_url.startswith(f"{BASE}/docker-local/{ROOT}/")
        name = request.full_url.rsplit("/", 1)[1]
        if name == "partial-upload" and not failed:
            failed = True
            raise TimeoutError("transport token=secret-token")
        entries.discard(name)
        return Response(status=204)

    registry = client(artifactory)
    with pytest.raises(RegistryError):
        for entry in registry.list_image_entries(AUTOMATION_ID):
            registry.delete_image(AUTOMATION_ID, entry)
    assert entries == {"partial-upload", "sha256__untagged"}
    for entry in registry.list_image_entries(AUTOMATION_ID):
        registry.delete_image(AUTOMATION_ID, entry)
    assert registry.list_image_entries(AUTOMATION_ID) == []
    assert len(calls) == 7


@pytest.mark.parametrize("method", ["list", "delete"])
def test_404_is_idempotent_success_and_closes_error_response(method):
    body = io.BytesIO(b"absent")
    error = HTTPError("https://redacted", 404, "absent", {}, body)
    registry = client(Transport(error))
    if method == "list":
        assert registry.list_image_entries(AUTOMATION_ID) == []
    else:
        assert registry.delete_image(AUTOMATION_ID, "old-tag") is None
    assert body.closed


@pytest.mark.parametrize("status", [301, 302, 401, 403, 429, 500, 503])
@pytest.mark.parametrize("method", ["list", "delete"])
def test_http_failures_are_not_treated_as_absence(status, method):
    body = io.BytesIO(b"secret-token")
    registry = client(Transport(HTTPError("secret-token", status, "secret-token", {}, body)))
    with pytest.raises(RegistryError) as exc:
        if method == "list":
            registry.list_image_entries(AUTOMATION_ID)
        else:
            registry.delete_image(AUTOMATION_ID, "old-tag")
    assert "secret-token" not in "".join(traceback.format_exception(exc.value))
    assert body.closed


@pytest.mark.parametrize("error", [TimeoutError("secret-token"), URLError("secret-token")])
def test_transport_errors_are_sanitized(error):
    with pytest.raises(RegistryError) as exc:
        client(Transport(error)).list_image_entries(AUTOMATION_ID)
    assert "secret-token" not in "".join(traceback.format_exception(exc.value))


def test_body_read_failure_closes_response_and_redacts_exception():
    class BrokenBody(Response):
        def read(self, size):
            raise TimeoutError("secret-token")

    response = BrokenBody()
    with pytest.raises(RegistryError) as exc:
        client(Transport(response)).list_image_entries(AUTOMATION_ID)
    assert response.closed
    assert "secret-token" not in "".join(traceback.format_exception(exc.value))


@pytest.mark.parametrize("payload", [
    b"not-json", [], {}, {"files": []},
    {**listing(), "children": None},
    {**listing(), "repo": "other-repository"},
    {**listing(), "path": "/automations/other"},
    {**listing(), "children": [{}]},
    {**listing(), "children": [None]},
])
def test_malformed_inventory_never_counts_as_empty(payload):
    with pytest.raises(RegistryError):
        client(Transport(Response(payload))).list_image_entries(AUTOMATION_ID)


@pytest.mark.parametrize("entry", [
    "", ".", "..", "../golden-base", "tag/../../other", "/other",
    "tag/child", "tag\\child", "%2e%2e", "tag%2fchild", "tag?x=1", "tag#fragment",
])
def test_unsafe_inventory_and_delete_paths_are_rejected(entry):
    transport = Transport(Response(listing(entry)))
    registry = client(transport)
    with pytest.raises(RegistryError):
        registry.list_image_entries(AUTOMATION_ID)
    with pytest.raises(RegistryError):
        registry.delete_image(AUTOMATION_ID, entry)
    assert len(transport.requests) == 1  # no destructive request was sent


def test_inventory_read_is_bounded_and_oversize_fails_closed():
    response = Response(b"x" * (ArtifactoryRegistryClient._MAX_LISTING_BYTES + 1))
    with pytest.raises(RegistryError, match="size limit"):
        client(Transport(response)).list_image_entries(AUTOMATION_ID)
    assert response.closed


@pytest.mark.parametrize("status", [200, 202, 301])
def test_delete_only_accepts_completed_deletion(status):
    with pytest.raises(RegistryError):
        client(Transport(Response(status=status))).delete_image(AUTOMATION_ID, "old-tag")


def test_basic_auth_uses_header_only():
    transport = Transport(Response(listing()))
    client(transport, token="", username="reader", password="password").list_image_entries(AUTOMATION_ID)
    request, _ = transport.requests[0]
    assert request.get_header("Authorization") == "Basic " + base64.b64encode(b"reader:password").decode()
    assert "password" not in request.full_url


def test_redirect_handler_never_forwards_request():
    request = Request(BASE, headers={"Authorization": "Bearer secret-token"})
    assert _NoRedirect().redirect_request(request, None, 302, "", {}, "https://other") is None


@pytest.mark.parametrize("overrides", [
    {"base_url": "http://registry.example"},
    {"base_url": "https://user:password@registry.example"},
    {"base_url": "https://registry.example?token=secret-token"},
    {"repository": "../other"}, {"repository": ""},
    {"image_prefix": ""}, {"image_prefix": "../other"},
    {"image_prefix": "automations/%2e%2e"},
    {"timeout_seconds": 0}, {"timeout_seconds": float("inf")},
    {"timeout_seconds": float("nan")},
    {"token": ""}, {"username": "user"}, {"token": "bad\r\nheader"},
])
def test_invalid_configuration_refuses_network_calls(overrides):
    transport = Transport()
    with pytest.raises(ValueError):
        client(transport, **overrides)
    assert transport.requests == []


def test_default_adapter_requires_explicit_configuration(monkeypatch):
    monkeypatch.setattr(cascade_adapters, "_default_registry", None)
    monkeypatch.setattr(cascade_adapters.config, "ARTIFACTORY_URL", "")
    registry = cascade_adapters.get_default_registry_client()
    with pytest.raises(cascade_adapters.ExternalDependencyNotConfiguredError):
        registry.list_image_entries(AUTOMATION_ID)


def test_default_adapter_uses_deployment_configuration(monkeypatch):
    monkeypatch.setattr(cascade_adapters, "_default_registry", None)
    for name, value in {
        "ARTIFACTORY_URL": BASE, "ARTIFACTORY_REPOSITORY": "docker-local",
        "ARTIFACTORY_IMAGE_PREFIX": "automations", "ARTIFACTORY_TOKEN": "secret-token",
        "ARTIFACTORY_USERNAME": "", "ARTIFACTORY_PASSWORD": "",
    }.items():
        monkeypatch.setattr(cascade_adapters.config, name, value)
    registry = cascade_adapters.get_default_registry_client()
    assert isinstance(registry, ArtifactoryRegistryClient)
    assert isinstance(registry, cascade_adapters.RegistryClient)
    assert cascade_adapters.get_default_registry_client() is registry
