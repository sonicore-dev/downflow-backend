# Persistent storage: Turso (libSQL over HTTPS) in production, a local SQLite file when Turso is not configured.
#
# Configure with environment variables (never commit the token):
#
#     DOWNFLOW_TURSO_URL    libsql://<db>-<org>.<region>.turso.io      (https:// also accepted)
#     DOWNFLOW_TURSO_TOKEN  the database auth token
#     DOWNFLOW_STORE_KEY    optional: passphrase that encrypts cookies at rest (default: derived from DOWNFLOW_SECRET / admin login)
#
# Without Turso the same code uses ``data/downflow.db`` (SQLite), which is fine for local use but is erased by a
# redeploy on hosts with an ephemeral disk (Render free). Tables are created automatically on first use.
#
# The Turso backend uses the libSQL "pipeline" HTTP API with the standard library only (no extra dependency).

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import ssl
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
    # The database could not be reached or rejected a statement.
    pass


# backends: both expose run(statements) -> list of row lists
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


def _why(exc: Any) -> str:
    text = re.sub(r"\s+", " ", str(exc)).strip()
    return f"{exc.__class__.__name__}: {text}"[:160] if text else exc.__class__.__name__


def _ssl_context() -> ssl.SSLContext:
    # Use the certifi bundle when present (some Windows/Python installs miss root certificates).
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


class TursoHTTP:
    kind = "turso"

    def __init__(self, url: str, token: str, timeout: float = 6.0) -> None:
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
            with urllib.request.urlopen(req, timeout=self.timeout, context=_ssl_context()) as r:   # noqa: S310 (operator-configured https URL)
                data = json.loads(r.read())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read()).get("error", "")
            except Exception:  # noqa: BLE001
                pass
            raise DbError(f"turso HTTP {exc.code} {detail}".strip()) from None
        except urllib.error.URLError as exc:
            raise DbError(f"turso unreachable: {_why(exc.reason)}") from None
        except Exception as exc:  # noqa: BLE001
            raise DbError(f"turso unreachable: {_why(exc)}") from None
        out: list[list[tuple]] = []
        for res in data.get("results", [])[:len(stmts)]:
            if res.get("type") == "error":
                raise DbError("turso: " + str(res.get("error", {}).get("message", "error")))
            result = res.get("response", {}).get("result", {})
            out.append([tuple(self._val(c) for c in row) for row in result.get("rows", [])])
        return out


# connection management
# Turso is the main store. If it cannot be reached, calls fail fast and a local SQLite file keeps the server working
# until Turso answers again (data written meanwhile stays in the local file only).
_lock = threading.Lock()
_primary: Any = None
_fallback: Any = None
_prepared: set = set()
_state: dict[str, Any] = {"ok": None, "error": "", "last_ok": 0.0, "down_until": 0.0, "fails": 0, "epoch": 0, "active": ""}


def _make_primary() -> Any:
    cfg = get_config()
    if cfg.turso_url:
        if not cfg.turso_token:
            log.warning("DOWNFLOW_TURSO_URL is set but DOWNFLOW_TURSO_TOKEN is empty; using the local SQLite file instead")
        else:
            return TursoHTTP(cfg.turso_url, cfg.turso_token)
    return LocalSQLite(BASE_DIR / "data" / "downflow.db")


def _local() -> Any:
    global _fallback
    with _lock:
        if _fallback is None:
            _fallback = LocalSQLite(BASE_DIR / "data" / "downflow.db")
        return _fallback


def _backend() -> Any:
    global _primary
    with _lock:
        if _primary is None:
            _primary = _make_primary()
        return _primary


def _prepare(db: Any) -> None:
    if id(db) not in _prepared:
        db.run([(s, ()) for s in SCHEMA])
        _prepared.add(id(db))
        log.info("check ok  database ready · %s · %s", db.kind, db.where)


def _switch(kind: str) -> None:
    if _state["active"] != kind:
        if _state["active"]:
            _state["epoch"] += 1
        _state["active"] = kind


def run(stmts: Sequence[Stmt]) -> list[list[tuple]]:
    primary = _backend()
    if primary.kind == "turso" and time.time() >= _state["down_until"]:
        try:
            _prepare(primary)
            out = primary.run(stmts)
            if _state["fails"]:
                log.info("check ok  database reconnected · turso · %s", primary.where)
            _state.update(ok=True, error="", last_ok=time.time(), fails=0, down_until=0.0)
            _switch("turso")
            return out
        except DbError as exc:
            if not str(exc).startswith("turso unreachable") and not str(exc).startswith("turso HTTP 5"):
                _state.update(ok=False, error=str(exc))
                raise
            _state["fails"] += 1
            wait = min(300, 15 * 2 ** min(_state["fails"] - 1, 5))
            _state.update(ok=False, error=str(exc), down_until=time.time() + wait)
            if _state["fails"] == 1 or _state["fails"] % 10 == 0:
                log.warning("database unreachable (%s); using the local file, retrying in %ds", exc, wait)
    db = primary if primary.kind != "turso" else _local()
    _prepare(db)
    _switch("sqlite" if primary.kind != "turso" else "fallback")
    try:
        return db.run(stmts)
    except DbError as exc:
        _state.update(ok=False, error=str(exc))
        raise


def one(sql: str, args: Sequence[Any] = ()) -> list[tuple]:
    return run([(sql, args)])[0]


def get_db() -> Any:
    # Make sure a backend is ready (Turso when reachable, else the local file). Never raises for network problems.
    one("SELECT 1")
    return _backend()


def epoch() -> int:
    # Changes whenever the active backend switches (callers reload their caches).
    return _state["epoch"]


def reset() -> None:
    # Forget the connection (tests / config change).
    global _primary, _fallback
    with _lock:
        _primary, _fallback = None, None
        _prepared.clear()
    _state.update(ok=None, error="", last_ok=0.0, down_until=0.0, fails=0, epoch=0, active="")


def status() -> dict[str, Any]:
    cfg = get_config()
    kind = "turso" if (cfg.turso_url and cfg.turso_token) else "sqlite"
    active = _state["active"]
    return {"backend": kind, "configured": kind == "turso", "ok": _state["ok"], "error": _state["error"],
            "last_ok": _state["last_ok"] or None,
            "persistent": kind == "turso" and active == "turso",
            "fallback": kind == "turso" and active == "fallback",
            "where": (cfg.turso_url or "").split("://", 1)[-1] if kind == "turso" else "data/downflow.db"}


# encryption at rest (cookies are credentials)
def _key() -> bytes:
    pw = os.environ.get("DOWNFLOW_STORE_KEY") or os.environ.get("DOWNFLOW_SECRET") or \
        (os.environ.get("DOWNFLOW_ADMIN_USER", "saad") + "\0" + os.environ.get("DOWNFLOW_ADMIN_PASS", "sk.saadu098"))
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), b"downflow-store-v1", 120_000)


_keys: Optional[tuple] = None


def _k() -> tuple:
    global _keys
    if _keys is None:
        base = _key()
        _keys = (hmac.new(base, b"enc", "sha256").digest(), hmac.new(base, b"mac", "sha256").digest(), base)
    return _keys


def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
    out, i = bytearray(), 0
    while len(out) < n:
        out += hmac.new(key, nonce + i.to_bytes(8, "big"), "sha256").digest()
        i += 1
    return bytes(out[:n])


def seal(text: str) -> str:
    # Authenticated encryption using only the standard library (standard library only, no extra packages).
    enc, mac, _ = _k()
    data = text.encode("utf-8")
    nonce = os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(data, _stream(enc, nonce, len(data))))
    tag = hmac.new(mac, nonce + ct, "sha256").digest()
    return "v2." + base64.urlsafe_b64encode(nonce + ct + tag).decode()


def unseal(token: str) -> str:
    enc, mac, base = _k()
    bad = DbError("cannot decrypt: DOWNFLOW_STORE_KEY / SECRET differs from the one used when this was saved")
    try:
        if token.startswith("v2."):
            raw = base64.urlsafe_b64decode(token[3:].encode())
            nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
            if not hmac.compare_digest(tag, hmac.new(mac, nonce + ct, "sha256").digest()):
                raise bad
            return bytes(a ^ b for a, b in zip(ct, _stream(enc, nonce, len(ct)))).decode("utf-8")
        from cryptography.fernet import Fernet            # legacy tokens from the first build
        return Fernet(base64.urlsafe_b64encode(base)).decrypt(token.encode()).decode("utf-8")
    except DbError:
        raise
    except Exception:  # noqa: BLE001
        raise bad from None


# key/value helpers (strategy memory, metrics, small settings)
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
