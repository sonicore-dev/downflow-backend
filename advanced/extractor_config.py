"""Operator configuration for the extraction engine.

All settings come from environment variables (prefix ``DOWNFLOW_``) with
conservative defaults, so nothing needs editing in source. See README section
"Extraction engine configuration" for the full list.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

__all__ = ["ExtractorConfig", "get_config"]

log = logging.getLogger("downflow.config")

ADVANCED_DIR = Path(__file__).resolve().parent
BASE_DIR = ADVANCED_DIR.parent
DEFAULT_USER_AGENT_FILE = ADVANCED_DIR / "headers" / "user-agents.txt"

_PREFIX = "DOWNFLOW_"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _env(name: str) -> Optional[str]:
    value = os.environ.get(_PREFIX + name)
    return value.strip() if value is not None and value.strip() != "" else None


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    low = raw.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    log.warning("Ignoring invalid boolean for %s%s; using %s", _PREFIX, name, default)
    return default


def _num(name: str, default, cast, minimum, maximum):
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = cast(raw)
    except ValueError:
        log.warning("Ignoring invalid number for %s%s; using %s", _PREFIX, name, default)
        return default
    return max(minimum, min(maximum, value))


def _int(name, default, minimum, maximum):
    return _num(name, default, int, minimum, maximum)


def _float(name, default, minimum, maximum):
    return _num(name, default, float, minimum, maximum)


def _list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _env(name)
    if raw is None:
        return default
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _proxy_file() -> tuple[str, ...]:
    """Read one proxy URL per line from DOWNFLOW_PROXY_FILE (blank lines and # comments ignored)."""
    path = _env("PROXY_FILE")
    if not path:
        return ()
    try:
        lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    except OSError:
        log.warning("Cannot read %sPROXY_FILE", _PREFIX)
        return ()
    return tuple(l.strip() for l in lines if l.strip() and not l.strip().startswith("#"))


@dataclass(frozen=True)
class ExtractorConfig:
    # Network
    socket_timeout: float = 20.0
    request_timeout: float = 60.0            # overall per-request budget (API + retries)
    extractor_retries: int = 2               # yt-dlp internal extractor retries
    download_retries: int = 2                # yt-dlp "retries"
    fragment_retries: int = 2
    # Retry manager
    retry_max_attempts: int = 3
    retry_base_delay: float = 1.5
    retry_max_delay: float = 20.0
    retry_rate_limit_attempts: int = 2
    # Concurrency / limits
    max_concurrent: int = 4
    queue_wait: float = 30.0                 # seconds a queued task waits for a free slot
    max_queued: int = 50                     # tasks allowed to wait for a slot
    task_ttl: float = 900.0                  # seconds finished tasks stay retrievable
    max_tasks: int = 500                     # hard cap on tracked tasks
    max_url_length: int = 2048
    max_playlist_entries: int = 50
    # Formats
    default_format: str = "best"
    max_format_length: int = 200
    # Proxy / cookies
    proxy: Optional[str] = None
    proxy_list: tuple[str, ...] = ()         # optional pool (rotating / residential); see advanced/proxy_manager.py
    proxy_mode: str = "sticky"               # sticky (same proxy per video) | round_robin | random
    cookie_file: Optional[str] = None        # explicit path; default is <cookies_dir>/youtube.txt
    cookies_dir: str = str(BASE_DIR / "cookies")
    cookies_from_browser: Optional[str] = None   # yt-dlp spec: BROWSER[+KEYRING][:PROFILE][::CONTAINER]
    browser_profile_dir: Optional[str] = None    # persistent Chrome/Chromium --user-data-dir
    browser_name: str = "chromium"
    browser_keyring: str = "basictext"
    cookie_source: str = "auto"              # auto | file | browser | none
    cookie_writeback: bool = True            # keep rotated cookies yt-dlp refreshes during use
    cookie_check_interval: float = 3600.0    # seconds between live validity checks (0 = off)
    # Anti-bot behaviour
    force_ipv4: bool = True
    sleep_requests: float = 0.5              # pause between extractor requests (seconds)
    sleep_interval: float = 0.0              # pause before each download (yt-dlp --sleep-interval)
    max_sleep_interval: float = 0.0
    yt_player_clients: tuple[str, ...] = ()  # empty = automatic (cookie-friendly clients when cookies are active)
    yt_po_token: tuple[str, ...] = ()        # e.g. "web.gvs+XXXX" (see yt-dlp PO Token guide)
    yt_visitor_data: Optional[str] = None
    # Headers
    rotate_user_agent: bool = False
    user_agent_file: str = str(DEFAULT_USER_AGENT_FILE)
    accept_language: str = "en-US,en;q=0.9"
    # Security
    allowed_ports: tuple[int, ...] = (80, 443)
    allow_private_hosts: bool = False        # SSRF guard override (development only)
    check_redirects: bool = True
    max_redirects: int = 5
    redirect_check_timeout: float = 8.0
    # yt-dlp
    js_runtimes: tuple[str, ...] = ("deno", "node")
    remote_components: tuple[str, ...] = ()  # e.g. "ejs:github" to allow solver download
    verbose: bool = False

    @property
    def default_cookie_file(self) -> str:
        """The cookie file in use: DOWNFLOW_COOKIE_FILE, else <cookies_dir>/youtube.txt."""
        return self.cookie_file or str(Path(self.cookies_dir).expanduser() / "youtube.txt")

    @classmethod
    def from_env(cls) -> "ExtractorConfig":
        d = cls()
        return cls(
            socket_timeout=_float("SOCKET_TIMEOUT", d.socket_timeout, 3, 120),
            request_timeout=_float("REQUEST_TIMEOUT", d.request_timeout, 5, 300),
            extractor_retries=_int("EXTRACTOR_RETRIES", d.extractor_retries, 0, 10),
            download_retries=_int("DOWNLOAD_RETRIES", d.download_retries, 0, 10),
            fragment_retries=_int("FRAGMENT_RETRIES", d.fragment_retries, 0, 10),
            retry_max_attempts=_int("RETRY_MAX_ATTEMPTS", d.retry_max_attempts, 1, 6),
            retry_base_delay=_float("RETRY_BASE_DELAY", d.retry_base_delay, 0, 30),
            retry_max_delay=_float("RETRY_MAX_DELAY", d.retry_max_delay, 0, 120),
            retry_rate_limit_attempts=_int("RETRY_RATE_LIMIT_ATTEMPTS", d.retry_rate_limit_attempts, 1, 4),
            max_concurrent=_int("MAX_CONCURRENT", d.max_concurrent, 1, 64),
            queue_wait=_float("QUEUE_WAIT", d.queue_wait, 0, 300),
            max_queued=_int("MAX_QUEUED", d.max_queued, 0, 1000),
            task_ttl=_float("TASK_TTL", d.task_ttl, 30, 86400),
            max_tasks=_int("MAX_TASKS", d.max_tasks, 10, 10000),
            max_url_length=_int("MAX_URL_LENGTH", d.max_url_length, 64, 8192),
            max_playlist_entries=_int("MAX_PLAYLIST_ENTRIES", d.max_playlist_entries, 1, 500),
            default_format=_env("DEFAULT_FORMAT") or d.default_format,
            proxy=_env("PROXY"),
            proxy_list=_list("PROXY_LIST", d.proxy_list) or _proxy_file(),
            proxy_mode=(_env("PROXY_MODE") or d.proxy_mode).lower(),
            cookie_file=_env("COOKIE_FILE"),
            cookies_dir=_env("COOKIES_DIR") or d.cookies_dir,
            cookies_from_browser=_env("COOKIES_FROM_BROWSER"),
            browser_profile_dir=_env("BROWSER_PROFILE_DIR"),
            browser_name=(_env("BROWSER_NAME") or d.browser_name).lower(),
            browser_keyring=(_env("BROWSER_KEYRING") or d.browser_keyring).lower(),
            cookie_source=(_env("COOKIE_SOURCE") or d.cookie_source).lower(),
            cookie_writeback=_bool("COOKIE_WRITEBACK", d.cookie_writeback),
            cookie_check_interval=_float("COOKIE_CHECK_INTERVAL", d.cookie_check_interval, 0, 86400),
            force_ipv4=_bool("FORCE_IPV4", d.force_ipv4),
            sleep_requests=_float("SLEEP_REQUESTS", d.sleep_requests, 0, 10),
            sleep_interval=_float("SLEEP_INTERVAL", d.sleep_interval, 0, 60),
            max_sleep_interval=_float("MAX_SLEEP_INTERVAL", d.max_sleep_interval, 0, 120),
            yt_player_clients=_list("YT_PLAYER_CLIENTS", d.yt_player_clients),
            yt_po_token=_list("YT_PO_TOKEN", d.yt_po_token),
            yt_visitor_data=_env("YT_VISITOR_DATA"),
            rotate_user_agent=_bool("ROTATE_USER_AGENT", d.rotate_user_agent),
            user_agent_file=_env("USER_AGENT_FILE") or d.user_agent_file,
            accept_language=_env("ACCEPT_LANGUAGE") or d.accept_language,
            allowed_ports=tuple(int(p) for p in _list("ALLOWED_PORTS", ("80", "443")) if p.isdigit()) or d.allowed_ports,
            allow_private_hosts=_bool("ALLOW_PRIVATE_HOSTS", d.allow_private_hosts),
            check_redirects=_bool("CHECK_REDIRECTS", d.check_redirects),
            max_redirects=_int("MAX_REDIRECTS", d.max_redirects, 0, 10),
            redirect_check_timeout=_float("REDIRECT_CHECK_TIMEOUT", d.redirect_check_timeout, 1, 30),
            js_runtimes=_list("JS_RUNTIMES", d.js_runtimes),
            remote_components=_list("REMOTE_COMPONENTS", d.remote_components),
            verbose=_bool("VERBOSE", d.verbose),
        )


@lru_cache(maxsize=1)
def get_config() -> ExtractorConfig:
    """Return the process-wide configuration (read once from the environment)."""
    return ExtractorConfig.from_env()
