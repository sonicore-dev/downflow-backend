# HTTP header / user-agent management.
#
# User-agent rotation is optional (``DOWNFLOW_ROTATE_USER_AGENT``) and is *not* a
# way to bypass access controls or rate limits; it only varies the browser
# identity on ordinary page requests. A user-agent is paired with a matching
# ``Accept`` header for its browser family so the combination stays coherent, and
# no client-hint (``Sec-CH-UA*``) headers are invented.

from __future__ import annotations

import logging
import random
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .extractor_config import ExtractorConfig

__all__ = ["load_user_agents", "build_headers", "FALLBACK_USER_AGENTS"]

log = logging.getLogger("downflow.headers")

FALLBACK_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
)

_UA_RE = re.compile(r"^Mozilla/5\.0 \([^()\r\n]{3,120}\) [\x20-\x7e]{3,200}$")
_MAX_UA_FILE_BYTES = 4 * 1024 * 1024

_ACCEPT_CHROMIUM = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
_ACCEPT_FIREFOX = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_ACCEPT_SAFARI = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


def _is_valid(ua: str) -> bool:
    return bool(_UA_RE.match(ua))


@lru_cache(maxsize=4)
def _load_cached(path_str: str, mtime_ns: int) -> tuple[str, ...]:
    path = Path(path_str)
    seen: set[str] = set()
    result: list[str] = []
    invalid = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if _is_valid(line) and line not in seen:
                seen.add(line)
                result.append(line)
            else:
                invalid += 1
    if invalid:
        log.warning("Skipped %d invalid or duplicate user-agent lines", invalid)
    return tuple(result)


def load_user_agents(path_str: str) -> tuple[str, ...]:
    # Load and validate user-agents; fall back to built-ins if unusable.
    try:
        path = Path(path_str)
        st = path.stat()
        if st.st_size > _MAX_UA_FILE_BYTES:
            raise ValueError("file too large")
        agents = _load_cached(str(path), st.st_mtime_ns)
        if agents:
            return agents
        log.warning("User-agent file has no valid entries; using built-in defaults")
    except (OSError, ValueError) as exc:
        log.warning("User-agent file unavailable (%s); using built-in defaults", exc.__class__.__name__)
    return FALLBACK_USER_AGENTS


def _accept_for(ua: str) -> str:
    if "Firefox/" in ua or "FxiOS/" in ua:
        return _ACCEPT_FIREFOX
    if "Chrome/" in ua or "CriOS/" in ua or "Edg/" in ua or "OPR/" in ua:
        return _ACCEPT_CHROMIUM
    return _ACCEPT_SAFARI


def build_headers(config: ExtractorConfig, rng: Optional[random.Random] = None) -> dict[str, str]:
    # Return extra HTTP headers for yt-dlp (``http_headers``).
    #
    # Without rotation only ``Accept-Language`` is overridden and yt-dlp keeps its
    # own default (and extractor-specific) User-Agent.
    headers = {"Accept-Language": config.accept_language}
    if config.rotate_user_agent:
        ua = (rng or random).choice(load_user_agents(config.user_agent_file))
        headers["User-Agent"] = ua
        headers["Accept"] = _accept_for(ua)
    return headers
