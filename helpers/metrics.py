"""Tiny thread-safe counters shown on the admin dashboard."""

from __future__ import annotations

import threading
import time
from collections import Counter

START = time.time()
_lock = threading.Lock()
_c: Counter = Counter()
_platforms: Counter = Counter()


def inc(name: str, n: int = 1) -> None:
    with _lock:
        _c[name] += n


def gauge(name: str, delta: int) -> None:
    with _lock:
        _c[name] += delta


def platform(name: str) -> None:
    with _lock:
        _platforms[name] += 1


def snapshot() -> dict:
    with _lock:
        return {"uptime": int(time.time() - START), "counters": dict(_c), "platforms": dict(_platforms.most_common(8))}
