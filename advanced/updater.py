"""Keep yt-dlp current (YouTube breaks extractors every few weeks; a stale yt-dlp is the #1 cause of failures).

``DOWNFLOW_AUTO_UPDATE`` = ``off`` (default) | ``stable`` | ``nightly``. When enabled a background thread
checks every ``DOWNFLOW_UPDATE_INTERVAL`` seconds (and once shortly after start), runs
``pip install -U yt-dlp[default]`` (``--pre`` for nightly) and, if the version changed, drops the old yt-dlp
modules from memory so the next extraction uses the new code without a restart.
``update_now()`` is also exposed through the admin API.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from typing import Any

from .extractor_config import ExtractorConfig

__all__ = ["update_now", "start", "status", "current_version"]

log = logging.getLogger("downflow.update")
_lock = threading.Lock()
_state: dict[str, Any] = {"last_check": None, "last_result": "never run", "updated": False}


def current_version() -> str:
    try:
        import importlib.metadata as md
        return md.version("yt-dlp")
    except Exception:  # noqa: BLE001
        return "unknown"


class _file_lock:
    """Cross-process lock (multi-worker deployments): only one worker runs pip at a time."""

    def __enter__(self) -> bool:
        self.f = None
        try:
            import fcntl
            from .extractor_config import BASE_DIR
            (BASE_DIR / "data").mkdir(exist_ok=True)
            self.f = open(BASE_DIR / "data" / ".update.lock", "w")
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:              # Windows: no fcntl, single-process assumed
            return True
        except OSError:
            self.f.close()
            self.f = None
            return False
        return True

    def __exit__(self, *exc) -> None:
        if self.f:
            self.f.close()


def update_now(channel: str = "stable") -> dict[str, Any]:
    """pip-upgrade yt-dlp and reload it in this process. Returns a small status dict."""
    with _lock, _file_lock() as got:
        if not got:                      # another worker is already updating; do not run pip twice
            return {"ok": True, "before": current_version(), "after": current_version(), "changed": False,
                    "detail": "another worker is updating"}
        before = current_version()
        cmd = [sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check", "-q",
               "--break-system-packages", "yt-dlp[default]", "curl_cffi"]
        if channel == "nightly":
            cmd.insert(5, "--pre")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            ok = proc.returncode == 0
            detail = "" if ok else (proc.stderr or proc.stdout)[-300:]
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}"
        after = current_version()
        changed = ok and after != before
        if changed:
            for name in [m for m in sys.modules if m == "yt_dlp" or m.startswith(("yt_dlp.", "yt_dlp_plugins", "yt_dlp_ejs"))]:
                sys.modules.pop(name, None)
        _state.update(last_check=time.time(), updated=changed,
                      last_result=("updated" if changed else "up to date") if ok else f"failed: {detail}")
        log.info("yt-dlp %s -> %s (%s)", before, after, _state["last_result"])
        return {"ok": ok, "before": before, "after": after, "changed": changed, "detail": detail}


def status() -> dict[str, Any]:
    return {"version": current_version(), **_state}


def start(config: ExtractorConfig) -> None:
    """Start the background updater if enabled."""
    if config.auto_update not in ("stable", "nightly"):
        return

    def loop() -> None:
        time.sleep(30)
        while True:
            try:
                update_now(config.auto_update)
            except Exception:  # noqa: BLE001
                log.exception("auto-update failed")
            time.sleep(config.update_interval)

    threading.Thread(target=loop, daemon=True, name="ytdlp-updater").start()
