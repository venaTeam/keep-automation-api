"""Artifactory storage cleanup for D18.

Folder info lists immediate children, including tag/digest directories and
partial build artifacts. Delete Item removes each child recursively. This
avoids Docker tag pagination and removes untagged leftovers too, with one
request per child rather than per layer. No global blob deletion is performed.

API references:
https://docs.jfrog.com/artifactory/reference/getstorageitem
https://docs.jfrog.com/artifactory/reference/deleteitem
"""
import base64
import json
import math
import re
from collections.abc import Callable
from http.client import HTTPException, HTTPResponse
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID


class RegistryError(RuntimeError):
    """Sanitized registry failure, safe for the cascade's error handling."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credentials or destructive operations to another URL.
        return None


def _valid_segment(value: str) -> bool:
    # Reject encoded separators/dot segments as well as literal traversal.
    return (
        isinstance(value, str)
        and value not in (".", "..")
        and re.fullmatch(r"[A-Za-z0-9_.:+-]+", value) is not None
    )


class ArtifactoryRegistryClient:
    """Synchronous adapter; base_url includes the /artifactory context path.

    A0/F23 must configure the actual image prefix used for pushes. All images
    and build artifacts for an automation must live below prefix/UUID; the
    golden base must live outside that path. Missing/unsafe settings fail
    before network I/O. Credentials are supplied by deployment secret injection.
    """

    _MAX_LISTING_BYTES = 4 * 1024 * 1024

    def __init__(
        self,
        base_url: str,
        repository: str,
        image_prefix: str,
        token: str = "",
        username: str = "",
        password: str = "",
        timeout_seconds: float = 10.0,
        opener: Callable[..., HTTPResponse] | None = None,
    ) -> None:
        try:
            url = urlsplit(base_url)
            valid_url = (
                url.scheme == "https"
                and bool(url.hostname)
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
                and not any(c.isspace() for c in base_url)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("ARTIFACTORY_URL must be an HTTPS URL without credentials")
        if not _valid_segment(repository) or not all(
            _valid_segment(part) for part in image_prefix.split("/")
        ):
            raise ValueError("Artifactory repository and image prefix must be safe paths")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Artifactory timeout must be finite and positive")
        if token and not username and not password:
            authorization = f"Bearer {token}"
        elif username and password and not token:
            raw = f"{username}:{password}".encode("utf-8")
            authorization = "Basic " + base64.b64encode(raw).decode("ascii")
        else:
            raise ValueError("Configure one Artifactory token or username/password pair")
        if "\r" in authorization or "\n" in authorization:
            raise ValueError("Invalid Artifactory credentials")
        self._base_url = base_url.rstrip("/")
        self._repository = repository
        self._prefix = image_prefix
        self._authorization = authorization
        self._timeout = timeout_seconds
        self._opener = opener

    def _request(self, method: str, path: str) -> bytes | None:
        request = Request(
            f"{self._base_url}/{path}",
            method=method,
            headers={"Accept": "application/json", "Authorization": self._authorization},
        )
        # A request-local opener has no shared mutable transport state when
        # concurrent cascade runners use the process-wide adapter.
        open_request = self._opener or build_opener(_NoRedirect()).open
        try:
            with open_request(request, timeout=self._timeout) as response:
                expected_status = 200 if method == "GET" else 204
                if response.status != expected_status:
                    raise RegistryError("Unexpected Artifactory response status")
                if method == "DELETE":
                    return b""
                body = response.read(self._MAX_LISTING_BYTES + 1)
                if len(body) > self._MAX_LISTING_BYTES:
                    raise RegistryError("Artifactory listing exceeds the size limit")
                return body
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 404:
                return None
            raise RegistryError(f"Artifactory request failed with HTTP {status}") from None
        except (OSError, URLError, HTTPException, ValueError):
            # Suppress transport exception text/traceback chains: they can
            # contain URLs, headers, and credentials. Includes response reads.
            raise RegistryError("Artifactory request failed") from None

    def _image_path(self, automation_id: UUID) -> str:
        return f"{self._prefix}/{UUID(str(automation_id))}"

    def list_image_entries(self, automation_id: UUID) -> list[str]:
        root = self._image_path(automation_id)
        path = quote(f"{self._repository}/{root}", safe="/")
        body = self._request("GET", f"api/storage/{path}")
        if body is None:
            return []
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise RegistryError("Artifactory returned an invalid listing") from None
        if (
            not isinstance(payload, dict)
            or payload.get("repo") != self._repository
            or payload.get("path") != f"/{root}"
            or not isinstance(payload.get("children"), list)
        ):
            raise RegistryError("Artifactory returned an invalid listing")
        entries: set[str] = set()
        for child in payload["children"]:
            uri = child.get("uri") if isinstance(child, dict) else None
            if (
                not isinstance(uri, str)
                or not uri.startswith("/")
                or not _valid_segment(uri[1:])
            ):
                raise RegistryError("Artifactory returned an unsafe child path")
            entries.add(uri[1:])
        return sorted(entries)

    def delete_image(self, automation_id: UUID, entry: str) -> None:
        if not _valid_segment(entry):
            raise RegistryError("Registry entry must be an immediate child of the image")
        path = f"{self._repository}/{self._image_path(automation_id)}/{entry}"
        self._request("DELETE", quote(path, safe="/"))
