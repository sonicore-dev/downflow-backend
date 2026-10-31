# Restore saved state at start-up and flush it periodically (strategy memory, request counters).
#
# Everything is best effort: if the database is down the server keeps working from memory and retries later.

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
        cookie_store.load()
        strategy_engine.memory_restore(db.kv_get("strategy_winner"))
        metrics.restore_state(db.kv_get("metrics") or {})
        from helpers.update_check import invalidate, get_update_data
        invalidate()
        get_update_data()
    except db.DbError as exc:
        log.warning("restore: %s", exc)
        return False
    return True


def flush() -> bool:
    try:
        db.kv_set("strategy_winner", strategy_engine.memory_export())
        db.kv_set("metrics", metrics.export_state())
        return True
    except db.DbError:
        return False


def _loop() -> None:
    seen = db.epoch()
    tick = 0
    while True:
        time.sleep(30)
        tick += 1
        try:
            db.one("SELECT 1")
            if db.epoch() != seen:
                seen = db.epoch()
                log.info("check ok  database changed (%s); reloading saved data", db.status()["persistent"] and "turso" or "local file")
                restore()
            if tick % 4 == 0:
                flush()
        except Exception as exc:  # noqa: BLE001
            log.warning("persist: %s", exc)


def start() -> None:
    global _started
    if _started:
        return
    _started = True

    def boot():
        try:
            restore()
        except Exception as exc:  # noqa: BLE001
            log.warning("restore failed: %s", exc)
        _loop()

    threading.Thread(target=boot, daemon=True, name="persist").start()
