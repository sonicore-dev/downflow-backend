"""Clean, multi-color console logging.

    21:04:07  INFO  engine    start https://...
Colors: terminals only. NO_COLOR=1 disables, DOWNFLOW_COLOR=1 forces.
DOWNFLOW_LOG_LEVEL=DEBUG|INFO|WARNING controls verbosity.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from collections import deque

__all__ = ["setup_logging", "get_logs", "clear_logs"]

R = "\033[0m"


def c(n: int, bold: bool = False) -> str:
    return f"\033[{'1;' if bold else ''}38;5;{n}m"


DIM, BOLD, UL = "\033[38;5;243m", "\033[1m", "\033[4m"
LEVEL = {  # badge background, text
    "DEBUG": ("\033[48;5;238m\033[38;5;250m", " DBG "),
    "INFO": ("\033[48;5;29m\033[38;5;255m", " INF "),
    "WARNING": ("\033[48;5;172m\033[38;5;16m", " WRN "),
    "ERROR": ("\033[48;5;160m\033[38;5;255m", " ERR "),
    "CRITICAL": ("\033[48;5;125m\033[38;5;255m", " CRT "),
}
MODULE = {"engine": 81, "api": 213, "stealth": 141, "download": 208, "retry": 179, "config": 109,
          "cookies": 180, "headers": 110, "status": 114, "access": 244, "error": 203, "downflow": 117}
TAG = {"start": 51, "try": 75, "ok": 84, "done": 46, "block": 214, "failed": 203, "send": 213,
       "fetch": 117, "direct": 208, "merge": 177, "queue": 221}
LEVEL_TEXT = {"WARNING": 221, "ERROR": 210, "CRITICAL": 205}

_URL = re.compile(r"https?://\S+")
_NUM = re.compile(r"(?<![\w/.])\d+(?:\.\d+)?\s?(?:s|ms|MB|KB|GB|%)(?![\w])")
_FRAC = re.compile(r"\b\d+/\d+\b")
_ACCESS = re.compile(r'"(GET|POST|PUT|DELETE|HEAD|OPTIONS) (\S+) (HTTP/[\d.]+)" (\d{3})')
_METHOD = {"GET": 84, "POST": 214, "PUT": 75, "DELETE": 203, "HEAD": 141, "OPTIONS": 141}


def _status_color(code: int) -> int:
    return 84 if code < 300 else 117 if code < 400 else 221 if code < 500 else 203


def _paint(msg: str, level: str) -> str:
    m = _ACCESS.search(msg)
    if m:
        verb, path, proto, code = m.groups()
        pretty = f'{c(_METHOD.get(verb, 250), True)}{verb}{R} {c(255)}{path}{R} {DIM}{proto}{R} ' \
                 f'{c(_status_color(int(code)), True)}{code}{R}'
        return f"{DIM}{msg[:m.start()].strip()}{R} {pretty}"
    base = c(LEVEL_TEXT.get(level, 252))
    parts = msg.split(" ", 1)
    out = msg
    if parts[0] in TAG:
        out = f"{c(TAG[parts[0]], True)}{parts[0]:<5}{R}{base}" + (" " + parts[1] if len(parts) > 1 else "")
    out = _URL.sub(lambda x: f"{c(75)}{UL}{x.group(0)}{R}{base}", out)
    out = _NUM.sub(lambda x: f"{c(221, True)}{x.group(0)}{R}{base}", out)
    out = _FRAC.sub(lambda x: f"{c(177)}{x.group(0)}{R}{base}", out)
    out = out.replace(" · ", f" {DIM}·{R}{base} ").replace("->", f"{c(203)}→{R}{base}")
    return f"{base}{out}{R}"


class _Formatter(logging.Formatter):
    def __init__(self, color: bool) -> None:
        super().__init__(datefmt="%H:%M:%S")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        name = record.name.replace("downflow.", "").replace("uvicorn.", "")
        level = record.levelname
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        t = self.formatTime(record, self.datefmt)
        badge_c, badge = LEVEL.get(level, LEVEL["INFO"])
        if not self.color:
            return f"{t} {badge.strip():<3} {name[:9]:<9} {msg}"
        return (f"{DIM}{t}{R} {badge_c}{badge}{R} {c(MODULE.get(name, 245), True)}{name[:9]:<9}{R} "
                f"{_paint(msg, level)}")


_BUF: deque = deque(maxlen=2000)
_LOCK = threading.Lock()
_SEQ = [0]
_QUIET = ("/api/v1/admin/logs", "/api/v1/admin/stats", "/api/v1/admin/tasks", "/api/v1/auth/me")


class _Ring(logging.Handler):
    """Keeps recent records in memory for the admin dashboard terminal."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + logging.Formatter().formatException(record.exc_info)
            with _LOCK:
                _SEQ[0] += 1
                _BUF.append({"id": _SEQ[0], "t": time.strftime("%H:%M:%S", time.localtime(record.created)),
                             "level": record.levelname,
                             "name": record.name.replace("downflow.", "").replace("uvicorn.", ""), "msg": msg})
        except Exception:  # noqa: BLE001
            pass


class _DropPolling(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not any(q in record.getMessage() for q in _QUIET)


def get_logs(after: int = 0, limit: int = 500) -> list[dict]:
    out: list[dict] = []
    with _LOCK:
        for r in reversed(_BUF):          # newest first: stop as soon as we reach what the client has
            if r["id"] <= after or len(out) >= limit:
                break
            out.append(r)
    out.reverse()
    return out


def clear_logs() -> None:
    with _LOCK:
        _BUF.clear()


def setup_logging(level: str | None = None) -> None:
    color = os.environ.get("DOWNFLOW_COLOR") == "1" or (sys.stderr.isatty() and "NO_COLOR" not in os.environ)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter(color))
    handler.addFilter(_DropPolling())
    ring = _Ring()
    ring.addFilter(_DropPolling())
    root = logging.getLogger()
    root.handlers = [handler, ring]
    root.setLevel((level or os.environ.get("DOWNFLOW_LOG_LEVEL", "INFO")).upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
