"""Admin login: signed, long-lived cookie ("remember me" by default).

Credentials default to the values below and can be overridden with
DOWNFLOW_ADMIN_USER / DOWNFLOW_ADMIN_PASS. Set DOWNFLOW_SECRET to keep sessions
valid across servers; otherwise a random secret is stored in data/.secret.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

ADMIN_USER = os.environ.get("DOWNFLOW_ADMIN_USER", "saad")
ADMIN_PASS = os.environ.get("DOWNFLOW_ADMIN_PASS", "sk.saadu098")
COOKIE = "downflow_session"
MAX_AGE = 60 * 60 * 24 * 30  # 30 days

_SECRET_FILE = Path(__file__).resolve().parent.parent / "data" / ".secret"


def _secret() -> bytes:
    env = os.environ.get("DOWNFLOW_SECRET")
    if env:
        return env.encode()
    try:
        if _SECRET_FILE.exists():
            return _SECRET_FILE.read_bytes()
        value = secrets.token_hex(32).encode()
        _SECRET_FILE.write_bytes(value)
        os.chmod(_SECRET_FILE, 0o600)
        return value
    except OSError:
        return secrets.token_hex(32).encode()  # sessions reset on restart


_KEY = _secret()


def _sign(payload: str) -> str:
    return hmac.new(_KEY, payload.encode(), hashlib.sha256).hexdigest()


def make_token(user: str) -> str:
    payload = f"{user}.{int(time.time()) + MAX_AGE}"
    return f"{payload}.{_sign(payload)}"


def verify_token(token: Optional[str]) -> bool:
    try:
        user, exp, sig = (token or "").rsplit(".", 2)
        return hmac.compare_digest(sig, _sign(f"{user}.{exp}")) and int(exp) > time.time() and user == ADMIN_USER
    except ValueError:
        return False


def check_credentials(user: str, password: str) -> bool:
    ok_u = hmac.compare_digest(user.encode(), ADMIN_USER.encode())
    ok_p = hmac.compare_digest(password.encode(), ADMIN_PASS.encode())
    return ok_u and ok_p


# -- brute-force guard: 6 failures -> 5 minute lock per IP ---------------------
_fails: dict[str, list[float]] = {}


def client_ip(request: Request) -> str:
    from helpers.netinfo import client_ip as _ip
    return _ip(request.scope.get("headers", []), request.client.host if request.client else None)


def locked(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _fails.get(ip, []) if now - t < 300]
    _fails[ip] = hits
    return len(hits) >= 6


def record_fail(ip: str) -> None:
    _fails.setdefault(ip, []).append(time.time())


def clear_fails(ip: str) -> None:
    _fails.pop(ip, None)


def is_authed(request: Request) -> bool:
    return verify_token(request.cookies.get(COOKIE))


def require_admin(request: Request) -> None:
    if not is_authed(request):
        raise HTTPException(status_code=401, detail="Login required")
