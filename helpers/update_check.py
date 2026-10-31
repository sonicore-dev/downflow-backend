# App-update data. Stored in the database (manage it from the admin console); data/updates-data.json is the fallback.

import json
import logging
import threading
import time
from typing import Any, Optional

from dir_config import UPDATES_DATA_FILE

log = logging.getLogger("downflow.updates")
_TTL = 60.0
_lock = threading.Lock()
_cache: dict[str, Any] = {"data": None, "at": 0.0}


def _file_data() -> dict:
    with open(UPDATES_DATA_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def _public(row: tuple) -> dict:
    return {"version_name": row[0], "version_code": int(row[1]), "force_update": bool(row[2])}


def _seed(db) -> None:
    if db.one("SELECT COUNT(*) FROM app_updates")[0][0] == 0:
        try:
            d = _file_data()
        except (OSError, ValueError):
            d = {"version_name": "1.0.0", "version_code": 1, "force_update": False}
        db.one("INSERT INTO app_updates (version_name, version_code, force_update, notes, active, created_at) VALUES (?,?,?,?,1,?)",
               (str(d.get("version_name", "1.0.0")), int(d.get("version_code", 1)), int(bool(d.get("force_update"))),
                "Imported from updates-data.json", int(time.time())))


def get_update_data() -> dict:
    # The active release. Same shape the app has always received; cached for a minute; never fails.
    now = time.monotonic()
    with _lock:
        if _cache["data"] is not None and now - _cache["at"] < _TTL:
            return dict(_cache["data"])
    try:
        from advanced import db
        _seed(db)
        rows = db.one("SELECT version_name, version_code, force_update FROM app_updates WHERE active = 1 "
                      "ORDER BY version_code DESC, id DESC LIMIT 1")
        data = _public(rows[0]) if rows else _file_data()
    except Exception as exc:  # noqa: BLE001
        log.warning("update data: database unavailable (%s); using last known value", exc.__class__.__name__)
        with _lock:
            if _cache["data"] is not None:
                return dict(_cache["data"])
        return _file_data()
    with _lock:
        _cache.update(data=data, at=now)
    return dict(data)


def invalidate() -> None:
    with _lock:
        _cache.update(data=None, at=0.0)


# admin operations
def list_releases() -> list[dict]:
    from advanced import db
    _seed(db)
    rows = db.one("SELECT id, version_name, version_code, force_update, notes, active, created_at FROM app_updates "
                  "ORDER BY version_code DESC, id DESC LIMIT 100")
    return [{"id": r[0], "version_name": r[1], "version_code": r[2], "force_update": bool(r[3]), "notes": r[4] or "",
             "active": bool(r[5]), "created_at": r[6]} for r in rows]


def publish(version_name: str, version_code: int, force_update: bool = False, notes: str = "", activate: bool = True) -> dict:
    from advanced import db
    name = (version_name or "").strip()
    if not name or len(name) > 40:
        raise ValueError("Version name is required (e.g. 1.2.0).")
    if not isinstance(version_code, int) or version_code < 1 or version_code > 2_000_000_000:
        raise ValueError("Version code must be a whole number ≥ 1.")
    _seed(db)
    stmts = []
    if activate:
        stmts.append(("UPDATE app_updates SET active = 0", ()))
    stmts.append(("INSERT INTO app_updates (version_name, version_code, force_update, notes, active, created_at) VALUES (?,?,?,?,?,?)",
                  (name, version_code, int(force_update), (notes or "")[:500], int(activate), int(time.time()))))
    db.run(stmts)
    invalidate()
    return get_update_data()


def activate(release_id: int, force_update: Optional[bool] = None) -> dict:
    from advanced import db
    if not db.one("SELECT 1 FROM app_updates WHERE id = ?", (release_id,)):
        raise KeyError(release_id)
    stmts = [("UPDATE app_updates SET active = 0", ()), ("UPDATE app_updates SET active = 1 WHERE id = ?", (release_id,))]
    if force_update is not None:
        stmts.append(("UPDATE app_updates SET force_update = ? WHERE id = ?", (int(force_update), release_id)))
    db.run(stmts)
    invalidate()
    return get_update_data()


def remove(release_id: int) -> None:
    from advanced import db
    db.one("DELETE FROM app_updates WHERE id = ? AND active = 0", (release_id,))
    invalidate()
