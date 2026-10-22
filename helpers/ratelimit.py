"""Small in-memory sliding-window rate limiter for the public API (per client IP)."""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_hits: dict[tuple[str, str], deque] = defaultdict(deque)
LIMITS = {  # bucket -> (max requests, window seconds); override with DOWNFLOW_RL_<BUCKET>="30/60"
    "extract": (20, 60),
    "download": (20, 60),
}


def _limit(bucket: str) -> tuple[int, int]:
    raw = os.environ.get(f"DOWNFLOW_RL_{bucket.upper()}")
    if raw and "/" in raw:
        try:
            n, w = raw.split("/", 1)
            return max(1, int(n)), max(1, int(w))
        except ValueError:
            pass
    return LIMITS[bucket]


def check(bucket: str, ip: str) -> int:
    """Record a request. Returns 0 if allowed, else the seconds until the client may retry."""
    limit, window = _limit(bucket)
    now = time.monotonic()
    with _lock:
        q = _hits[(bucket, ip)]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return max(1, int(window - (now - q[0])) + 1)
        q.append(now)
        if len(_hits) > 5000:                       # keep memory bounded
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > window][:1000]:
                _hits.pop(k, None)
    return 0
