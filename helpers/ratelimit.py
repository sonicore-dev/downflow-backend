# Per-client rate limiting: O(1) token buckets, bounded memory, optional Redis for multi-worker setups.
#
# ``check(bucket, ip)`` returns 0 when the request is allowed, else the seconds the client should wait.
# Buckets (override with ``DOWNFLOW_RL_<BUCKET>="30/60"`` = 30 requests per 60 s):
#
# * ``extract`` / ``download``  -- the expensive routes (default 20 per 60 s)
# * ``api``                     -- every /api/ request, a flood guard (``DOWNFLOW_API_RATE``, default 300/60)
#
# A token bucket allows short bursts up to the limit and refills continuously, and needs two numbers per client
# instead of a list of timestamps. At most ``MAX_KEYS`` clients are tracked (least recently seen are dropped),
# so a flood of spoofed addresses cannot exhaust memory. With ``DOWNFLOW_REDIS_URL`` set, counters live in Redis
# (fixed window) so every worker and instance shares one limit; if Redis hiccups the local bucket takes over.

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict

from advanced.extractor_config import get_config

__all__ = ["check", "stats"]

log = logging.getLogger("downflow.limit")
MAX_KEYS = 200_000
_lock = threading.Lock()
_buckets: "OrderedDict[tuple[str, str], list[float]]" = OrderedDict()   # key -> [tokens, last_refill]
_denied = [0]
LIMITS = {"extract": (20, 60), "download": (20, 60)}


def _parse(raw: str | None, default: tuple[int, int]) -> tuple[int, int]:
    if raw and "/" in raw:
        try:
            n, w = raw.split("/", 1)
            return max(1, int(n)), max(1, int(w))
        except ValueError:
            pass
    return default


def _limit(bucket: str) -> tuple[int, int]:
    if bucket == "api":
        return _parse(os.environ.get("DOWNFLOW_RL_API") or get_config().api_rate, (300, 60))
    return _parse(os.environ.get(f"DOWNFLOW_RL_{bucket.upper()}"), LIMITS.get(bucket, (60, 60)))


def _local(bucket: str, key: str, limit: int, window: int) -> int:
    now = time.monotonic()
    rate = limit / window
    with _lock:
        k = (bucket, key)
        b = _buckets.get(k)
        if b is None:
            b = [float(limit), now]
            _buckets[k] = b
            if len(_buckets) > MAX_KEYS:
                for _ in range(MAX_KEYS // 20):          # drop the least recently seen 5 %
                    _buckets.popitem(last=False)
        else:
            b[0] = min(float(limit), b[0] + (now - b[1]) * rate)
            b[1] = now
            _buckets.move_to_end(k)
        if b[0] >= 1.0:
            b[0] -= 1.0
            return 0
        _denied[0] += 1
        return max(1, int((1.0 - b[0]) / rate) + 1)


def _shared(bucket: str, key: str, limit: int, window: int):
    from helpers.redis_store import get_redis
    r = get_redis()
    if r is None:
        return None
    try:
        slot = int(time.time() // window)
        name = f"df:rl:{bucket}:{key}:{slot}"
        pipe = r.pipeline()
        pipe.incr(name)
        pipe.expire(name, window + 1)
        count = pipe.execute()[0]
        if count <= limit:
            return 0
        _denied[0] += 1
        return max(1, int(window - (time.time() % window)) + 1)
    except Exception:  # noqa: BLE001 - never let the limiter take the API down
        return None


def check(bucket: str, ip: str) -> int:
    from helpers.netinfo import limit_key
    key = limit_key(ip)
    limit, window = _limit(bucket)
    if get_config().redis_url:
        got = _shared(bucket, key, limit, window)
        if got is not None:
            return got
    return _local(bucket, key, limit, window)


def stats() -> dict:
    with _lock:
        return {"tracked_clients": len(_buckets), "denied": _denied[0], "shared": bool(get_config().redis_url)}
