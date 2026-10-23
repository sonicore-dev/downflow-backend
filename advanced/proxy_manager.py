"""Optional proxy support (single endpoint or a rotating pool).

Configure with environment variables:

* ``DOWNFLOW_PROXY``       -- one proxy URL (``http://user:pass@host:port``, ``socks5h://...``).
* ``DOWNFLOW_PROXY_LIST``  -- comma separated pool, or ``DOWNFLOW_PROXY_FILE`` (one per line).
* ``DOWNFLOW_PROXY_MODE``  -- ``sticky`` (default), ``round_robin`` or ``random``.

Why "sticky": YouTube media URLs are bound to the IP that requested them, so the proxy that
extracted a video must also download it. Sticky mode maps the same video URL to the same pool
entry every time. For a residential *gateway* that rotates by itself, use a single
``DOWNFLOW_PROXY`` with the provider's sticky-session option instead.
"""

from __future__ import annotations

import hashlib
import itertools
import random
import threading
from typing import Optional
from urllib.parse import urlsplit

from .extractor_config import ExtractorConfig

__all__ = ["pick_proxy", "pool", "redact"]

_ALLOWED = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
_rr = itertools.count()
_lock = threading.Lock()


def _valid(proxy: str) -> bool:
    try:
        return urlsplit(proxy).scheme.lower() in _ALLOWED
    except ValueError:
        return False


def pool(config: ExtractorConfig) -> list[str]:
    items = ([config.proxy] if config.proxy else []) + list(config.proxy_list)
    seen, out = set(), []
    for p in items:
        if p and p not in seen and _valid(p):
            seen.add(p)
            out.append(p)
    return out


def pick_proxy(config: ExtractorConfig, key: Optional[str] = None) -> Optional[str]:
    """Return the proxy URL to use for ``key`` (normally the video URL), or None."""
    proxies = pool(config)
    if not proxies:
        return None
    if len(proxies) == 1:
        return proxies[0]
    if config.proxy_mode == "random":
        return random.choice(proxies)
    if config.proxy_mode == "round_robin":
        with _lock:
            return proxies[next(_rr) % len(proxies)]
    digest = hashlib.sha1((key or "").encode("utf-8", "ignore")).digest()
    return proxies[int.from_bytes(digest[:4], "big") % len(proxies)]


def redact(proxy: Optional[str]) -> str:
    """Host:port only, never credentials (safe for logs)."""
    if not proxy:
        return "none"
    try:
        p = urlsplit(proxy)
        return f"{p.scheme}://{p.hostname}:{p.port}" if p.port else f"{p.scheme}://{p.hostname}"
    except ValueError:
        return "invalid"
