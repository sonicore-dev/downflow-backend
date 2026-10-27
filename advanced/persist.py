"""Restore saved state at start-up and flush it periodically (strategy memory, request counters).

Everything is best effort: if the database is down the server keeps working from memory and retries later.
"""

from __future__ import annotations

import logging
import threading
import time

from . import cookie_store, db, strategy_engine
from helpers import metrics

log = logging.getLogger("downflow.store")
_started = False
FLUSH_EVERY = 120.0


def restore() -> bool:
    try:
        db.get_db()
    except db.DbError as exc:
        log.warning("database unavailable at start-up (%s); will retry in the background", exc)
        return False
    cookie_store.load()
    try:
        strategy_engine.memory_restore(db.kv_get("strategy_winner"))
        metrics.restore_state(db.kv_get("metrics") or {})
        from helpers.update_check import get_update_data
        get_update_data()
    except db.DbError as exc:
        log.warning("restore: %s", exc)
    return True


def flush() -> bool:
    try:
        db.kv_set("strategy_winner", strategy_engine.memory_export())
        db.kv_set("metrics", metrics.export_state())
        return True
    except db.DbError:
        return False


def _loop(restored: bool) -> None:
    while True:
        time.sleep(FLUSH_EVERY if restored else 30)
        if not restored:                       # database was down at boot: retry
            try:
                restored = restore()
            except db.DbError:
                continue
        flush()


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    def boot():
        ok = restore()
        threading.Thread(target=_loop, args=(ok,), daemon=True, name="persist").start()
    threading.Thread(target=boot, daemon=True, name="persist-boot").start()
