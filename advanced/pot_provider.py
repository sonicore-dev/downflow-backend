# PO Token provider integration (bgutil-ytdlp-pot-provider).
#
# Some platforms want a Proof-of-Origin token for most clients. The ``bgutil-ytdlp-pot-provider`` yt-dlp plugin
# (installed from requirements.txt) fetches tokens from a small HTTP server. This module only
#
# * tells the plugin where that server is (``DOWNFLOW_POT_URL``; default ``http://127.0.0.1:4416``),
# * pings it so the strategy planner can put PO-token routes first when the server is up, and
# * exposes the state for the admin dashboard.
#
# The provider server itself is started by ``start.sh`` / the Dockerfile. A PO token helps traffic look
# legitimate but does not unblock an IP that a platform has already flagged; pair it with a clean proxy.

from __future__ import annotations

import importlib.metadata
import json
import threading
import time
import urllib.request
from typing import Any

from .extractor_config import ExtractorConfig

__all__ = ["DEFAULT_URL", "alive", "extractor_args", "status", "plugin_version"]

DEFAULT_URL = "http://127.0.0.1:4416"
_lock = threading.Lock()
_cache: dict[str, Any] = {"t": 0.0, "ok": False, "detail": "not checked", "url": ""}


def base_url(config: ExtractorConfig) -> str:
    return (config.pot_url or DEFAULT_URL).rstrip("/")


def plugin_version() -> str | None:
    try:
        return importlib.metadata.version("bgutil-ytdlp-pot-provider")
    except importlib.metadata.PackageNotFoundError:
        return None


def alive(config: ExtractorConfig, max_age: float = 20.0) -> bool:
    # True when the plugin is installed and its HTTP server answers ``/ping`` (cached for ``max_age`` s).
    url = base_url(config)
    now = time.monotonic()
    with _lock:
        if _cache["url"] == url and now - _cache["t"] < max_age:
            return bool(_cache["ok"])
    ok, detail = False, ""
    if plugin_version() is None:
        detail = "plugin bgutil-ytdlp-pot-provider is not installed"
    else:
        try:
            with urllib.request.urlopen(url + "/ping", timeout=1.5) as r:   # noqa: S310 (operator-configured URL)
                body = r.read(2048).decode("utf-8", "ignore")
            ok = r.status == 200
            try:
                detail = "up, server " + str(json.loads(body).get("version", "?"))
            except ValueError:
                detail = "up"
        except Exception as exc:  # noqa: BLE001
            detail = f"server not reachable ({type(exc).__name__})"
    with _lock:
        _cache.update(t=now, ok=ok, detail=detail, url=url)
    return ok


def extractor_args(config: ExtractorConfig) -> dict[str, dict[str, list[str]]]:
    # Extra top-level yt-dlp ``extractor_args`` entries that point the plugin at the provider server.
    if config.pot_url:
        return {"youtubepot-bgutilhttp": {"base_url": [config.pot_url.rstrip("/")]}}
    return {}


def status(config: ExtractorConfig) -> dict[str, Any]:
    ok = alive(config, max_age=5.0)
    with _lock:
        return {"url": base_url(config), "plugin": plugin_version(), "alive": ok, "detail": _cache["detail"]}
