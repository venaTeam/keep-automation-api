"""Logstash URL SSRF guard (spec §4.1): https-only + resolve-and-reject.

Courtesy check — the real boundary is the wallet's egress policy in CAPP
(TOCTOU is accepted: the host can re-point after validation). We still reject
anything that resolves to a non-global address so the control plane never
stores an obviously-internal target. The URL is NEVER fetched here.

Blocking-I/O pattern (pinned for this service — D14's git I/O must reuse it):
the BL is synchronous and routes run it via `run_in_threadpool`, so a stalled
syscall never blocks the event loop. `getaddrinfo` itself honors no timeout,
so it is additionally bounded by a single-use executor + `future.result(3s)`.
"""
import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from urllib.parse import urlparse

from src.contracts.validation_errors import ErrorCode, FieldError

RESOLVE_TIMEOUT_SECONDS = 3.0


def _resolve_bounded(hostname: str) -> list[str]:
    """All A/AAAA addresses for hostname, or raise (gaierror / FutureTimeoutError)."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(socket.getaddrinfo, hostname, 443, proto=socket.IPPROTO_TCP)
        infos = future.result(timeout=RESOLVE_TIMEOUT_SECONDS)
    return [info[4][0] for info in infos]


def validate_logstash_url(logstash_url: str | None) -> list[FieldError]:
    if logstash_url is None:
        return []

    parsed = urlparse(logstash_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return [
            FieldError(
                field="logstash_url",
                code=ErrorCode.LOGSTASH_SCHEME_NOT_HTTPS,
                message="logstash_url must be a valid https:// URL.",
            )
        ]

    try:
        addresses = _resolve_bounded(parsed.hostname)
    except FutureTimeoutError:
        return [
            FieldError(
                field="logstash_url",
                code=ErrorCode.LOGSTASH_UNRESOLVABLE,
                message=f"Hostname resolution exceeded {RESOLVE_TIMEOUT_SECONDS:.0f}s.",
            )
        ]
    except (socket.gaierror, UnicodeError):
        return [
            FieldError(
                field="logstash_url",
                code=ErrorCode.LOGSTASH_UNRESOLVABLE,
                message="Hostname does not resolve.",
            )
        ]

    # Reject if ANY record is non-global: private, loopback, link-local
    # (incl. 169.254.169.254 cloud metadata), CGNAT, reserved, unspecified.
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if not ip.is_global:
            return [
                FieldError(
                    field="logstash_url",
                    code=ErrorCode.LOGSTASH_PRIVATE_ADDRESS,
                    message="logstash_url resolves to a private or control-plane address.",
                )
            ]
    return []
