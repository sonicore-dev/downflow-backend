"""Operator-supplied cookie handling (file or persistent browser profile).

Cookies are never collected automatically. The operator configures one source:

* a Netscape ``cookies.txt`` -- by default ``cookies/youtube.txt`` (override the folder with
  ``DOWNFLOW_COOKIES_DIR`` or the file with ``DOWNFLOW_COOKIE_FILE``), or
* a browser profile on the server -- ``DOWNFLOW_COOKIES_FROM_BROWSER`` (yt-dlp spec, e.g.
  ``chromium+basictext:/data/browser-profile``) or ``DOWNFLOW_BROWSER_PROFILE_DIR``.

``DOWNFLOW_COOKIE_SOURCE`` = ``auto`` (default: file if present, else browser) | ``file`` | ``browser`` | ``none``.

Hot reload: the file is read again for every extraction, so replacing it (or POSTing new text
to ``/api/v1/admin/cookies``) takes effect immediately, with no restart.

yt-dlp rewrites its cookie file while running (YouTube rotates ``__Secure-*PSIDTS`` cookies), so
each extraction works on a private temporary copy. When ``DOWNFLOW_COOKIE_WRITEBACK`` is on, the
rotated copy is merged back atomically, which keeps the session alive much longer.
Cookie values are never logged or returned by the API.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from .error_handler import ErrorCode, ExtractionError
from .extractor_config import ExtractorConfig, get_config

__all__ = ["cookie_options", "cookies_active", "validate_cookie_file", "parse_browser_spec", "browser_spec_string",
           "cli_args", "cookie_status", "install_cookie_text", "check_cookies_live", "last_check", "resolve_source"]

log = logging.getLogger("downflow.cookies")

MAX_COOKIE_FILE_BYTES = 5 * 1024 * 1024
SUPPORTED_BROWSERS = {"brave", "chrome", "chromium", "edge", "firefox", "opera", "safari", "vivaldi", "whale"}
SUPPORTED_KEYRINGS = {"basictext", "gnomekeyring", "kwallet", "kwallet5", "kwallet6"}
_NETSCAPE_HEADERS = ("# netscape http cookie file", "# http cookie file")
# Cookies that prove a logged-in Google session.
AUTH_COOKIES = ("SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID", "__Secure-3PAPISID", "LOGIN_INFO")

_write_lock = threading.Lock()
_state_lock = threading.Lock()
_last_check: dict[str, Any] = {"valid": None, "checked_at": None, "detail": "not checked yet"}


# --------------------------------------------------------------------------- #
# parsing + validation
# --------------------------------------------------------------------------- #
def _fail(reason: str) -> ExtractionError:
    # The reason is logged server-side only; the client message is generic.
    return ExtractionError(ErrorCode.CONFIG_ERROR, "The configured cookie file is missing or invalid.",
                           detail=f"cookie file problem: {reason}")


def parse_netscape(text: str) -> list[tuple]:
    """Parse cookie text into ``(domain, flag, path, secure, expires, name, value)`` tuples.

    Accepts tab- or whitespace-separated 7-field lines and the ``#HttpOnly_`` prefix.
    """
    rows = []
    for raw in text.splitlines():
        line = raw.strip("\r\n")
        if not line.strip():
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            parts = line.split(None, 6)
        if len(parts) != 7:
            continue
        d, flag, path, secure, exp, name, value = [x.strip() for x in parts]
        if not name or not re.fullmatch(r"-?\d+", exp or "x"):
            continue
        rows.append((d, flag.upper(), path or "/", secure.upper(), exp, name, value))
    return rows


def _dedupe(rows: list[tuple]) -> list[tuple]:
    """Keep the latest expiry per (domain, path, name)."""
    best: dict[tuple, tuple] = {}
    for r in rows:
        k = (r[0].lower(), r[2], r[5])
        if k not in best or int(r[4]) >= int(best[k][4]):
            best[k] = r
    return list(best.values())


def _render(rows: list[tuple]) -> str:
    out = ["# Netscape HTTP Cookie File", "# Managed by Downflow. Contains credentials: keep private (chmod 600).", ""]
    out += ["\t".join(r) for r in rows]
    return "\n".join(out) + "\n"


def validate_cookie_file(path_str: str) -> Path:
    """Validate a cookie file path; raise a sanitised error if unusable."""
    path = Path(path_str).expanduser()
    try:
        if not path.is_file():
            raise _fail("not a file / does not exist")
        size = path.stat().st_size
        if size == 0 or size > MAX_COOKIE_FILE_BYTES:
            raise _fail(f"unexpected size ({size} bytes)")
        if not os.access(path, os.R_OK):
            raise _fail("not readable")
        text = path.read_text(encoding="utf-8", errors="strict")
        if not parse_netscape(text):
            raise _fail("not in Netscape cookie format")
    except ExtractionError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise _fail(exc.__class__.__name__) from exc
    return path


def inspect_rows(rows: list[tuple]) -> dict[str, Any]:
    """Summary of a cookie set. Never includes cookie values."""
    now = time.time()
    names = {r[5] for r in rows}
    auth = [n for n in AUTH_COOKIES if n in names]
    exps = [int(r[4]) for r in rows if r[5] in AUTH_COOKIES and int(r[4]) > 0]
    soonest = min(exps) if exps else None
    return {
        "count": len(rows),
        "domains": sorted({r[0].lstrip(".") for r in rows})[:12],
        "auth_cookies_present": auth,
        "logged_in_cookies": bool(auth),
        "earliest_auth_expiry": soonest,
        "expires_in_days": round((soonest - now) / 86400, 1) if soonest else None,
        "expired": bool(soonest and soonest < now),
    }


# --------------------------------------------------------------------------- #
# browser profile spec
# --------------------------------------------------------------------------- #
def parse_browser_spec(spec: str) -> tuple:
    """Parse yt-dlp's ``BROWSER[+KEYRING][:PROFILE][::CONTAINER]`` into the tuple yt-dlp expects."""
    m = re.fullmatch(r"(?P<name>[^+:]+)(?:\s*\+\s*(?P<keyring>[^:]+))?(?:\s*:\s*(?!:)(?P<profile>.+?))?(?:\s*::\s*(?P<container>.+))?", spec.strip())
    if not m:
        raise ExtractionError(ErrorCode.CONFIG_ERROR, "The configured cookie browser is not supported.",
                              detail="bad browser spec")
    name = m.group("name").strip().lower()
    keyring = (m.group("keyring") or "").strip().lower() or None
    if name not in SUPPORTED_BROWSERS or (keyring and keyring not in SUPPORTED_KEYRINGS):
        raise ExtractionError(ErrorCode.CONFIG_ERROR, "The configured cookie browser is not supported.",
                              detail=f"unsupported browser/keyring {name!r}/{keyring!r}")
    profile = (m.group("profile") or "").strip() or None
    container = (m.group("container") or "").strip() or None
    return (name, profile, keyring, container)


def browser_spec_string(t: tuple) -> str:
    name, profile, keyring, container = (list(t) + [None] * 4)[:4]
    return name + (f"+{keyring}" if keyring else "") + (f":{profile}" if profile else "") + (f"::{container}" if container else "")


def _browser_tuple(config: ExtractorConfig) -> Optional[tuple]:
    if config.cookies_from_browser:
        return parse_browser_spec(config.cookies_from_browser)
    if config.browser_profile_dir:
        return parse_browser_spec(f"{config.browser_name}+{config.browser_keyring}:{config.browser_profile_dir}")
    return None


# --------------------------------------------------------------------------- #
# source resolution
# --------------------------------------------------------------------------- #
def resolve_source(config: ExtractorConfig) -> tuple[Optional[str], Any]:
    """Return ``("file", Path)``, ``("browser", tuple)`` or ``(None, None)``."""
    mode = config.cookie_source
    if mode == "none":
        return None, None
    file_path = Path(config.default_cookie_file)
    if mode in ("auto", "file") and file_path.is_file():
        return "file", file_path
    if mode == "file":
        return None, None
    browser = _browser_tuple(config)
    if mode in ("auto", "browser") and browser:
        return "browser", browser
    return None, None


def cookies_active(config: Optional[ExtractorConfig] = None) -> bool:
    return resolve_source(config or get_config())[0] is not None


# --------------------------------------------------------------------------- #
# yt-dlp integration
# --------------------------------------------------------------------------- #
def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".cookies-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _writeback(src: Path, copy: Path, before_sha: str, src_sig: tuple, original_count: int) -> None:
    """Merge cookies rotated by yt-dlp back into the source file (best effort, never raises)."""
    try:
        if _sha(copy) == before_sha:
            return
        rows = _dedupe(parse_netscape(copy.read_text(encoding="utf-8", errors="ignore")))
        if sorted(rows) == sorted(_dedupe(parse_netscape(src.read_text(encoding="utf-8", errors="ignore")))):
            return                          # only formatting changed: nothing to save
        if len(rows) < max(1, int(original_count * 0.8)) or not inspect_rows(rows)["logged_in_cookies"]:
            log.warning("cookies: refreshed copy looks incomplete; keeping the original file")
            return
        with _write_lock:
            st = src.stat()
            if (st.st_mtime_ns, st.st_size) != src_sig:
                return                      # operator replaced the file meanwhile: never overwrite it
            _atomic_write(src, _render(rows))
        log.info("cookies: saved refreshed session cookies (%d)", len(rows))
    except Exception as exc:  # noqa: BLE001
        log.warning("cookies: could not save refreshed cookies (%s)", exc.__class__.__name__)


@contextlib.contextmanager
def cookie_options(config: ExtractorConfig) -> Iterator[dict[str, Any]]:
    """Yield yt-dlp option fragments for the active cookie source (if any)."""
    kind, src = resolve_source(config)
    if kind == "file":
        validate_cookie_file(str(src))
        st = src.stat()
        sig, before = (st.st_mtime_ns, st.st_size), _sha(src)
        count = len(parse_netscape(src.read_text(encoding="utf-8", errors="ignore")))
        tmp_dir = tempfile.mkdtemp(prefix="downflow-cookies-")
        try:
            os.chmod(tmp_dir, 0o700)
            copy = Path(tmp_dir) / "cookies.txt"
            shutil.copyfile(src, copy)
            os.chmod(copy, 0o600)
            try:
                yield {"cookiefile": str(copy)}
            finally:
                if config.cookie_writeback:
                    _writeback(src, copy, before, sig, count)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    elif kind == "browser":
        yield {"cookiesfrombrowser": src}
    else:
        yield {}


def cli_args(opts: dict[str, Any]) -> list[str]:
    """Translate :func:`cookie_options` output into yt-dlp command-line flags."""
    if opts.get("cookiefile"):
        return ["--cookies", opts["cookiefile"]]
    if opts.get("cookiesfrombrowser"):
        return ["--cookies-from-browser", browser_spec_string(opts["cookiesfrombrowser"])]
    return []


# --------------------------------------------------------------------------- #
# status, install, live check
# --------------------------------------------------------------------------- #
def cookie_status(config: Optional[ExtractorConfig] = None) -> dict[str, Any]:
    config = config or get_config()
    kind, src = resolve_source(config)
    out: dict[str, Any] = {"source": kind or "none", "configured_mode": config.cookie_source,
                           "writeback": config.cookie_writeback, "last_check": last_check()}
    if kind == "file":
        try:
            st = src.stat()
            out.update(inspect_rows(parse_netscape(src.read_text(encoding="utf-8", errors="ignore"))))
            out["file_modified"] = st.st_mtime
            out["file_mode"] = oct(st.st_mode & 0o777)
        except OSError:
            out["error"] = "cookie file unreadable"
    elif kind == "browser":
        out["browser"] = browser_spec_string(src).split(":")[0]
    else:
        out["hint"] = f"Put a Netscape cookie file at {config.default_cookie_file} or POST it to /api/v1/admin/cookies"
    return out


def install_cookie_text(text: str, config: Optional[ExtractorConfig] = None) -> dict[str, Any]:
    """Validate and atomically install new cookies (hot reload: the next request uses them)."""
    config = config or get_config()
    if len(text.encode("utf-8", "ignore")) > MAX_COOKIE_FILE_BYTES:
        raise _fail("too large")
    rows = _dedupe(parse_netscape(text))
    if not rows:
        raise _fail("not in Netscape cookie format")
    path = Path(config.default_cookie_file)
    with _write_lock:
        if path.is_file():
            with contextlib.suppress(OSError):
                shutil.copyfile(path, path.with_suffix(".bak"))
                os.chmod(path.with_suffix(".bak"), 0o600)
        _atomic_write(path, _render(rows))
    with _state_lock:
        _last_check.update({"valid": None, "checked_at": None, "detail": "cookies replaced; not checked yet"})
    log.info("cookies: installed %d cookies", len(rows))
    return cookie_status(config)


def last_check() -> dict[str, Any]:
    with _state_lock:
        return dict(_last_check)


_LOGGED_IN = re.compile(r'"LOGGED_IN"\s*:\s*(true|false)', re.I)


def check_cookies_live(config: Optional[ExtractorConfig] = None) -> dict[str, Any]:
    """Blocking: ask YouTube whether the current cookies still log in. Never returns cookie values."""
    from .proxy_manager import pick_proxy
    config = config or get_config()
    kind, src = resolve_source(config)
    result: dict[str, Any] = {"valid": None, "checked_at": time.time(), "detail": ""}
    try:
        if kind == "file":
            rows = [r for r in parse_netscape(src.read_text(encoding="utf-8", errors="ignore"))
                    if r[0].lstrip(".").endswith(("youtube.com", "google.com"))]
            info = inspect_rows(rows)
            if not info["logged_in_cookies"]:
                result.update(valid=False, detail="no login cookies in the file")
            elif info["expired"]:
                result.update(valid=False, detail="login cookies have expired")
            else:
                jar = {r[5]: r[6] for r in rows if r[0].lstrip(".").endswith("youtube.com")}
                header = "; ".join(f"{k}={v}" for k, v in jar.items())
                try:
                    from curl_cffi import requests as http
                    kw: dict[str, Any] = {"impersonate": "chrome"}
                except Exception:  # noqa: BLE001
                    import requests as http  # type: ignore
                    kw = {}
                proxy = pick_proxy(config, "cookie-check")
                if proxy:
                    kw["proxies"] = {"http": proxy, "https": proxy}
                resp = http.get("https://www.youtube.com/", timeout=20, allow_redirects=True,
                                headers={"Cookie": header, "Accept-Language": config.accept_language,
                                         "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}, **kw)
                m = _LOGGED_IN.search(resp.text or "")
                if m and m.group(1).lower() == "true":
                    result.update(valid=True, detail="signed in")
                elif m:
                    result.update(valid=False, detail="YouTube treats the cookies as signed out (expired or revoked)")
                else:
                    result.update(valid=None, detail=f"inconclusive (HTTP {resp.status_code}); may be a consent page")
        elif kind == "browser":
            result.update(valid=None, detail="browser profile source: validity is checked by yt-dlp at extraction time")
        else:
            result.update(valid=None, detail="no cookie source configured")
    except Exception as exc:  # noqa: BLE001
        result.update(valid=None, detail=f"check failed: {exc.__class__.__name__}")
    with _state_lock:
        _last_check.update(result)
    lvl = logging.INFO if result["valid"] else logging.WARNING
    log.log(lvl, "cookies: %s", result["detail"])
    return result
