"""Thread-safe, in-memory tracking of extraction tasks.

Each extraction gets a unique ``task_id`` the frontend can poll. Progress is
reported by *stage*; ``progress`` stays ``None`` because yt-dlp gives no
reliable percentage while extracting metadata, and numbers are never invented.

NOTE: state lives in this process only. With several workers or instances (for
example ``uvicorn --workers 4`` or multiple replicas) a status request can land
on a process that never saw the task and returns 404. Multi-worker deployments
need shared storage such as Redis behind the same interface.
"""

from __future__ import annotations

import copy
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from advanced.error_handler import sanitize_text
from advanced.extractor_config import get_config

__all__ = ["StatusManager", "TaskStatus", "STAGE_LABELS", "get_status_manager"]


class TaskStatus:
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

    TERMINAL = frozenset({COMPLETED, FAILED})


STAGE_LABELS: dict[str, str] = {
    "queued": "Queued",
    "validating_url": "Validating URL",
    "initializing_extractor": "Connecting to the extractor",
    "extracting_metadata": "Extracting metadata",
    "retrying": "Retrying after a temporary problem",
    "resolving_formats": "Resolving available formats",
    "completed": "Completed",
    "failed": "Failed",
}

_MAX_HISTORY = 20
_CLEANUP_INTERVAL = 30.0
_UNSET: Any = object()


def _iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts is not None else None


class _Task:
    __slots__ = ("task_id", "status", "stage", "progress", "message", "created", "updated",
                 "completed", "error", "result", "history")

    def __init__(self, task_id: str, now: float) -> None:
        self.task_id = task_id
        self.status = TaskStatus.QUEUED
        self.stage = "queued"
        self.progress: Optional[float] = None
        self.message = "Waiting for a free extraction slot"
        self.created = now
        self.updated = now
        self.completed: Optional[float] = None
        self.error: Optional[dict[str, Any]] = None
        self.result: Optional[dict[str, Any]] = None
        self.history: list[dict[str, Any]] = [{"stage": "queued", "label": STAGE_LABELS["queued"],
                                               "message": self.message, "at": _iso(now)}]

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, self.stage),
            "progress": self.progress,
            "message": self.message,
            "created_at": _iso(self.created),
            "updated_at": _iso(self.updated),
            "completed_at": _iso(self.completed),
            "error": copy.deepcopy(self.error),
            "history": copy.deepcopy(self.history),
        }


class StatusManager:
    """Registry of extraction tasks with expiry and a size cap."""

    def __init__(self, ttl: float = 900.0, max_tasks: int = 500) -> None:
        self.ttl = ttl
        self.max_tasks = max_tasks
        self._tasks: dict[str, _Task] = {}
        self._lock = threading.RLock()
        self._last_cleanup = time.monotonic()

    # -- lifecycle ---------------------------------------------------------
    def create(self) -> dict[str, Any]:
        with self._lock:
            self._maybe_cleanup(force=len(self._tasks) >= self.max_tasks)
            task_id = secrets.token_urlsafe(16)
            while task_id in self._tasks:  # astronomically unlikely
                task_id = secrets.token_urlsafe(16)
            task = _Task(task_id, time.time())
            self._tasks[task_id] = task
            return task.snapshot()

    def update(self, task_id: str, *, status: Optional[str] = None, stage: Optional[str] = None,
               message: Optional[str] = None, progress: Any = _UNSET) -> bool:
        """Update a non-finished task. Returns False if unknown or already finished."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status in TaskStatus.TERMINAL:
                return False
            if status in (TaskStatus.QUEUED, TaskStatus.PROCESSING):
                task.status = status
            if message is not None:
                task.message = sanitize_text(message, 300)
            if progress is not _UNSET:
                task.progress = float(progress) if isinstance(progress, (int, float)) and not isinstance(progress, bool) else None
            if stage is not None and stage != task.stage:
                task.stage = stage
            now = time.time()
            task.updated = now
            if stage is not None:
                task.history.append({"stage": task.stage, "label": STAGE_LABELS.get(task.stage, task.stage),
                                     "message": task.message, "at": _iso(now)})
                del task.history[:-_MAX_HISTORY]
            return True

    def complete(self, task_id: str, result: dict[str, Any]) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status in TaskStatus.TERMINAL:
                return False
            now = time.time()
            task.status, task.stage, task.progress = TaskStatus.COMPLETED, "completed", 1.0
            task.message = "Extraction completed"
            task.result, task.error = result, None
            task.updated = task.completed = now
            task.history.append({"stage": "completed", "label": STAGE_LABELS["completed"],
                                 "message": task.message, "at": _iso(now)})
            return True

    def fail(self, task_id: str, error: dict[str, Any]) -> bool:
        """Mark failed with a sanitised error (only known keys are kept)."""
        clean = {
            "code": str(error.get("code", "internal_error"))[:60],
            "message": sanitize_text(str(error.get("message", "Extraction failed")), 300),
            "retryable": bool(error.get("retryable", False)),
        }
        if error.get("retry_after"):
            clean["retry_after"] = int(error["retry_after"])
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status in TaskStatus.TERMINAL:
                return False
            now = time.time()
            task.status, task.stage, task.progress = TaskStatus.FAILED, "failed", None
            task.message = clean["message"]
            task.error, task.result = clean, None
            task.updated = task.completed = now
            task.history.append({"stage": "failed", "label": STAGE_LABELS["failed"],
                                 "message": task.message, "at": _iso(now)})
            return True

    # -- queries -----------------------------------------------------------
    def get_status(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            self._maybe_cleanup()
            task = self._tasks.get(task_id)
            return task.snapshot() if task else None

    def get_result(self, task_id: str) -> Optional[tuple[dict[str, Any], Optional[dict[str, Any]]]]:
        """Return ``(status_snapshot, result)`` or None if unknown/expired."""
        with self._lock:
            self._maybe_cleanup()
            task = self._tasks.get(task_id)
            if task is None:
                return None
            return task.snapshot(), task.result

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.status not in TaskStatus.TERMINAL)

    def __len__(self) -> int:
        with self._lock:
            return len(self._tasks)

    def summary(self, limit: int = 50) -> list[dict[str, Any]]:
        """Recent tasks (newest first) without their result payloads."""
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda t: t.created, reverse=True)[:limit]
            out = []
            for t in tasks:
                r = t.result if isinstance(t.result, dict) else {}
                out.append({"task_id": t.task_id, "status": t.status, "stage": t.stage,
                            "created": _iso(t.created), "completed": _iso(t.completed),
                            "title": r.get("title"), "platform": (r.get("platform") or {}).get("key"),
                            "url": r.get("input_url"), "error": (t.error or {}).get("code") if t.error else None})
            return out

    # -- housekeeping ------------------------------------------------------
    def fail_active(self, error: dict[str, Any]) -> int:
        """Fail every unfinished task (used on shutdown)."""
        with self._lock:
            ids = [t.task_id for t in self._tasks.values() if t.status not in TaskStatus.TERMINAL]
        return sum(1 for i in ids if self.fail(i, error))

    def cleanup(self) -> int:
        """Drop expired tasks, then oldest finished tasks beyond ``max_tasks``."""
        with self._lock:
            now = time.time()
            removed = 0
            for tid, t in list(self._tasks.items()):
                finished_expired = t.completed is not None and now - t.completed > self.ttl
                abandoned = t.completed is None and now - t.created > self.ttl * 2
                if finished_expired or abandoned:
                    del self._tasks[tid]
                    removed += 1
            excess = len(self._tasks) - self.max_tasks
            if excess > 0:
                finished = sorted((t for t in self._tasks.values() if t.completed is not None),
                                  key=lambda t: t.completed)
                for t in finished[:excess]:
                    del self._tasks[t.task_id]
                    removed += 1
            self._last_cleanup = time.monotonic()
            return removed

    def _maybe_cleanup(self, force: bool = False) -> None:
        if force or time.monotonic() - self._last_cleanup >= _CLEANUP_INTERVAL:
            self.cleanup()


_manager: Optional[StatusManager] = None
_manager_lock = threading.Lock()


def get_status_manager():
    """The process-wide task registry: Redis-backed when DOWNFLOW_REDIS_URL works, else in memory."""
    global _manager
    with _manager_lock:
        if _manager is None:
            cfg = get_config()
            if cfg.redis_url:
                from helpers.redis_store import RedisStatusManager, get_redis
                r = get_redis()
                if r is not None:
                    _manager = RedisStatusManager(r, cfg.task_ttl, cfg.max_tasks)
            if _manager is None:
                _manager = StatusManager(ttl=cfg.task_ttl, max_tasks=cfg.max_tasks)
        return _manager
