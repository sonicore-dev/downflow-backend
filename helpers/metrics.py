"""Thread-safe counters, latency histogram and a 60 s request-rate window for the admin dashboard."""

from __future__ import annotations

import threading
import time
from bisect import bisect_left
from collections import Counter

START = time.time()
_lock = threading.Lock()
_c: Counter = Counter()
_platforms: Counter = Counter()
_BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000)
_hist = [0] * (len(_BUCKETS_MS) + 1)
_rate = [0] * 60                  # requests per second, indexed by (epoch second % 60)
_rate_sec = [0]
_classes: Counter = Counter()


def inc(name: str, n: int = 1) -> None:
    with _lock:
        _c[name] += n


def gauge(name: str, delta: int) -> None:
    with _lock:
        _c[name] += delta


def platform(name: str) -> None:
    with _lock:
        _platforms[name] += 1


def observe(status: int, seconds: float, counted: bool = True) -> None:
    """Record one finished HTTP request."""
    now = int(time.time())
    with _lock:
        if counted:
            _hist[bisect_left(_BUCKETS_MS, seconds * 1000)] += 1
        _classes[f"{status // 100}xx"] += 1
        if _rate_sec[0] != now:                      # clear seconds we skipped
            for s in range(_rate_sec[0] + 1, min(now, _rate_sec[0] + 61)):
                _rate[s % 60] = 0
            _rate[now % 60] = 0
            _rate_sec[0] = now
        _rate[now % 60] += 1


def _quantile(q: float) -> float:
    total = sum(_hist)
    if not total:
        return 0.0
    target, acc = q * total, 0
    for i, n in enumerate(_hist):
        acc += n
        if acc >= target:
            return float(_BUCKETS_MS[i] if i < len(_BUCKETS_MS) else _BUCKETS_MS[-1] * 2)
    return float(_BUCKETS_MS[-1])


def traffic() -> dict:
    now = int(time.time())
    with _lock:
        last = [(_rate[s % 60] if now - 59 <= s <= now and s <= _rate_sec[0] else 0) for s in range(now - 59, now + 1)]
        return {"rps_now": last[-2] if len(last) > 1 else 0, "rps_peak_60s": max(last), "rps_series": last,
                "latency_ms": {"p50": _quantile(.5), "p95": _quantile(.95), "p99": _quantile(.99)},
                "classes": dict(_classes)}


def snapshot() -> dict:
    with _lock:
        base = {"uptime": int(time.time() - START), "counters": dict(_c), "platforms": dict(_platforms.most_common(8))}
    base["traffic"] = traffic()
    return base


def export_state() -> dict:
    with _lock:
        return {"c": dict(_c), "p": dict(_platforms), "k": dict(_classes), "h": list(_hist)}


def restore_state(data: dict) -> None:
    """Add counters saved before the last restart (called once at start-up)."""
    if not isinstance(data, dict):
        return
    with _lock:
        for k, v in (data.get("c") or {}).items():
            if isinstance(v, int) and not k.startswith(("inflight", "active", "running")):
                _c[k] += v
        for k, v in (data.get("p") or {}).items():
            if isinstance(v, int):
                _platforms[k] += v
        for k, v in (data.get("k") or {}).items():
            if isinstance(v, int):
                _classes[k] += v
        h = data.get("h")
        if isinstance(h, list) and len(h) == len(_hist):
            for i, v in enumerate(h):
                if isinstance(v, int):
                    _hist[i] += v
