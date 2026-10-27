"""Video metadata and media-URL extraction engine built on yt-dlp.

This module is deliberately independent of FastAPI: it takes a URL and options
and returns a plain JSON-serialisable ``dict`` or raises
:class:`advanced.ExtractionError`. Extraction is blocking; async callers should
run :func:`extract` in a worker thread.

Pipeline::

    validate/normalise URL  ->  SSRF + redirect checks  ->  build yt-dlp options
    -> extract_info(download=False) with bounded retries  ->  normalise result

No media is ever downloaded. Media URLs are returned only if the extractor
exposed them, they are http(s), and their hosts resolve to public addresses. A
webpage URL is never substituted for a missing media URL.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

from advanced import (
    ErrorCode,
    ExtractionError,
    ExtractorConfig,
    RetryPolicy,
    classify_exception,
    get_config,
    run_with_retries,
    sanitize_text,
    sanitize_url,
)
from advanced.cookie_manager import cookie_options, cookies_active
from advanced import proxy_manager, strategy_engine
from advanced.proxy_manager import redact
from advanced.header_manager import build_headers
from helpers import metrics
from advanced.url_security import is_public_media_url, resolve_redirects, validate_url

__all__ = ["FORMAT_ALIASES", "STAGES", "extract", "get_engine_info", "precheck_request"]

log = logging.getLogger("downflow.engine")

# Lifecycle stages reported through the ``progress`` callback, in execution order.
# (yt-dlp resolves metadata and formats in one call, so there is no separate
# "formats" stage during that call; format work is reported when results are
# resolved afterwards.)
STAGES = (
    "validating_url",
    "initializing_extractor",
    "extracting_metadata",
    "retrying",
    "resolving_formats",
)

ProgressCallback = Callable[[str, str], None]

# Friendly names accepted for the ``format`` option; anything else is treated as
# a raw yt-dlp format selector (validated for length and character set).
FORMAT_ALIASES: dict[str, str] = {
    "best": "bv*+ba/b",            # best quality; may be separate video + audio streams
    "best_single": "b",            # best single file containing both audio and video
    "worst": "wv*+wa/w",
    "audio": "ba/b",               # best audio-only stream
    "video": "bv*/b",              # best video (may have no audio)
}
_FORMAT_SELECTOR_RE = re.compile(r"^[A-Za-z0-9_\-+*/\[\]<>=!^$().,:~&|\s]+$")

MAX_DESCRIPTION_CHARS = 5000
MAX_TAGS = 50
MAX_THUMBNAILS = 20
MAX_FORMATS = 200
MAX_LANGUAGES = 300

_SENSITIVE_HEADER_RE = re.compile(r"cookie|authorization|auth|token|session|secret|password|api-?key", re.I)
_DEFAULT_HEADER_NAMES = {"user-agent", "accept", "accept-language", "accept-encoding", "sec-fetch-mode"}
_SIGNING_PARAM_RE = re.compile(
    r"^(sig|signature|token|hdnts|hdnea|policy|key-pair-id|x-amz-signature|x-goog-signature|"
    r"expires?|exp|expire|e|st|se|sp|sv|hmac|auth|authorization|nva|nvb)$",
    re.I,
)
_MANIFEST_PROTOCOLS = {"m3u8", "m3u8_native", "http_dash_segments", "http_dash_segments_generator", "ism", "f4m"}


# --------------------------------------------------------------------------- #
# Public helpers
# --------------------------------------------------------------------------- #
def get_engine_info() -> dict[str, Optional[str]]:
    """Return the installed yt-dlp version (useful when diagnosing outdated extractors)."""
    try:
        from yt_dlp.version import __version__

        return {"yt_dlp": __version__}
    except Exception:  # noqa: BLE001
        return {"yt_dlp": None}


def resolve_format_selector(fmt: Optional[str], config: ExtractorConfig) -> str:
    """Map aliases and validate a user-supplied format selector."""
    value = (fmt or config.default_format or "best").strip()
    key = value.lower()
    if key in FORMAT_ALIASES:
        return FORMAT_ALIASES[key]
    if len(value) > config.max_format_length or not _FORMAT_SELECTOR_RE.match(value):
        raise ExtractionError(ErrorCode.FORMAT_NOT_AVAILABLE, "The format selector is invalid.",
                              detail="invalid format selector")
    return value


def precheck_request(url: str, format: Optional[str] = None, config: Optional[ExtractorConfig] = None) -> str:  # noqa: A002
    """Validate URL (syntax, SSRF/DNS) and format selector without extracting; returns the normalised URL."""
    config = config or get_config()
    resolve_format_selector(format, config)
    return validate_url(url, config)


# --------------------------------------------------------------------------- #
# yt-dlp options
# --------------------------------------------------------------------------- #
class _YdlLogger:
    """Route yt-dlp output into our logger, with secrets and URL queries removed."""

    def __init__(self, verbose: bool) -> None:
        self.verbose = verbose

    def debug(self, msg: str) -> None:
        if self.verbose:
            log.debug("yt-dlp: %s", sanitize_text(msg, 300))

    def info(self, msg: str) -> None:
        self.debug(msg)

    def warning(self, msg: str) -> None:
        log.warning("yt-dlp: %s", sanitize_text(msg, 300))

    def error(self, msg: str) -> None:
        log.warning("yt-dlp error: %s", sanitize_text(msg, 300))


_VALID_RUNTIMES = {"deno", "node", "bun", "quickjs"}
_REMOTE_COMPONENT_RE = re.compile(r"^[a-z0-9_-]+:[a-z0-9_-]+$", re.I)


def build_ydl_options(
    config: ExtractorConfig,
    *,
    metadata_only: bool,
    format_selector: str,
    cookie_opts: dict[str, Any],
    proxy: Optional[str] = None,
    single: bool = False,
) -> dict[str, Any]:
    """Construct the yt-dlp option dict (no downloads, no user config files)."""
    opts: dict[str, Any] = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": False,
        "no_color": True,
        "noprogress": True,
        "logger": _YdlLogger(config.verbose),
        "ignoreconfig": True,          # never read yt-dlp.conf from the host
        "enable_file_urls": False,
        "socket_timeout": config.socket_timeout,
        "retries": config.download_retries,
        "extractor_retries": config.extractor_retries,
        "fragment_retries": config.fragment_retries,
        "noplaylist": False,
        "extract_flat": "in_playlist",
        "playlistend": config.max_playlist_entries,
        "format": format_selector,
        "ignore_no_formats_error": metadata_only,
        "http_headers": build_headers(config),
        "js_runtimes": {name: {} for name in config.js_runtimes if name in _VALID_RUNTIMES},
    }
    if config.remote_components:
        opts["remote_components"] = [c for c in config.remote_components if _REMOTE_COMPONENT_RE.match(c)]
    proxy = proxy or config.proxy
    if proxy:
        opts["proxy"] = _validated_proxy(proxy)
    if config.force_ipv4:
        opts["source_address"] = "0.0.0.0"            # yt-dlp -4: YouTube links are bound to one IP family
    if config.sleep_requests:
        opts["sleep_interval_requests"] = config.sleep_requests
    if config.sleep_interval:
        opts["sleep_interval"] = config.sleep_interval
        opts["max_sleep_interval"] = max(config.max_sleep_interval, config.sleep_interval)
    if single:                      # one video only: never walk a playlist, skip the flat listing
        opts["noplaylist"] = True
        opts.pop("extract_flat", None)
        opts.pop("playlistend", None)
    opts.update(cookie_opts)
    return opts


def _validated_proxy(proxy: str) -> str:
    try:
        scheme = urlsplit(proxy).scheme.lower()
    except ValueError:
        scheme = ""
    if scheme not in {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}:
        raise ExtractionError(ErrorCode.CONFIG_ERROR, detail="proxy scheme not supported")
    return proxy


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def _run_yt_dlp(url: str, opts: dict[str, Any]) -> dict[str, Any]:
    import yt_dlp  # imported lazily so importing this module never needs yt-dlp

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info is None:
            raise ExtractionError(ErrorCode.NO_FORMATS, detail="extractor returned no info")
        # sanitize_info drops non-serialisable objects and resolves lazy playlist entries.
        return ydl.sanitize_info(info)


def _run_strategies(url: str, base_opts: dict[str, Any], policy: RetryPolicy, deadline: float,
                    config: ExtractorConfig, on_retry, report, attempt_base: int = 0) -> tuple[dict[str, Any], Optional[str]]:
    """Run the access strategies (see advanced.strategy_engine). Returns ``(info, proxy_used)``."""
    quick = RetryPolicy(max_attempts=2, base_delay=policy.base_delay, max_delay=policy.max_delay,
                        rate_limit_max_attempts=1)
    main_cookie = base_opts.get("cookiefile")
    cookie_copies: dict[str, str] = {}      # strategy name -> its private copy of the cookie file

    def attempt(name: str, overlay: dict[str, Any], proxy: Optional[str]) -> dict[str, Any]:
        opts = {**base_opts}
        opts.update({k: v for k, v in overlay.items()
                     if k not in ("http_headers", "extractor_args") and not k.startswith("_")})
        opts["http_headers"] = {**base_opts.get("http_headers", {}), **overlay.get("http_headers", {})}
        if "extractor_args" in overlay:
            opts["extractor_args"] = overlay["extractor_args"]
        if overlay.get("_cookies") is False:
            opts.pop("cookiefile", None)
            opts.pop("cookiesfrombrowser", None)
        elif opts.get("cookiefile"):
            # parallel attempts must not share one cookie file (yt-dlp rewrites it on close)
            copy = f"{opts['cookiefile']}.{uuid.uuid4().hex[:8]}"
            shutil.copyfile(opts["cookiefile"], copy)
            os.chmod(copy, 0o600)
            opts["cookiefile"] = copy
            cookie_copies[name] = copy
        if proxy:
            opts["proxy"] = _validated_proxy(proxy)
        else:
            opts.pop("proxy", None)
        return run_with_retries(lambda: _run_yt_dlp(url, opts), quick, deadline, on_retry=on_retry)

    info, proxy, name = strategy_engine.run(url, config, attempt, deadline, report, attempt_base)
    winner_copy = cookie_copies.get(name)
    if winner_copy and main_cookie:
        try:                                  # keep cookies YouTube rotated during the winning attempt
            shutil.copyfile(winner_copy, main_cookie)
        except OSError:
            pass
    return info, proxy


_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key: tuple, ttl: float) -> Optional[dict]:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return copy.deepcopy(hit[1])
        _CACHE.pop(key, None)
    return None


def _cache_put(key: tuple, value: dict, cap: int) -> None:
    with _CACHE_LOCK:
        while len(_CACHE) >= cap:
            _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]), None)
        _CACHE[key] = (time.monotonic(), copy.deepcopy(value))


class _Flight:
    """One in-progress extraction that identical concurrent requests wait for instead of repeating."""
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Optional[dict] = None
        self.error: Optional[ExtractionError] = None


_FLIGHTS: dict[tuple, _Flight] = {}
_FLIGHT_LOCK = threading.Lock()


def extract(
    url: str,
    *,
    metadata_only: bool = False,
    format: Optional[str] = None,  # noqa: A002  (public keyword name matches the API field)
    config: Optional[ExtractorConfig] = None,
    timeout: Optional[float] = None,
    progress: Optional[ProgressCallback] = None,
    cancelled: Optional[Callable[[], bool]] = None,
    single: bool = False,
    attempt: int = 0,
) -> dict[str, Any]:
    """Extract metadata (and media URLs unless ``metadata_only``) for ``url``.

    Raises :class:`ExtractionError` for every failure; messages are safe to show
    to clients and ``error.detail`` holds sanitised diagnostics for logs.

    ``progress(stage, message)`` is called at each real lifecycle stage (see
    ``STAGES``); ``cancelled()`` is polled between stages and before retries.
    Neither couples the engine to any web framework.

    Speed: identical requests are served from a short-lived cache, identical *concurrent* requests share
    one extraction, and the strategy engine remembers which access method last worked for each site.
    """
    config = config or get_config()
    started = time.monotonic()
    deadline = started + (timeout or config.request_timeout)
    safe_url = ""
    flight: Optional[_Flight] = None
    try:
        def report(stage: str, message: str) -> None:
            if cancelled is not None and cancelled():
                raise ExtractionError(ErrorCode.CANCELLED)
            if progress is not None:
                try:
                    progress(stage, message)
                except Exception:  # noqa: BLE001 - reporting must never break extraction
                    log.debug("progress callback failed", exc_info=True)

        report("validating_url", "Validating URL")
        log.info("start %s", sanitize_url(url))
        selector = resolve_format_selector(format, config)
        normalized = validate_url(url, config)
        safe_url = sanitize_url(normalized)
        cache_key = (normalized, selector, bool(metadata_only), bool(single))
        cached = _cache_get(cache_key, config.cache_ttl) if attempt == 0 else None   # a retry wants a fresh route
        if cached is not None:
            metrics.inc("cache_hits")
            metrics.inc("extract_ok")
            log.info("done  %s · cache hit · %.1fs", cached["platform"]["key"], time.monotonic() - started)
            report("resolving_formats", "Loaded from cache")
            return cached

        with _FLIGHT_LOCK:
            existing = _FLIGHTS.get(cache_key)
            if existing is None:
                flight = _FLIGHTS[cache_key] = _Flight()
        if existing is not None:
            report("extracting_metadata", "Joining an identical request already in progress")
            if existing.event.wait(max(1.0, deadline - time.monotonic())):
                if existing.error is not None:
                    raise existing.error
                if existing.result is not None:
                    metrics.inc("cache_hits")
                    metrics.inc("extract_ok")
                    return copy.deepcopy(existing.result)
            # the leader failed to deliver in time: do our own extraction below

        resolve_redirects(normalized, config)
        report("initializing_extractor", "Connecting to the extractor")

        policy = RetryPolicy(
            max_attempts=config.retry_max_attempts,
            base_delay=config.retry_base_delay,
            max_delay=config.retry_max_delay,
            rate_limit_max_attempts=config.retry_rate_limit_attempts,
        )
        with cookie_options(config, normalized) as cookie_opts:
            opts = build_ydl_options(config, metadata_only=metadata_only, format_selector=selector,
                                     cookie_opts=cookie_opts, proxy=None, single=single)
            report("extracting_metadata", "Extracting video information")

            def on_retry(error: ExtractionError, attempt: int, delay: float) -> None:
                report("retrying", f"Temporary problem ({error.code.value}); retrying in {delay:.0f}s")

            info, used_proxy = _run_strategies(normalized, opts, policy, deadline, config, on_retry, report, attempt)

        if used_proxy:                         # media links are IP-bound: the download must use this exit IP
            proxy_manager.bind(normalized, used_proxy, config.proxy_affinity_ttl)
            log.info("proxy %s", redact(used_proxy))
        report("resolving_formats", "Resolving available formats and media URLs")
        result = normalize_result(info, config, metadata_only=metadata_only, format_selector=selector,
                                  input_url=normalized)
        _cache_put(cache_key, result, config.cache_max)
        if flight is not None:
            flight.result = copy.deepcopy(result)
        metrics.inc("extract_ok")
        metrics.platform(result["platform"]["key"] or "unknown")
        log.info("done  %s · %s · %.1fs", result["platform"]["key"], safe_url, time.monotonic() - started)
        return result
    except ExtractionError as err:
        _log_failure(err, safe_url, config)
        err = _adjust_for_cookies(err, config, normalized)
        if flight is not None:
            flight.error = err
        raise err
    except Exception as exc:  # noqa: BLE001
        err = classify_exception(exc)
        _log_failure(err, safe_url, config)
        err = _adjust_for_cookies(err, config, normalized)
        if flight is not None:
            flight.error = err
        raise err from None
    finally:
        if flight is not None:
            flight.event.set()
            with _FLIGHT_LOCK:
                _FLIGHTS.pop(cache_key, None)


def _adjust_for_cookies(err: ExtractionError, config: ExtractorConfig, url: str = "") -> ExtractionError:
    if err.code is ErrorCode.AUTH_REQUIRED and url and cookies_active(config, url):
        return ExtractionError(ErrorCode.COOKIES_INVALID, detail=err.detail)
    return err


def _log_failure(err: ExtractionError, safe_url: str, config: ExtractorConfig) -> None:
    metrics.inc("extract_fail")
    level = logging.ERROR if err.code in (ErrorCode.INTERNAL_ERROR, ErrorCode.CONFIG_ERROR,
                                          ErrorCode.EXTRACTOR_OUTDATED) else logging.WARNING
    log.log(level, "failed %s · %s · %s", err.code.value, safe_url or "-", sanitize_text(err.detail or "", 140))


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
def _iso_date(value: Any) -> Optional[str]:
    if isinstance(value, str) and re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return None


def _iso_ts(value: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _num(value: Any) -> Optional[float | int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _str(value: Any, limit: int = 1000) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _codec(value: Any) -> Optional[str]:
    text = _str(value, 100)
    return None if text in (None, "none") else text


def _expiry(url: str) -> dict[str, Any]:
    """Best-effort lifetime hint for a media URL (never authoritative)."""
    try:
        query = {k.lower(): v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    except ValueError:
        return {"status": "unknown", "expires_at": None}
    expires_at: Optional[str] = None
    for key in ("expire", "expires", "exp", "e"):
        raw = query.get(key, "")
        if raw.isdigit() and 1_500_000_000 <= int(raw) <= 4_500_000_000:
            expires_at = _iso_ts(int(raw))
            break
    if expires_at is None and query.get("x-amz-expires", "").isdigit() and re.fullmatch(r"\d{8}T\d{6}Z", query.get("x-amz-date", "")):
        try:
            start = datetime.strptime(query["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            expires_at = datetime.fromtimestamp(start.timestamp() + int(query["x-amz-expires"]), tz=timezone.utc).isoformat()
        except (ValueError, OverflowError):
            expires_at = None
    if expires_at:
        return {"status": "known", "expires_at": expires_at}
    if any(_SIGNING_PARAM_RE.match(k) for k in query):
        return {"status": "likely_temporary", "expires_at": None}
    return {"status": "unknown", "expires_at": None}


def _safe_headers(headers: Any) -> dict[str, str]:
    if not isinstance(headers, dict):
        return {}
    return {str(k): str(v) for k, v in headers.items()
            if isinstance(v, (str, int, float)) and not _SENSITIVE_HEADER_RE.search(str(k))}


def _format_kind(vcodec: Optional[str], acodec: Optional[str], raw: dict[str, Any]) -> str:
    has_v = vcodec is not None or (raw.get("vcodec") is None and raw.get("height") is not None)
    has_a = acodec is not None
    if raw.get("acodec") is None and raw.get("vcodec") is None:
        # Codecs unknown: assume a combined file if it has a resolution, else audio.
        has_a = has_v = raw.get("height") is not None or raw.get("width") is not None
        if not has_v and raw.get("abr") is not None:
            has_a = True
    if has_v and has_a:
        return "video+audio"
    if has_v:
        return "video-only"
    if has_a:
        return "audio-only"
    return "unknown"


def _normalize_format(raw: dict[str, Any], config: ExtractorConfig, *, include_url: bool,
                      host_cache: dict[str, bool], webpage_url: Optional[str],
                      direct: bool) -> dict[str, Any]:
    vcodec, acodec = _codec(raw.get("vcodec")), _codec(raw.get("acodec"))
    protocol = _str(raw.get("protocol"), 50)
    url = _str(raw.get("url"), 8192)
    usable = bool(url) and is_public_media_url(url, config, host_cache) and not (
        url == webpage_url and not direct)
    headers = _safe_headers(raw.get("http_headers"))
    entry: dict[str, Any] = {
        "format_id": _str(raw.get("format_id"), 100),
        "kind": _format_kind(vcodec, acodec, raw),
        "ext": _str(raw.get("ext"), 20),
        "container": _str(raw.get("container"), 50),
        "protocol": protocol,
        "is_manifest": protocol in _MANIFEST_PROTOCOLS,
        "resolution": _str(raw.get("resolution"), 30),
        "width": _num(raw.get("width")),
        "height": _num(raw.get("height")),
        "fps": _num(raw.get("fps")),
        "vcodec": vcodec,
        "acodec": acodec,
        "dynamic_range": _str(raw.get("dynamic_range"), 20),
        "tbr": _num(raw.get("tbr")),
        "vbr": _num(raw.get("vbr")),
        "abr": _num(raw.get("abr")),
        "asr": _num(raw.get("asr")),
        "audio_channels": _num(raw.get("audio_channels")),
        "language": _str(raw.get("language"), 20),
        "format_note": _str(raw.get("format_note"), 100),
        "filesize": _num(raw.get("filesize")),
        "filesize_approx": _num(raw.get("filesize_approx")),
        "has_url": usable,
    }
    if include_url:
        entry["download_url"] = url if usable else None
        entry["http_headers"] = headers if usable else {}
        entry["requires_headers"] = usable and any(k.lower() not in _DEFAULT_HEADER_NAMES for k in headers)
        entry["expiry"] = _expiry(url) if usable else {"status": "unknown", "expires_at": None}
    return entry


def _build_stream(raw: dict[str, Any], fmt: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
    headers = _safe_headers(raw.get("http_headers") or info.get("http_headers"))
    stream = {k: fmt.get(k) for k in (
        "format_id", "kind", "ext", "protocol", "is_manifest", "resolution", "vcodec", "acodec",
        "tbr", "filesize", "filesize_approx")}
    stream.update({
        "role": {"video+audio": "combined", "video-only": "video", "audio-only": "audio"}.get(fmt["kind"], "unknown"),
        "download_url": fmt.get("download_url"),
        "http_headers": headers,
        "requires_headers": any(k.lower() not in _DEFAULT_HEADER_NAMES for k in headers),
        "requires_cookies": bool(raw.get("cookies") or info.get("cookies")),
        "expiry": _expiry(fmt["download_url"]) if fmt.get("download_url") else {"status": "unknown", "expires_at": None},
    })
    return stream


def _thumbnails(info: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for t in info.get("thumbnails") or []:
        u = _str(t.get("url"), 2048) if isinstance(t, dict) else None
        if u and u.lower().startswith(("http://", "https://")):
            items.append({"url": u, "width": _num(t.get("width")), "height": _num(t.get("height")),
                          "id": _str(t.get("id"), 50), "_p": _num(t.get("preference")) or 0})
    items.sort(key=lambda t: t["_p"])  # yt-dlp lists worst -> best
    for t in items:
        del t["_p"]
    items = items[::-1][:MAX_THUMBNAILS]
    main = _str(info.get("thumbnail"), 2048)
    if main and main.lower().startswith(("http://", "https://")) and all(t["url"] != main for t in items):
        items.insert(0, {"url": main, "width": None, "height": None, "id": "primary"})
    return items


def _subtitles(info: dict[str, Any]) -> dict[str, Any]:
    manual = sorted((info.get("subtitles") or {}).keys())[:MAX_LANGUAGES]
    auto = sorted((info.get("automatic_captions") or {}).keys())[:MAX_LANGUAGES]
    return {"available": bool(manual or auto), "manual_languages": manual, "automatic_languages": auto}


def _common_metadata(info: dict[str, Any]) -> dict[str, Any]:
    desc = _str(info.get("description"), MAX_DESCRIPTION_CHARS + 1)
    description_truncated = bool(desc and len(desc) > MAX_DESCRIPTION_CHARS)
    return {
        "id": _str(info.get("id"), 200),
        "title": _str(info.get("title"), 500),
        "description": desc[:MAX_DESCRIPTION_CHARS] if desc else None,
        "description_truncated": description_truncated,
        "webpage_url": _str(info.get("webpage_url"), 2048),
        "original_url": _str(info.get("original_url"), 2048),
        "uploader": _str(info.get("uploader"), 300),
        "uploader_id": _str(info.get("uploader_id"), 200),
        "uploader_url": _str(info.get("uploader_url"), 2048),
        "channel": _str(info.get("channel"), 300),
        "channel_id": _str(info.get("channel_id"), 200),
        "channel_url": _str(info.get("channel_url"), 2048),
        "channel_follower_count": _num(info.get("channel_follower_count")),
        "creators": [str(c)[:200] for c in (info.get("creators") or [])][:20]
                    or ([str(info["creator"])[:200]] if info.get("creator") else []),
        "artists": [str(a)[:200] for a in (info.get("artists") or [])][:20]
                   or ([str(info["artist"])[:200]] if info.get("artist") else []),
        "thumbnails": _thumbnails(info),
        "thumbnail": _str(info.get("thumbnail"), 2048),
    }


def normalize_result(info: dict[str, Any], config: ExtractorConfig, *, metadata_only: bool,
                     format_selector: str, input_url: str) -> dict[str, Any]:
    """Convert a raw yt-dlp info dict into the stable response structure."""
    platform = {
        "key": _str(info.get("extractor_key") or info.get("ie_key"), 100),
        "name": _str(info.get("extractor"), 100),
    }
    base = {
        "input_url": input_url,
        "metadata_only": metadata_only,
        "platform": platform,
        "engine": get_engine_info(),
        "notes": [],
    }
    if info.get("_type") in ("playlist", "multi_video"):
        return _normalize_playlist(info, config, base, metadata_only)
    return _normalize_video(info, config, base, metadata_only, format_selector)


def _normalize_playlist(info: dict[str, Any], config: ExtractorConfig, base: dict[str, Any],
                        metadata_only: bool) -> dict[str, Any]:
    host_cache: dict[str, bool] = {}
    entries = []
    for idx, e in enumerate(info.get("entries") or [], start=1):
        if not isinstance(e, dict) or idx > config.max_playlist_entries:
            continue
        url = _str(e.get("url") or e.get("webpage_url"), 2048)
        if url and not is_public_media_url(url, config, host_cache):
            url = None
        entries.append({
            "index": idx,
            "id": _str(e.get("id"), 200),
            "title": _str(e.get("title"), 500),
            "url": url,
            "url_type": "playlist" if e.get("_type") == "playlist" else "webpage",
            "duration": _num(e.get("duration")),
            "uploader": _str(e.get("uploader") or e.get("channel"), 300),
            "thumbnail": next((t["url"] for t in _thumbnails(e)), None),
        })
    meta = _common_metadata(info)
    for k in ("description_truncated", "thumbnail", "original_url", "channel_follower_count"):
        meta.pop(k, None)
    count = _num(info.get("playlist_count")) or len(entries)
    base["notes"].append("This URL is a playlist. Entry URLs are webpage URLs, not media URLs; submit an "
                         "individual entry URL to extract its media.")
    if count > len(entries):
        base["notes"].append(f"Only the first {len(entries)} entries are listed.")
    return {**base, "result_type": "playlist", **meta, "entry_count": count, "entries": entries,
            "formats": [], "selected": None, "media": None}


def _normalize_video(info: dict[str, Any], config: ExtractorConfig, base: dict[str, Any],
                     metadata_only: bool, format_selector: str) -> dict[str, Any]:
    direct = bool(info.get("direct"))
    webpage_url = _str(info.get("webpage_url"), 2048)
    host_cache: dict[str, bool] = {}

    raw_formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    if not raw_formats and info.get("url"):  # single-format extractors expose the URL at top level
        raw_formats = [{k: info.get(k) for k in (
            "format_id", "ext", "protocol", "width", "height", "fps", "vcodec", "acodec", "tbr", "abr", "asr",
            "filesize", "filesize_approx", "http_headers", "url", "resolution", "format_note", "language",
            "container", "dynamic_range", "vbr", "audio_channels", "cookies")}]
    # Drop storyboards / non-media entries.
    raw_formats = [f for f in raw_formats if f.get("protocol") != "mhtml" and f.get("ext") != "mhtml"]

    formats = [_normalize_format(f, config, include_url=not metadata_only, host_cache=host_cache,
                                 webpage_url=webpage_url, direct=direct) for f in raw_formats][::-1][:MAX_FORMATS]
    by_id = {f["format_id"]: f for f in formats if f["format_id"]}

    live_status = _str(info.get("live_status"), 30)
    meta = _common_metadata(info)
    result: dict[str, Any] = {
        **base,
        "result_type": "direct_media" if direct else "video",
        **meta,
        "duration": _num(info.get("duration")),
        "duration_string": _str(info.get("duration_string"), 20),
        "upload_date": _iso_date(info.get("upload_date")),
        "timestamp": _iso_ts(info.get("timestamp") or info.get("release_timestamp")),
        "is_live": bool(info.get("is_live")),
        "was_live": bool(info.get("was_live")),
        "live_status": live_status,
        "availability": _str(info.get("availability"), 30),
        "age_limit": _num(info.get("age_limit")),
        "categories": [str(c)[:100] for c in (info.get("categories") or [])][:20],
        "tags": [str(t)[:100] for t in (info.get("tags") or [])][:MAX_TAGS],
        "language": _str(info.get("language"), 20),
        "stats": {
            "view_count": _num(info.get("view_count")),
            "like_count": _num(info.get("like_count")),
            "dislike_count": _num(info.get("dislike_count")),
            "comment_count": _num(info.get("comment_count")),
            "repost_count": _num(info.get("repost_count")),
            "average_rating": _num(info.get("average_rating")),
        },
        "subtitles": _subtitles(info),
        "formats": formats,
        "selected": None,
        "media": None,
        "download_url": None,
        "download_url_info": None,
        "download_url_note": None,
    }

    if metadata_only:
        result["download_url_note"] = "Metadata-only request: media URLs were not requested."
        result["notes"].append("Metadata only: media URLs were not requested.")
        for f in result["formats"]:
            f["download_url"] = None
        return result

    requested = [f for f in (info.get("requested_formats") or []) if isinstance(f, dict)]
    chosen_raw = requested or ([info] if info.get("url") else [])
    streams: list[dict[str, Any]] = []
    for raw in chosen_raw:
        fmt = by_id.get(_str(raw.get("format_id"), 100) or "")
        if fmt is None:
            fmt = _normalize_format(raw, config, include_url=True, host_cache=host_cache,
                                    webpage_url=webpage_url, direct=direct)
        if "download_url" not in fmt:
            fmt = {**fmt, "download_url": None}
        streams.append(_build_stream(raw, fmt, info))

    if not streams or any(not s["download_url"] for s in streams):
        raise ExtractionError(
            ErrorCode.NO_MEDIA_URL,
            detail=f"selected {len(streams)} stream(s), usable urls: {sum(1 for s in streams if s['download_url'])}",
        )

    separate = len(streams) > 1
    _set_download_url(result, streams, separate)
    result["selected"] = {
        "format_selector": format_selector,
        "format_ids": [s["format_id"] for s in streams],
        "format_label": _str(info.get("format"), 200),
        "resolution": _str(info.get("resolution"), 30),
        "ext": _str(info.get("ext"), 20),
    }
    result["media"] = {
        "layout": "separate_streams" if separate else "single",
        "requires_merge": separate,
        "streams": streams,
    }
    result["notes"].append("Media URLs may be temporary or tied to the requesting IP address and headers; "
                           "re-extract if a URL stops working.")
    if separate:
        result["notes"].append("Audio and video are separate streams and must be merged (e.g. with ffmpeg).")
    if any(s["is_manifest"] for s in streams):
        result["notes"].append("At least one URL is a streaming manifest (HLS/DASH), not a single media file.")
    if any(s["requires_headers"] for s in streams):
        result["notes"].append("Some streams require the listed HTTP request headers.")
    if any(s["requires_cookies"] for s in streams):
        result["notes"].append("Some streams require session cookies that are not exposed by this API.")
    if result["is_live"]:
        result["notes"].append("This is a live stream; the URL points at the live manifest.")
    return result


def _set_download_url(result: dict[str, Any], streams: list[dict[str, Any]], separate: bool) -> None:
    """Populate the first-class ``download_url`` fields from the selected streams.

    ``download_url`` is set only when the selection is one single stream, so a
    lone audio or video stream is never presented as a complete video. The URL
    comes straight from the extractor; it is not fetched or verified here.
    """
    if separate:
        result["download_url_note"] = (
            "The selected quality is delivered as separate video and audio streams, so no single "
            "download_url exists. Use media.streams (one video, one audio) and merge them, or choose "
            "the 'best_single' format.")
        return
    s = streams[0]
    kind = {"combined": "video+audio", "video": "video-only", "audio": "audio-only"}.get(s["role"], "unknown")
    result["download_url"] = s["download_url"]
    result["download_url_info"] = {
        "format_id": s["format_id"], "ext": s["ext"], "resolution": s["resolution"], "contents": kind,
        "complete_video": kind == "video+audio", "is_manifest": s["is_manifest"],
        "http_headers": s["http_headers"], "requires_headers": s["requires_headers"],
        "requires_cookies": s["requires_cookies"], "expiry": s["expiry"], "verified": False,
    }
    notes = ["The URL was returned by the extractor and has not been checked for reachability."]
    if kind == "video-only":
        notes.append("This stream has no audio track.")
    elif kind == "audio-only":
        notes.append("This stream contains audio only.")
    elif kind == "unknown":
        notes.append("The extractor did not say whether this stream has audio, video or both.")
    if s["is_manifest"]:
        notes.append("This is a streaming manifest (HLS/DASH), not a single downloadable file.")
    result["download_url_note"] = " ".join(notes)
