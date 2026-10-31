# Error classification and sanitisation for the extraction engine.
#
# Raw ``yt-dlp`` exceptions can contain URLs with signed query strings, cookie
# fragments and local paths. Everything returned to API clients goes through
# this module; the raw detail is only ever written to server logs (after
# sanitising).

from __future__ import annotations

import enum
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "ErrorCode",
    "ExtractionError",
    "classify_exception",
    "sanitize_text",
    "sanitize_url",
]


class ErrorCode(str, enum.Enum):
    INVALID_URL = "invalid_url"
    URL_NOT_ALLOWED = "url_not_allowed"
    UNSUPPORTED_URL = "unsupported_url"
    RATE_LIMITED = "rate_limited"
    AUTH_REQUIRED = "auth_required"
    IP_BLOCKED = "ip_blocked"
    COOKIES_INVALID = "cookies_invalid"
    PRIVATE_VIDEO = "private_video"
    UNAVAILABLE = "unavailable"
    GEO_RESTRICTED = "geo_restricted"
    AGE_RESTRICTED = "age_restricted"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    NO_FORMATS = "no_formats"
    NO_MEDIA_URL = "no_media_url"
    FORMAT_NOT_AVAILABLE = "format_not_available"
    EXTRACTOR_OUTDATED = "extractor_outdated"
    CONFIG_ERROR = "config_error"
    BUSY = "busy"
    CANCELLED = "cancelled"
    TASK_NOT_FOUND = "task_not_found"
    INTERNAL_ERROR = "internal_error"


# code -> (http status, retryable by engine, safe message for clients)
_META: dict[ErrorCode, tuple[int, bool, str]] = {
    ErrorCode.INVALID_URL: (400, False, "The submitted URL is not valid. Only absolute http(s) URLs are accepted."),
    ErrorCode.URL_NOT_ALLOWED: (400, False, "The submitted URL points to a destination that is not allowed."),
    ErrorCode.UNSUPPORTED_URL: (422, False, "This URL or platform is not supported."),
    ErrorCode.RATE_LIMITED: (429, True, "The source platform is rate limiting requests. Try again later."),
    ErrorCode.IP_BLOCKED: (503, True, "The source platform is blocking or rate limiting this server's IP address. Wait a while, switch network, or configure a proxy."),
    ErrorCode.AUTH_REQUIRED: (403, False, "This content requires authentication, which is not available to the server."),
    ErrorCode.COOKIES_INVALID: (403, False, "The configured cookies were rejected or have expired. The operator must refresh them."),
    ErrorCode.PRIVATE_VIDEO: (403, False, "This video is private."),
    ErrorCode.UNAVAILABLE: (404, False, "This video is unavailable, removed, or does not exist."),
    ErrorCode.GEO_RESTRICTED: (403, False, "This content is not available from the server's region."),
    ErrorCode.AGE_RESTRICTED: (403, False, "This content is age-restricted and cannot be accessed without an authorised account."),
    ErrorCode.TIMEOUT: (504, True, "The request to the source platform timed out."),
    ErrorCode.NETWORK_ERROR: (502, True, "A network error occurred while contacting the source platform."),
    ErrorCode.NO_FORMATS: (422, False, "No downloadable formats were found for this video."),
    ErrorCode.NO_MEDIA_URL: (422, False, "Extraction finished but the platform did not expose a usable direct media URL."),
    ErrorCode.FORMAT_NOT_AVAILABLE: (422, False, "The requested format is not available for this video."),
    ErrorCode.EXTRACTOR_OUTDATED: (502, False, "Extraction failed. The platform may have changed; the server's yt-dlp may need updating."),
    ErrorCode.CONFIG_ERROR: (500, False, "The server extraction configuration is invalid."),
    ErrorCode.BUSY: (503, True, "The server is busy handling other extractions. Try again shortly."),
    ErrorCode.CANCELLED: (503, True, "The extraction was cancelled because the server is shutting down."),
    ErrorCode.TASK_NOT_FOUND: (404, False, "Task not found. It may have expired."),
    ErrorCode.INTERNAL_ERROR: (500, False, "An unexpected internal error occurred."),
}


@dataclass
class ExtractionError(Exception):
    # Sanitised, classified extraction failure.

    code: ErrorCode
    message: str = ""
    detail: str = ""  # sanitised diagnostic text, for logs only
    retry_after: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.message:
            self.message = _META[self.code][2]
        Exception.__init__(self, self.message)

    @property
    def http_status(self) -> int:
        return _META[self.code][0]

    @property
    def retryable(self) -> bool:
        return _META[self.code][1]

    def to_dict(self) -> dict:
        data = {"code": self.code.value, "message": self.message, "retryable": self.retryable}
        if self.retry_after:
            data["retry_after"] = self.retry_after
        return data


# Sanitising helpers
_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"(?i)\b(cookie|set-cookie|authorization|proxy-authorization|x-[a-z-]*token|password|passwd|"
    r"api[_-]?key|access[_-]?token|sessionid|sid)\b\s*[:=]\s*[^\s,;]+"
)
_PATH_RE = re.compile(r"(?<![\w:/])(?:/[\w.\-@+~ ]+){2,}|[A-Za-z]:\\(?:[^\\\s]+\\?)+")


def sanitize_url(url: str) -> str:
    # Strip credentials, query string and fragment so a URL is safe to log.
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        path = parts.path
        if len(path) > 80:
            path = path[:80] + "..."
        return urlunsplit((parts.scheme, host, path, "", ""))
    except Exception:  # noqa: BLE001
        return "<unparseable-url>"


def sanitize_text(text: str, limit: int = 500) -> str:
    # Remove URLs' credentials/queries, secrets and filesystem paths from text.
    if not text:
        return ""
    text = _URL_RE.sub(lambda m: sanitize_url(m.group(0)), str(text))
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    text = _PATH_RE.sub("<path>", text)
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)  # ANSI colour codes
    text = " ".join(text.split())
    return text[:limit]


# Classification
_PATTERNS: list[tuple[ErrorCode, re.Pattern[str]]] = [
    (ErrorCode.RATE_LIMITED, re.compile(r"http error 429|too many requests|rate[- ]?limit|try again later|temporarily blocked", re.I)),
    (ErrorCode.COOKIES_INVALID, re.compile(r"cookies? (?:are |is )?(?:no longer valid|invalid|expired|rejected)|cookie file|netscape format", re.I)),
    (ErrorCode.AGE_RESTRICTED, re.compile(r"confirm your age|age[- ]restrict|age[- ]gate|inappropriate for some users", re.I)),
    (ErrorCode.PRIVATE_VIDEO, re.compile(r"private video|this video is private|is a private", re.I)),
    (ErrorCode.GEO_RESTRICTED, re.compile(r"geo[- ]?restrict|(?:not made this video )?available in your (?:country|region)|not available in your (?:country|region)|blocked in your country|from your location", re.I)),
    (ErrorCode.AUTH_REQUIRED, re.compile(r"sign in|log in|login|authentication|members[- ]only|premium|subscribers? only|http error 40[13]|confirm you.re not a bot|account", re.I)),
    (ErrorCode.FORMAT_NOT_AVAILABLE, re.compile(r"requested format is not available|requested format not available", re.I)),
    (ErrorCode.NO_FORMATS, re.compile(r"no video formats found|no formats", re.I)),
    (ErrorCode.UNSUPPORTED_URL, re.compile(r"unsupported url|no suitable extractor", re.I)),
    (ErrorCode.UNAVAILABLE, re.compile(r"video unavailable|has been removed|was deleted|no longer available|does not exist|not available|http error 404|http error 410|terminated|copyright", re.I)),
    (ErrorCode.TIMEOUT, re.compile(r"timed out|timeout", re.I)),
    (ErrorCode.NETWORK_ERROR, re.compile(r"connection (?:reset|refused|aborted|error)|temporary failure in name resolution|name or service not known|network is unreachable|unable to download (?:webpage|json|video data)|http error 5\d\d|remote end closed|incomplete read|ssl|eof occurred", re.I)),
    (ErrorCode.EXTRACTOR_OUTDATED, re.compile(r"unable to extract|please report this issue|update yt-dlp|signature|nsig|player response|unexpected response", re.I)),
]


def _unwrap(exc: BaseException) -> BaseException:
    # Return the innermost interesting exception from a yt-dlp DownloadError.
    seen = 0
    while seen < 5:
        seen += 1
        info = getattr(exc, "exc_info", None)
        if info and len(info) > 1 and isinstance(info[1], BaseException) and info[1] is not exc:
            exc = info[1]
            continue
        if exc.__cause__ is not None and not getattr(exc, "msg", None):
            exc = exc.__cause__
            continue
        break
    return exc


def classify_exception(exc: BaseException) -> ExtractionError:
    # Convert any exception raised during extraction into an ExtractionError.
    if isinstance(exc, ExtractionError):
        return exc

    inner = _unwrap(exc)
    raw = " ".join(filter(None, {str(exc), str(inner)}))
    detail = sanitize_text(raw)
    cls_names = {c.__name__ for c in type(inner).__mro__}

    # Typed exceptions first (names compared so this module never imports yt-dlp).
    if "GeoRestrictedError" in cls_names:
        return ExtractionError(ErrorCode.GEO_RESTRICTED, detail=detail)
    if "UnsupportedError" in cls_names:
        return ExtractionError(ErrorCode.UNSUPPORTED_URL, detail=detail)
    if isinstance(inner, (socket.timeout, TimeoutError)):
        return ExtractionError(ErrorCode.TIMEOUT, detail=detail)
    if isinstance(inner, ssl.SSLError):
        return ExtractionError(ErrorCode.NETWORK_ERROR, detail=detail)
    if "HTTPError" in cls_names:
        status = getattr(inner, "status", None) or getattr(getattr(inner, "response", None), "status", None)
        if status == 429:
            retry_after = None
            headers = getattr(getattr(inner, "response", None), "headers", None)
            try:
                retry_after = int(headers.get("Retry-After")) if headers else None
            except (TypeError, ValueError, AttributeError):
                retry_after = None
            return ExtractionError(ErrorCode.RATE_LIMITED, detail=detail, retry_after=retry_after)

    for code, pattern in _PATTERNS:
        if pattern.search(raw):
            return ExtractionError(code, detail=detail)

    if "ExtractorError" in cls_names or "DownloadError" in {c.__name__ for c in type(exc).__mro__}:
        return ExtractionError(ErrorCode.EXTRACTOR_OUTDATED, detail=detail)
    if isinstance(inner, (OSError, ConnectionError)):
        return ExtractionError(ErrorCode.NETWORK_ERROR, detail=detail)
    return ExtractionError(ErrorCode.INTERNAL_ERROR, detail=detail)
