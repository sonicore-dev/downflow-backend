# URL validation and SSRF protection.
#
# Layers:
# 1. Syntax: absolute http/https only, no embedded credentials, allowed ports.
# 2. DNS: every address the host resolves to must be globally routable
#    (no loopback, private, link-local, CGNAT, multicast, reserved ranges).
# 3. Redirects: the submitted URL's redirect chain is walked manually and each
#    hop is re-validated before yt-dlp is allowed to fetch it.
# 4. Output: media URLs returned by extractors are checked the same way.
#
# Known limitation: yt-dlp performs its own DNS lookups, so a hostile DNS server
# could still answer differently between our check and its fetch (DNS rebinding).
# For high-assurance deployments, also enforce egress filtering at the network
# level (firewall / proxy) -- see README.

from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from .error_handler import ErrorCode, ExtractionError, sanitize_url
from .extractor_config import ExtractorConfig

__all__ = ["validate_url", "resolve_redirects", "is_public_media_url"]

log = logging.getLogger("downflow.security")

_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "metadata.google.internal"}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet")


def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _ip_is_public(ip.ipv4_mapped)
        if ip.sixtofour is not None and not _ip_is_public(ip.sixtofour):
            return False
        if ip.teredo is not None:
            return False
    return ip.is_global and not ip.is_multicast


def _resolve(host: str, port: int) -> list[ipaddress._BaseAddress]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addrs = []
    for info in infos:
        raw = info[4][0].split("%", 1)[0]
        addrs.append(ipaddress.ip_address(raw))
    return addrs


def _assert_public_host(host: str, port: int, config: ExtractorConfig) -> None:
    lowered = host.lower().rstrip(".")
    if not config.allow_private_hosts:
        if lowered in _BLOCKED_HOSTNAMES or lowered.endswith(_BLOCKED_SUFFIXES):
            raise ExtractionError(ErrorCode.URL_NOT_ALLOWED, detail=f"blocked hostname {lowered}")
    try:
        addrs = _resolve(host, port)
    except (socket.gaierror, UnicodeError, ValueError) as exc:
        raise ExtractionError(ErrorCode.INVALID_URL, "The host name could not be resolved.",
                              detail=f"dns failure for {lowered}: {exc.__class__.__name__}") from exc
    if not addrs:
        raise ExtractionError(ErrorCode.INVALID_URL, "The host name could not be resolved.")
    if config.allow_private_hosts:
        return
    for ip in addrs:
        if not _ip_is_public(ip):
            raise ExtractionError(ErrorCode.URL_NOT_ALLOWED, detail=f"{lowered} resolves to non-public address")


def validate_url(raw: str, config: ExtractorConfig, *, resolve: bool = True) -> str:
    # Validate and normalise a user-supplied URL; return the normalised form.
    if not isinstance(raw, str):
        raise ExtractionError(ErrorCode.INVALID_URL)
    url = raw.strip()
    if not url or len(url) > config.max_url_length:
        raise ExtractionError(ErrorCode.INVALID_URL, detail="empty or too long")
    if any(ord(c) < 32 or ord(c) == 127 or c.isspace() for c in url):
        raise ExtractionError(ErrorCode.INVALID_URL, detail="whitespace/control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise ExtractionError(ErrorCode.INVALID_URL, detail=str(exc)) from exc

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ExtractionError(ErrorCode.INVALID_URL, "Only http and https URLs are supported.",
                              detail=f"scheme {scheme!r}")
    if not host:
        raise ExtractionError(ErrorCode.INVALID_URL, detail="missing host")
    if parts.username is not None or parts.password is not None:
        raise ExtractionError(ErrorCode.INVALID_URL, "URLs containing credentials are not accepted.")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ExtractionError(ErrorCode.INVALID_URL, detail="bad IDN host") from exc

    effective_port = port or (443 if scheme == "https" else 80)
    if not config.allow_private_hosts and effective_port not in config.allowed_ports:
        raise ExtractionError(ErrorCode.URL_NOT_ALLOWED, "This port is not allowed.", detail=f"port {effective_port}")

    netloc = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    if port and port != (443 if scheme == "https" else 80):
        netloc = f"{netloc}:{port}"
    normalized = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))

    if resolve:
        _assert_public_host(ascii_host, effective_port, config)
    return normalized


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102
        return None


def _build_probe_opener(config: ExtractorConfig) -> Optional[urllib.request.OpenerDirector]:
    handlers: list = [_NoRedirect()]
    if config.proxy:
        scheme = urlsplit(config.proxy).scheme.lower()
        if not scheme.startswith("http"):
            return None  # socks proxies cannot be probed with urllib
        handlers.append(urllib.request.ProxyHandler({"http": config.proxy, "https": config.proxy}))
    return urllib.request.build_opener(*handlers)


def resolve_redirects(url: str, config: ExtractorConfig, user_agent: Optional[str] = None) -> str:
    # Follow redirects manually, validating every hop. Returns the final URL.
    #
    # Network errors during the probe are not fatal (yt-dlp will report them with
    # better context); only a hop that points somewhere forbidden raises.
    if not config.check_redirects or config.max_redirects <= 0:
        return url
    opener = _build_probe_opener(config)
    if opener is None:
        return url
    headers = {"User-Agent": user_agent or "Mozilla/5.0 (compatible; DownflowProbe/1.0)", "Accept": "*/*"}
    current = url
    for _ in range(config.max_redirects + 1):
        location: Optional[str] = None
        for method in ("HEAD", "GET"):
            req = urllib.request.Request(current, method=method, headers=headers)
            try:
                resp = opener.open(req, timeout=config.redirect_check_timeout)
                resp.close()
                return current  # 2xx: chain ended
            except urllib.error.HTTPError as err:
                try:
                    if 300 <= err.code < 400 and err.headers.get("Location"):
                        location = err.headers["Location"]
                        break
                    if err.code in (403, 405, 501) and method == "HEAD":
                        continue
                    return current
                finally:
                    err.close()
            except Exception as exc:  # noqa: BLE001  (probe is best-effort)
                log.debug("redirect probe stopped at %s: %s", sanitize_url(current), exc.__class__.__name__)
                return current
        if location is None:
            return current
        nxt = urljoin(current, location)
        current = validate_url(nxt, config)  # raises on forbidden hop
    raise ExtractionError(ErrorCode.URL_NOT_ALLOWED, "The URL redirects too many times.", detail="redirect limit")


def is_public_media_url(url: str, config: ExtractorConfig, cache: dict[str, bool]) -> bool:
    # True if ``url`` is http(s) and its host resolves only to public addresses.
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return False
        host = parts.hostname.encode("idna").decode("ascii")
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except (ValueError, UnicodeError):
        return False
    key = f"{host}:{port}"
    if key not in cache:
        try:
            _assert_public_host(host, port, config)
            cache[key] = True
        except ExtractionError:
            cache[key] = False
    return cache[key]
