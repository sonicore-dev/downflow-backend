"""Optional Redis backend so many workers / instances share task state.

Set ``DOWNFLOW_REDIS_URL=redis://host:6379/0`` (``pip install redis``). Without it everything stays in process
memory, which is right for a single worker. With Redis:

* task status + extraction results are shared, so ``/extract/status`` and ``/download/{task}`` work no matter
  which worker or instance receives the request,
* rate limits are global (see helpers/ratelimit.py).

Tasks expire by Redis TTL, so no cleanup job is needed. Calls are blocking, so the API runs them in a thread
(``manager.blocking`` is True).
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from advanced.error_handler import sanitize_text
from advanced.extractor_config import get_config
from helpers.status_manager import STAGE_LABELS, TaskStatus

__all__ = ["get_redis", "RedisStatusManager"]

log = logging.getLogger("downflow.redis")
_client: Any = None
_tried = False
_lock = threading.Lock()


def get_redis():
    """Return a connected redis client or None (cached; logs once why it is unavailable)."""
    global _client, _tried
    url = get_config().redis_url
    if not url:
        return None
    with _lock:
        if _tried:
            return _client
        _tried = True
        try:
            import redis  # type: ignore
            c = redis.Redis.from_url(url, socket_timeout=2.0, socket_connect_timeout=2.0, health_check_interval=30,
                                     decode_responses=True)
            c.ping()
            _client = c
            log.info("connected · shared state enabled")
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis unavailable (%s: %s) · falling back to in-process state", type(exc).__name__, exc)
            _client = None
        return _client


def _iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts is not None else None


class RedisStatusManager:
    """Same interface as StatusManager, state kept in Redis."""

    blocking = True

    def __init__(self, r, ttl: float, max_tasks: int) -> None:
        self.r, self.ttl, self.max_tasks = r, ttl, max_tasks
        self._mine: set[str] = set()
        self._lock = threading.Lock()

    # -- storage helpers
    def _k(self, tid: str) -> str:
        return f"df:task:{tid}"

    def _load(self, tid: str) -> Optional[dict]:
        raw = self.r.get(self._k(tid))
        return json.loads(raw) if raw else None

    def _save(self, d: dict) -> None:
        self.r.set(self._k(d["task_id"]), json.dumps(d, separators=(",", ":"), default=str), ex=int(self.ttl * 2))

    @staticmethod
    def _snap(d: dict) -> dict:
        return {"task_id": d["task_id"], "status": d["status"], "stage": d["stage"],
                "stage_label": STAGE_LABELS.get(d["stage"], d["stage"]), "progress": d["progress"],
                "message": d["message"], "created_at": _iso(d["created"]), "updated_at": _iso(d["updated"]),
                "completed_at": _iso(d["completed"]), "error": d["error"], "history": d["history"]}

    # -- lifecycle
    def create(self) -> dict[str, Any]:
        now = time.time()
        tid = secrets.token_urlsafe(16)
        msg = "Waiting for a free extraction slot"
        d = {"task_id": tid, "status": TaskStatus.QUEUED, "stage": "queued", "progress": None, "message": msg,
             "created": now, "updated": now, "completed": None, "error": None, "result": None,
             "history": [{"stage": "queued", "label": STAGE_LABELS["queued"], "message": msg, "at": _iso(now)}]}
        self._save(d)
        pipe = self.r.pipeline()
        pipe.lpush("df:recent", tid)
        pipe.ltrim("df:recent", 0, self.max_tasks - 1)
        pipe.execute()
        with self._lock:
            self._mine.add(tid)
        return self._snap(d)

    def update(self, task_id: str, *, status: Optional[str] = None, stage: Optional[str] = None,
               message: Optional[str] = None, progress: Any = ...) -> bool:
        d = self._load(task_id)
        if d is None or d["status"] in TaskStatus.TERMINAL:
            return False
        if status in (TaskStatus.QUEUED, TaskStatus.PROCESSING):
            d["status"] = status
        if message is not None:
            d["message"] = sanitize_text(message, 300)
        if progress is not ...:
            d["progress"] = float(progress) if isinstance(progress, (int, float)) and not isinstance(progress, bool) else None
        now = time.time()
        if stage is not None:
            d["stage"] = stage
            d["history"].append({"stage": stage, "label": STAGE_LABELS.get(stage, stage), "message": d["message"], "at": _iso(now)})
            del d["history"][:-20]
        d["updated"] = now
        self._save(d)
        return True

    def complete(self, task_id: str, result: dict[str, Any]) -> bool:
        d = self._load(task_id)
        if d is None or d["status"] in TaskStatus.TERMINAL:
            return False
        now = time.time()
        d.update(status=TaskStatus.COMPLETED, stage="completed", progress=1.0, message="Extraction completed",
                 result=result, error=None, updated=now, completed=now)
        d["history"].append({"stage": "completed", "label": STAGE_LABELS["completed"], "message": d["message"], "at": _iso(now)})
        self._save(d)
        with self._lock:
            self._mine.discard(task_id)
        return True

    def fail(self, task_id: str, error: dict[str, Any]) -> bool:
        clean = {"code": str(error.get("code", "internal_error"))[:60],
                 "message": sanitize_text(str(error.get("message", "Extraction failed")), 300),
                 "retryable": bool(error.get("retryable", False))}
        if error.get("retry_after"):
            clean["retry_after"] = int(error["retry_after"])
        d = self._load(task_id)
        if d is None or d["status"] in TaskStatus.TERMINAL:
            return False
        now = time.time()
        d.update(status=TaskStatus.FAILED, stage="failed", progress=None, message=clean["message"], error=clean,
                 result=None, updated=now, completed=now)
        d["history"].append({"stage": "failed", "label": STAGE_LABELS["failed"], "message": d["message"], "at": _iso(now)})
        self._save(d)
        with self._lock:
            self._mine.discard(task_id)
        return True

    # -- queries
    def get_status(self, task_id: str) -> Optional[dict[str, Any]]:
        d = self._load(task_id)
        return self._snap(d) if d else None

    def get_result(self, task_id: str):
        d = self._load(task_id)
        return (self._snap(d), d["result"]) if d else None

    def _recent(self, limit: int) -> list[dict]:
        ids = [i.decode() if isinstance(i, bytes) else i for i in self.r.lrange("df:recent", 0, limit - 1)]
        raws = self.r.mget([self._k(i) for i in ids]) if ids else []
        return [json.loads(x) for x in raws if x]

    def active_count(self) -> int:
        return sum(1 for d in self._recent(200) if d["status"] not in TaskStatus.TERMINAL)

    def __len__(self) -> int:
        return int(self.r.llen("df:recent"))

    def summary(self, limit: int = 50) -> list[dict[str, Any]]:
        out = []
        for d in self._recent(limit):
            r = d["result"] if isinstance(d.get("result"), dict) else {}
            out.append({"task_id": d["task_id"], "status": d["status"], "stage": d["stage"],
                        "created": _iso(d["created"]), "completed": _iso(d["completed"]), "title": r.get("title"),
                        "platform": (r.get("platform") or {}).get("key"), "url": r.get("input_url"),
                        "error": (d["error"] or {}).get("code") if d.get("error") else None})
        return out

    def fail_active(self, error: dict[str, Any]) -> int:
        with self._lock:
            ids = list(self._mine)
        return sum(1 for i in ids if self.fail(i, error))

    def cleanup(self) -> int:
        return 0                                   # Redis TTLs expire tasks
