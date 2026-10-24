"""Proxy pool with health tracking, failover and sticky sessions.

Configure with environment variables:

* ``DOWNFLOW_PROXY``       -- one proxy URL (``http://user:pass@host:port``, ``socks5h://...``).
* ``DOWNFLOW_PROXY_LIST``  -- comma separated pool, or ``DOWNFLOW_PROXY_FILE`` (one per line).
* ``DOWNFLOW_PROXY_MODE``  -- ``sticky`` (default), ``round_robin``, ``random`` or ``weighted``.

Sticky sessions for rotating residential gateways
-------------------------------------------------
Put ``{sid}`` (deterministic per video + attempt) or ``{rand}`` (always new) in the proxy URL and it is
filled in for every request, for example::

    DOWNFLOW_PROXY=http://user-session-{sid}:pass@gate.example.com:7000

The same video always gets the same session id, so the IP that extracted a signed media URL is also the
IP that downloads it. A retry after a block gets a new id, i.e. a fresh exit IP.

Health + failover
-----------------
Every attempt reports its outcome. A proxy that got blocked by a site is parked for that site (exponential
cool-down), one that is unreachable is parked globally, and the pool prefers healthy entries. The proxy
that finally worked for a URL is remembered ("affinity") so the download reuses it. A proxy is never
starved: when everything is cooling, the one that recovers first is used.
"""

from __future__ import annotations

import hashlib
import itertools
import random
import secrets
import threading
import time
from typing import Iterable, Optional
from urllib.parse import urlsplit

from .extractor_config import ExtractorConfig

__all__ = ["pick_proxy", "pool", "redact", "report", "bind", "stats", "is_templated", "host_of"]

_ALLOWED = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
_MAX_COOLDOWN = 1800.0
_AFFINITY_MAX = 4096

_rr = itertools.count()
_lock = threading.RLock()
_health: dict[str, dict] = {}                       # proxy base key -> counters
_host_block: dict[tuple[str, str], list] = {}       # (proxy base key, site host) -> [fails, until]
_affinity: dict[str, tuple[str, float]] = {}        # url key -> (rendered proxy, expires)


def _valid(proxy: str) -> bool:
    try:
        return urlsplit(proxy).scheme.lower() in _ALLOWED
    except ValueError:
        return False


def is_templated(proxy: str) -> bool:
    return "{sid}" in proxy or "{rand}" in proxy


def host_of(url: str) -> str:
    """Registrable-ish site key: ``www.youtube.com`` -> ``youtube.com``."""
    try:
        h = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def redact(proxy: Optional[str]) -> str:
    """Host:port only, never credentials (safe for logs)."""
    if not proxy:
        return "none"
    try:
        p = urlsplit(proxy)
        return f"{p.scheme}://{p.hostname}:{p.port}" if p.port else f"{p.scheme}://{p.hostname}"
    except ValueError:
        return "invalid"


def _base(proxy: str) -> str:
    return redact(proxy)


def pool(config: ExtractorConfig) -> list[str]:
    items = ([config.proxy] if config.proxy else []) + list(config.proxy_list)
    seen, out = set(), []
    for p in items:
        if p and p not in seen and _valid(p):
            seen.add(p)
            out.append(p)
    return out


def render(template: str, key: str, attempt: int) -> str:
    if not is_templated(template):
        return template
    sid = hashlib.sha1(f"{key}|{attempt}".encode("utf-8", "ignore")).hexdigest()[:12]
    return template.replace("{sid}", sid).replace("{rand}", secrets.token_hex(6))


def _available(proxy: str, host: str, now: float) -> bool:
    b = _base(proxy)
    h = _health.get(b)
    if h and h["until"] > now:
        return False
    hb = _host_block.get((b, host))
    return not (hb and hb[1] > now)


def _recover_at(proxy: str, host: str) -> float:
    b = _base(proxy)
    until = _health.get(b, {}).get("until", 0.0)
    hb = _host_block.get((b, host))
    return max(until, hb[1] if hb else 0.0)


def pick_proxy(config: ExtractorConfig, key: Optional[str] = None, *, host: Optional[str] = None,
               exclude: Iterable[str] = (), attempt: int = 0) -> Optional[str]:
    """Return the proxy URL to use for ``key`` (normally the video URL), or None for a direct connection.

    ``attempt`` > 0 deliberately moves to another proxy / a new session id; ``exclude`` lists proxy URLs
    that already failed for this job.
    """
    proxies = pool(config)
    if not proxies:
        return None
    key = key or ""
    host = host or host_of(key)
    excluded = set(exclude)
    now = time.monotonic()
    with _lock:
        if key and attempt == 0:
            hit = _affinity.get(key)
            if hit and hit[1] > now and hit[0] not in excluded and any(_base(hit[0]) == _base(p) for p in proxies):
                return hit[0]
        usable = [p for p in proxies if is_templated(p) or p not in excluded]
        cands = [p for p in usable if _available(p, host, now)]
        if not cands:
            if usable:
                cands = [min(usable, key=lambda p: _recover_at(p, host))]   # never starve: soonest to recover
            elif config.proxy_direct_fallback:
                return None
            else:
                cands = proxies
        mode = config.proxy_mode
        if len(cands) == 1:
            chosen = cands[0]
        elif mode == "random":
            chosen = random.choice(cands)
        elif mode == "round_robin":
            chosen = cands[next(_rr) % len(cands)]
        elif mode == "weighted":
            weights = [1.0 / (1 + _health.get(_base(p), {}).get("fails", 0)) for p in cands]
            chosen = random.choices(cands, weights=weights, k=1)[0]
        else:  # sticky
            digest = hashlib.sha1(key.encode("utf-8", "ignore")).digest()
            chosen = cands[(int.from_bytes(digest[:4], "big") + attempt) % len(cands)]
        return render(chosen, key, attempt)


def report(proxy: Optional[str], host: str, outcome: str, config: Optional[ExtractorConfig] = None) -> None:
    """Record how ``proxy`` did: ``ok`` | ``blocked`` | ``rate_limited`` | ``dead``."""
    if not proxy:
        return
    base_cd = config.proxy_cooldown if config else 90.0
    b, now = _base(proxy), time.monotonic()
    with _lock:
        h = _health.setdefault(b, {"fails": 0, "ok": 0, "blocked": 0, "dead": 0, "until": 0.0})
        if outcome == "ok":
            h["ok"] += 1
            h["fails"] = 0
            h["until"] = 0.0
            _host_block.pop((b, host), None)
        elif outcome == "dead":
            h["dead"] += 1
            h["fails"] += 1
            h["until"] = now + min(base_cd * 2 ** (h["fails"] - 1), _MAX_COOLDOWN)
        elif outcome in ("blocked", "rate_limited"):
            h["blocked"] += 1
            if not is_templated(proxy):          # a templated gateway just needs a new session id
                hb = _host_block.setdefault((b, host), [0, 0.0])
                hb[0] += 1
                hb[1] = now + min(base_cd * 2 ** (hb[0] - 1), _MAX_COOLDOWN)


def bind(key: str, proxy: Optional[str], ttl: float = 7200.0) -> None:
    """Remember which proxy worked for ``key`` so the download uses the same exit IP."""
    if not proxy or not key:
        return
    now = time.monotonic()
    with _lock:
        if len(_affinity) >= _AFFINITY_MAX:
            for k in [k for k, v in _affinity.items() if v[1] <= now] or [min(_affinity, key=lambda k: _affinity[k][1])]:
                _affinity.pop(k, None)
        _affinity[key] = (proxy, now + ttl)


def stats(config: ExtractorConfig) -> list[dict]:
    """Admin view of pool health (no credentials)."""
    now = time.monotonic()
    out = []
    with _lock:
        for p in pool(config):
            h = _health.get(_base(p), {})
            blocks = {site: max(0, int(v[1] - now)) for (b, site), v in _host_block.items()
                      if b == _base(p) and v[1] > now}
            out.append({"proxy": redact(p), "templated": is_templated(p), "ok": h.get("ok", 0),
                        "blocked": h.get("blocked", 0), "dead": h.get("dead", 0),
                        "cooling_s": max(0, int(h.get("until", 0.0) - now)), "blocked_sites": blocks})
    return out
