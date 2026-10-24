"""Login + admin dashboard API (cookie-protected)."""

from __future__ import annotations

import dataclasses
import os
import platform
import shutil
import sys
import time
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import asyncio

from advanced import get_config
from advanced import cookie_manager
from advanced.stealth import impersonation_available
from api.version_prefix import API_PREFIX
from helpers import auth, metrics
from helpers.logger import clear_logs, get_logs
from helpers.status_manager import get_status_manager

router = APIRouter(prefix=API_PREFIX)
admin = APIRouter(prefix=f"{API_PREFIX}/admin", dependencies=[Depends(auth.require_admin)])


class Login(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
async def login(body: Login, request: Request):
    ip = auth.client_ip(request)
    if auth.locked(ip):
        return JSONResponse(status_code=429, content={"ok": False, "error": "Too many attempts. Try again in 5 minutes."})
    if not auth.check_credentials(body.username.strip(), body.password):
        auth.record_fail(ip)
        return JSONResponse(status_code=401, content={"ok": False, "error": "Wrong username or password."})
    auth.clear_fails(ip)
    resp = JSONResponse({"ok": True})
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(auth.COOKIE, auth.make_token(auth.ADMIN_USER), max_age=auth.MAX_AGE, httponly=True,
                    samesite="lax", secure=secure, path="/")
    return resp


@router.post("/auth/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@router.get("/auth/me")
async def me(request: Request):
    return {"authenticated": auth.is_authed(request), "user": auth.ADMIN_USER if auth.is_authed(request) else None}


_TOOLS: dict = {}     # cmd -> (expires_at, version); avoids spawning processes on every poll


def _tool_version(cmd: str, arg: str = "-version") -> Optional[str]:
    hit = _TOOLS.get(cmd)
    if hit and hit[0] > time.time():
        return hit[1]
    value = _probe_tool(cmd, arg)
    _TOOLS[cmd] = (time.time() + 120, value)
    return value


def _probe_tool(cmd: str, arg: str) -> Optional[str]:
    path = shutil.which(cmd)
    if not path:
        return None
    try:
        import subprocess
        out = subprocess.run([cmd, arg], capture_output=True, text=True, timeout=3).stdout.splitlines()
        return (out[0] if out else path)[:60]
    except Exception:  # noqa: BLE001
        return path


def _memory() -> Optional[dict]:
    try:
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
        total, avail = info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
        return {"total": total, "used": total - avail}
    except Exception:  # noqa: BLE001
        return None


@admin.get("/stats")
async def stats():
    try:
        from yt_dlp.version import __version__ as ytdlp
    except Exception:  # noqa: BLE001
        ytdlp = None
    mgr = get_status_manager()
    return {
        **metrics.snapshot(),
        "tasks_in_memory": len(mgr), "tasks_active": mgr.active_count(),
        "system": {"python": sys.version.split()[0], "platform": platform.platform(), "pid": os.getpid(),
                   "cpus": os.cpu_count(), "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
                   "memory": _memory()},
        "tools": {"yt_dlp": ytdlp, "ffmpeg": _tool_version("ffmpeg"), "node": _tool_version("node", "--version"),
                  "deno": _tool_version("deno", "--version"), "curl_cffi": impersonation_available()},
    }


@admin.get("/logs")
async def logs(after: int = 0, limit: int = 500):
    return {"logs": get_logs(after, min(limit, 1000))}


@admin.post("/logs/clear")
async def logs_clear():
    clear_logs()
    return {"ok": True}


@admin.get("/tasks")
async def tasks():
    return {"tasks": get_status_manager().summary(50)}


@admin.get("/config")
async def config():
    cfg = dataclasses.asdict(get_config())
    for key in ("proxy", "cookie_file", "cookies_from_browser", "browser_profile_dir", "yt_visitor_data"):
        if cfg.get(key):
            cfg[key] = "•••• set"
    for key in ("proxy_list", "yt_po_token"):
        if cfg.get(key):
            cfg[key] = [f"•••• {len(cfg[key])} set"]
    return cfg


@admin.get("/antiblock")
async def antiblock():
    """Live view of the anti-blocking layer: proxy health, PO-token provider, strategy memory, yt-dlp version."""
    from advanced import pot_provider, proxy_manager, strategy_engine, updater
    cfg = get_config()
    return {
        "proxies": proxy_manager.stats(cfg),
        "pot_provider": await asyncio.to_thread(pot_provider.status, cfg),
        "strategies": strategy_engine.memory_snapshot(),
        "ytdlp": updater.status(),
        "impersonation": impersonation_available(),
        "cookie_policy": cfg.cookie_policy,
        "race": {"width": cfg.race_width, "hedge_delay": cfg.hedge_delay},
    }


class UpdateBody(BaseModel):
    channel: str = "stable"


@admin.post("/update-ytdlp")
async def update_ytdlp(body: UpdateBody):
    from advanced import updater
    channel = body.channel if body.channel in ("stable", "nightly") else "stable"
    return await asyncio.to_thread(updater.update_now, channel)


class CookieText(BaseModel):
    text: str


@admin.get("/cookies")
async def cookies_status():
    """Cookie source, counts, expiry and the last validity check. Never returns cookie values."""
    return cookie_manager.cookie_status()


@admin.post("/cookies")
async def cookies_install(body: CookieText):
    """Replace the YouTube cookie file with pasted Netscape-format text. Takes effect immediately."""
    try:
        return {"ok": True, "status": await asyncio.to_thread(cookie_manager.install_cookie_text, body.text)}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"ok": False, "error": "That is not a valid Netscape cookie file."})


@admin.post("/cookies/check")
async def cookies_check():
    """Live test: does YouTube still treat the current cookies as signed in?"""
    return await asyncio.to_thread(cookie_manager.check_cookies_live)


_EXAMPLES = {
    "/api/v1/extract/": '{\n  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",\n  "format": "best",\n  "metadata_only": false\n}',
}


@admin.get("/routes")
async def routes(request: Request):
    out = []
    for path, ops in request.app.openapi().get("paths", {}).items():
        if path.startswith(API_PREFIX) and "/admin/" not in path and "/auth/" not in path:
            for method in ops:
                out.append({"method": method.upper(), "path": path, "body": _EXAMPLES.get(path, "")})
    return {"routes": sorted(out, key=lambda x: (x["path"], x["method"]))}
