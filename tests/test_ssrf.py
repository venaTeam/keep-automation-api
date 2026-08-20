"""SSRF guard tests — including the DNS-rebinding vector (public name → private IP)."""
import socket
from concurrent.futures import TimeoutError as FutureTimeoutError

import pytest

from src.bl import ssrf
from src.models.api.validation_errors import ErrorCode


def code_of(errors):
    assert len(errors) == 1
    return errors[0].code


def fake_getaddrinfo(address: str):
    def _fake(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    return _fake


def test_none_is_valid():
    assert ssrf.validate_logstash_url(None) == []


def test_http_scheme_rejected():
    errors = ssrf.validate_logstash_url("http://logs.example.com/ingest")
    assert code_of(errors) == ErrorCode.LOGSTASH_SCHEME_NOT_HTTPS.value


def test_garbage_url_rejected():
    errors = ssrf.validate_logstash_url("not a url")
    assert code_of(errors) == ErrorCode.LOGSTASH_SCHEME_NOT_HTTPS.value


def test_public_address_accepted(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))
    assert ssrf.validate_logstash_url("https://logs.example.com/ingest") == []


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",          # RFC1918
        "192.168.1.1",       # RFC1918
        "172.16.0.1",        # RFC1918
        "127.0.0.1",         # loopback
        "169.254.169.254",   # link-local / cloud metadata
        "100.64.0.1",        # CGNAT
        "0.0.0.0",           # unspecified
        "::1",               # v6 loopback
        "fd00::1",           # v6 ULA
    ],
)
def test_dns_rebinding_public_name_to_private_ip_rejected(monkeypatch, address):
    # The actual SSRF vector: hostname LOOKS public, resolves private.
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo(address))
    errors = ssrf.validate_logstash_url("https://logs.example.com/ingest")
    assert code_of(errors) == ErrorCode.LOGSTASH_PRIVATE_ADDRESS.value


def test_any_private_record_among_public_rejected(monkeypatch):
    def mixed(host, port, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", mixed)
    errors = ssrf.validate_logstash_url("https://logs.example.com/ingest")
    assert code_of(errors) == ErrorCode.LOGSTASH_PRIVATE_ADDRESS.value


def test_literal_private_ip_rejected():
    errors = ssrf.validate_logstash_url("https://192.168.1.10/ingest")
    assert code_of(errors) == ErrorCode.LOGSTASH_PRIVATE_ADDRESS.value


def test_nxdomain_soft_rejected(monkeypatch):
    def raise_gaierror(host, port, **kwargs):
        raise socket.gaierror("NXDOMAIN")

    monkeypatch.setattr(socket, "getaddrinfo", raise_gaierror)
    errors = ssrf.validate_logstash_url("https://no-such-host.example.com/x")
    assert code_of(errors) == ErrorCode.LOGSTASH_UNRESOLVABLE.value


def test_resolver_timeout_soft_rejected(monkeypatch):
    def fake_resolve(hostname):
        raise FutureTimeoutError()

    monkeypatch.setattr(ssrf, "_resolve_bounded", fake_resolve)
    errors = ssrf.validate_logstash_url("https://slow-dns.example.com/x")
    assert code_of(errors) == ErrorCode.LOGSTASH_UNRESOLVABLE.value
