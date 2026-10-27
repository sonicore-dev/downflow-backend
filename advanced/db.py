"""Persistent storage: Turso (libSQL over HTTPS) in production, a local SQLite file when Turso is not configured.

Configure with environment variables (never commit the token):

    DOWNFLOW_TURSO_URL    libsql://<db>-<org>.<region>.turso.io      (https:// also accepted)
    DOWNFLOW_TURSO_TOKEN  the database auth token
    DOWNFLOW_STORE_KEY    optional: passphrase that encrypts cookies at rest (default: derived from DOWNFLOW_SECRET / admin login)

Without Turso the same code uses ``data/downflow.db`` (SQLite), which is fine for local use and Termux but is erased by a
redeploy on hosts with an ephemeral disk (Render free). Tables are created automatically on first use.

The Turso backend uses the libSQL "pipeline" HTTP API with the standard library only (no extra dependency).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .extractor_config import BASE_DIR, get_config

__all__ = ["DbError", "get_db", "reset", "status", "seal", "unseal", "kv_get", "kv_set", "SCHEMA"]

log = logging.getLogger("downflow.store")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS cookies (
        domain TEXT PRIMARY KEY, label TEXT, aliases TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
        data TEXT, count INTEGER DEFAULT 0, logged_in INTEGER DEFAULT 0, expires_at INTEGER,
        created_at INTEGER, updated_at INTEGER, check_valid INTEGER, check_at INTEGER, check_detail TEXT, test_url TEXT)""",
    "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, updated_at INTEGER)",
    """CREATE TABLE IF NOT EXISTS app_updates (
        id INTEGER PRIMARY KEY AUTOINCREMENT, version_name TEXT NOT NULL, version_code INTEGER NOT NULL,
        force_update INTEGER DEFAULT 0, notes TEXT DEFAULT '', extra TEXT DEFAULT '{}', active INTEGER DEFAULT 0, created_at INTEGER)""",
]


class DbError(RuntimeError):
    """The database could not be reached or rejected a statement."""


# --------------------------------------------------------------------------- #
# backends: both expose run(statements) -> list of row lists
# --------------------------------------------------------------------------- #
Stmt = tuple[str, Sequence[Any]]


class LocalSQLite:
    kind = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.where = str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)

    def run(self, stmts: Sequence[Stmt]) -> list[list[tuple]]:
        out: list[list[tuple]] = []
        with self._lock:
            try:
                for sql, args in stmts:
                    cur = self._conn.execute(sql, tuple(args))
                    out.append(cur.fetchall() if cur.description else [])
            except sqlite3.Error as exc:
                raise DbError(f"sqlite: {exc}") from exc
        return out


class TursoHTTP:
    kind = "turso"

    def __init__(self, url: str, token: str, timeout: float = 10.0) -> None:
        url = url.strip().rstrip("/")
        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]
        elif url.startswith("wss://"):
            url = "https://" + url[len("wss://"):]
        self.base, self.token, self.timeout = url, token.strip(), timeout
        self.where = url.split("://", 1)[-1]

    @staticmethod
    def _arg(v: Any) -> dict:
        if v is None:
            return {"type": "null"}
        if isinstance(v, bool):
            return {"type": "integer", "value": str(int(v))}
        if isinstance(v, int):
            return {"type": "integer", "value": str(v)}
        if isinstance(v, float):
            return {"type": "float", "value": v}
        if isinstance(v, (bytes, bytearray)):
            return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode()}
        return {"type": "text", "value": str(v)}

    @staticmethod
    def _val(c: dict) -> Any:
        t = c.get("type")
        if t == "null":
            return None
        if t == "integer":
            return int(c["value"])
        if t == "float":
            return float(c["value"])
        if t == "blob":
            return base64.b64decode(c.get("base64", ""))
        return c.get("value")

    def run(self, stmts: Sequence[Stmt]) -> list[list[tuple]]:
        reqs = [{"type": "execute", "stmt": {"sql": s, "args": [self._arg(a) for a in args]}} for s, args in stmts]
        reqs.append({"type": "close"})
        body = json.dumps({"requests": reqs}).encode()
        req = urllib.request.Request(self.base + "/v2/pipeline", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                                              "User-Agent": "downflow-backend"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:   # noqa: S310 (operator-configured https URL)
                data = json.loads(r.read())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read()).get("error", "")
            except Exception:  # noqa: BLE001
                pass
            raise DbError(f"turso HTTP {exc.code} {detail}".strip()) from None
        except Exception as exc:  # noqa: BLE001
            raise DbError(f"turso unreachable ({exc.__class__.__name__})") from None
        out: list[list[tuple]] = []
        for res in data.get("results", [])[:len(stmts)]:
            if res.get("type") == "error":
                raise DbError("turso: " + str(res.get("error", {}).get("message", "error")))
            result = res.get("response", {}).get("result", {})
            out.append([tuple(self._val(c) for c in row) for row in result.get("rows", [])])
        return out


# --------------------------------------------------------------------------- #
# connection management
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_db: Any = None
_ready = False
_state: dict[str, Any] = {"ok": None, "error": "", "last_ok": 0.0}


def _make() -> Any:
    cfg = get_config()
    if cfg.turso_url:
        if not cfg.turso_token:
            log.warning("DOWNFLOW_TURSO_URL is set but DOWNFLOW_TURSO_TOKEN is empty; using the local SQLite file instead")
        else:
            return TursoHTTP(cfg.turso_url, cfg.turso_token)
    return LocalSQLite(BASE_DIR / "data" / "downflow.db")


def get_db() -> Any:
    """Return the backend (creating tables once). Raises DbError if Turso cannot be reached."""
    global _db, _ready
    with _lock:
        if _db is None:
            _db = _make()
        db = _db
    if not _ready:
        try:
            db.run([(s, ()) for s in SCHEMA])
            _ready = True
            log.info("check ok  database ready · %s · %s", db.kind, db.where)
        except DbError as exc:
            _state.update(ok=False, error=str(exc))
            raise
    return db


def run(stmts: Sequence[Stmt]) -> list[list[tuple]]:
    """Execute statements; keeps simple health state for the admin dashboard."""
    try:
        out = get_db().run(stmts)
        _state.update(ok=True, error="", last_ok=time.time())
        return out
    except DbError as exc:
        _state.update(ok=False, error=str(exc))
        raise


def one(sql: str, args: Sequence[Any] = ()) -> list[tuple]:
    return run([(sql, args)])[0]


def reset() -> None:
    """Forget the connection (tests / config change)."""
    global _db, _ready
    with _lock:
        _db, _ready = None, False
    _state.update(ok=None, error="", last_ok=0.0)


def status() -> dict[str, Any]:
    cfg = get_config()
    kind = "turso" if (cfg.turso_url and cfg.turso_token) else "sqlite"
    return {"backend": kind, "configured": kind == "turso", "ok": _state["ok"], "error": _state["error"],
            "last_ok": _state["last_ok"] or None,
            "persistent": kind == "turso",
            "where": (cfg.turso_url or "").split("://", 1)[-1] if kind == "turso" else "data/downflow.db"}


# --------------------------------------------------------------------------- #
# encryption at rest (cookies are credentials)
# --------------------------------------------------------------------------- #
_fernet: Any = None


def _key() -> bytes:
    pw = os.environ.get("DOWNFLOW_STORE_KEY") or os.environ.get("DOWNFLOW_SECRET") or \
        (os.environ.get("DOWNFLOW_ADMIN_USER", "saad") + "\0" + os.environ.get("DOWNFLOW_ADMIN_PASS", "sk.saadu098"))
    return base64.urlsafe_b64encode(hashlib.pbkdf2_hmac("sha256", pw.encode(), b"downflow-store-v1", 120_000))


def _f():
    global _fernet
    if _fernet is None:
        from cryptography.fernet import Fernet
        _fernet = Fernet(_key())
    return _fernet


def seal(text: str) -> str:
    return _f().encrypt(text.encode("utf-8")).decode()


def unseal(token: str) -> str:
    from cryptography.fernet import InvalidToken
    try:
        return _f().decrypt(token.encode()).decode("utf-8")
    except InvalidToken:
        raise DbError("cannot decrypt: DOWNFLOW_STORE_KEY / SECRET changed since this was saved") from None


# --------------------------------------------------------------------------- #
# key/value helpers (strategy memory, metrics, small settings)
# --------------------------------------------------------------------------- #
def kv_get(key: str) -> Optional[Any]:
    rows = one("SELECT v FROM kv WHERE k = ?", (key,))
    if not rows:
        return None
    try:
        return json.loads(rows[0][0])
    except (TypeError, ValueError):
        return None


def kv_set(key: str, value: Any) -> None:
    one("INSERT INTO kv (k, v, updated_at) VALUES (?, ?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v, updated_at = excluded.updated_at",
        (key, json.dumps(value, separators=(",", ":")), int(time.time())))
