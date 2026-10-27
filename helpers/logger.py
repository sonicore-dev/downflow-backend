"""Structured, colour-coded logging. Every colour means something (full legend in README "Reading the logs").

    14:03:22.418  INF  engine     a1b2c3  start https://youtu.be/...
    time          level component request  message

* Level badge ........ DBG grey · INF green · WRN amber · ERR red · CRT magenta
* Component .......... one colour per subsystem (access, api, engine, strategy, proxy, instant, download, ...)
* Request id ......... the same 6 characters on every line produced while serving one request (X-Request-ID)
* Message tags ....... start/try/ok/done/block/fail/retry/hedge/send/shed/limit ... each its own colour
* HTTP access lines .. method colour, status by class (2xx green, 3xx cyan, 4xx amber, 5xx red) and latency by
                       speed (fast green, normal white, slow amber, very slow red)

Environment
    DOWNFLOW_LOG_LEVEL   DEBUG | INFO (default) | WARNING | ERROR
    DOWNFLOW_LOG_FORMAT  pretty (default) | plain (no colour) | json (one JSON object per line, for log shippers)
    DOWNFLOW_COLOR       1 forces colour, 0 disables; NO_COLOR is honoured; otherwise colour only on a terminal
    DOWNFLOW_LOG_BUFFER  lines kept for the admin terminal (default 3000)

Floods are tamed: identical consecutive lines are collapsed ("repeated N times"), and the access log switches to
failures + slow requests + a periodic summary when traffic exceeds DOWNFLOW_ACCESS_LOG_RPS requests per second.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import threading
import time
from collections import deque

__all__ = ["setup_logging", "get_logs", "clear_logs", "request_id", "LEGEND"]

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("downflow_request_id", default="")

R = "\033[0m"


def c(n: int, bold: bool = False) -> str:
    return f"\033[{'1;' if bold else ''}38;5;{n}m"


DIM, BOLD, UL = "\033[38;5;243m", "\033[1m", "\033[4m"

LEVEL = {  # badge colours: background, text
    "DEBUG": ("\033[48;5;238m\033[38;5;250m", "DBG"),
    "INFO": ("\033[48;5;29m\033[38;5;255m", "INF"),
    "WARNING": ("\033[48;5;172m\033[38;5;16m", "WRN"),
    "ERROR": ("\033[48;5;160m\033[38;5;255m", "ERR"),
    "CRITICAL": ("\033[48;5;125m\033[38;5;255m", "CRT"),
}
# component -> 256-colour code (the admin terminal uses the same hues; keep in sync with web/admin.html)
COMPONENT = {
    "access": 75, "api": 213, "engine": 81, "strategy": 141, "stealth": 141, "proxy": 208, "instant": 191,
    "download": 214, "retry": 179, "limit": 221, "guard": 203, "cookies": 180, "doctor": 80, "update": 114,
    "store": 80, "persist": 80, "updates": 114, "redis": 167, "config": 109, "headers": 110, "status": 114, "error": 203, "downflow": 117, "server": 117,
}
# message tags (first word) -> colour
TAG = {
    "start": 51, "try": 75, "ok": 84, "done": 46, "block": 214, "fail": 203, "failed": 203, "send": 213,
    "fetch": 117, "direct": 208, "merge": 177, "queue": 221, "hedge": 141, "retry": 179, "shed": 196,
    "limit": 220, "giving": 203, "proxy": 208, "check": 80, "cache": 114, "join": 111, "unlock": 46,
}
LEGEND = {
    "levels": {"DBG": "debug detail", "INF": "normal operation", "WRN": "recoverable problem", "ERR": "failure",
               "CRT": "server cannot continue"},
    "components": {k: v for k, v in COMPONENT.items() if k not in ("stealth", "headers", "status", "downflow", "error")},
    "tags": TAG,
}

_METHOD = {"GET": 84, "POST": 214, "PUT": 75, "PATCH": 75, "DELETE": 203, "HEAD": 141, "OPTIONS": 141}
_URL = re.compile(r"https?://[^\s)]+")
_NUM = re.compile(r"(?<![\w/.:])\d+(?:\.\d+)?\s?(?:s|ms|MB|KB|GB|B|%)(?![\w])")
_FRAC = re.compile(r"\b\d+/\d+\b")
_ARROW = re.compile(r"->|→")
_CODE = re.compile(r"\b(auth_required|rate_limited|ip_blocked|format_not_available|extractor_outdated|timeout|network_error|"
                   r"no_formats|private_video|unavailable|geo_restricted|age_restricted|cookies_invalid|busy|internal_error)\b")


def _status_color(code: int) -> int:
    return 84 if code < 300 else 51 if code < 400 else 221 if code < 500 else 203


def _latency_color(ms: float) -> int:
    return 84 if ms < 250 else 252 if ms < 1000 else 221 if ms < 3000 else 203


def _fmt_ms(ms: float) -> str:
    return f"{ms:.0f}ms" if ms < 1000 else f"{ms / 1000:.2f}s"


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n}B"


def _paint(msg: str, level: str) -> str:
    base = c({"WARNING": 221, "ERROR": 210, "CRITICAL": 205}.get(level, 252))
    first, _, rest = msg.partition(" ")
    out = msg
    if first in TAG:
        out = f"{c(TAG[first], True)}{first:<6}{R}{base}" + (rest and " " + rest)
    out = _URL.sub(lambda m: f"{c(75)}{UL}{m.group(0)}{R}{base}", out)
    out = _NUM.sub(lambda m: f"{c(221, True)}{m.group(0)}{R}{base}", out)
    out = _FRAC.sub(lambda m: f"{c(177)}{m.group(0)}{R}{base}", out)
    out = _CODE.sub(lambda m: f"{c(203)}{m.group(0)}{R}{base}", out)
    out = _ARROW.sub(f"{c(203)}→{R}{base}", out).replace(" · ", f" {DIM}·{R}{base} ")
    return f"{base}{out}{R}"


def _short(name: str) -> str:
    return name.replace("downflow.", "").replace("uvicorn.error", "server").replace("uvicorn.access", "access").replace("uvicorn", "server")


class _Formatter(logging.Formatter):
    def __init__(self, mode: str, color: bool) -> None:
        super().__init__()
        self.mode, self.color = mode, color

    def format(self, record: logging.LogRecord) -> str:
        name = _short(record.name)
        level = record.levelname
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        rid = getattr(record, "rid", "") or request_id.get()
        http = getattr(record, "http", None)
        ts = time.strftime("%H:%M:%S", time.localtime(record.created)) + f".{int(record.msecs):03d}"
        if self.mode == "json":
            out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
                   "level": level, "component": name, "rid": rid or None, "msg": msg}
            if http:
                out["http"] = http
            return json.dumps(out, separators=(",", ":"), default=str)
        badge = LEVEL.get(level, LEVEL["INFO"])[1]
        rid6 = (rid or "")[:6].ljust(6)
        if http:
            msg = self._http_line(http, plain=not self.color)
        if not self.color:
            return f"{ts}  {badge}  {name[:9]:<9}  {rid6}  {msg}"
        badge_c = LEVEL.get(level, LEVEL["INFO"])[0]
        body = msg if http else _paint(msg, level)
        return (f"{DIM}{ts}{R}  {badge_c} {badge} {R}  {c(COMPONENT.get(name, 245), True)}{name[:9]:<9}{R}  "
                f"{c(176)}{rid6}{R}  {body}")

    @staticmethod
    def _http_line(h: dict, plain: bool) -> str:
        ms = h.get("ms", 0.0)
        size = _fmt_bytes(int(h.get("bytes", 0)))
        extra = f"  {h['note']}" if h.get("note") else ""
        if plain:
            return f"{h['method']:<6} {h['path']}  {h['status']}  {_fmt_ms(ms)}  {size}  {h.get('ip', '')}{extra}"
        return (f"{c(_METHOD.get(h['method'], 250), True)}{h['method']:<6}{R} {c(255)}{h['path']}{R}  "
                f"{c(_status_color(h['status']), True)}{h['status']}{R}  {c(_latency_color(ms), True)}{_fmt_ms(ms)}{R}  "
                f"{DIM}{size}  {h.get('ip', '')}{R}{c(221)}{extra}{R}")


class _Console(logging.StreamHandler):
    """stderr handler that collapses identical consecutive lines."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)
        self._last: tuple | None = None
        self._count = 0
        self._ts = 0.0

    def emit(self, record: logging.LogRecord) -> None:
        key = (record.name, record.levelno, record.getMessage()) if not hasattr(record, "http") else None
        now = time.monotonic()
        if key is not None and key == self._last and now - self._ts < 2.0:
            self._count += 1
            self._ts = now
            return
        if self._count:
            fmt = self.formatter
            note = logging.LogRecord("downflow", logging.INFO, "", 0, f"previous line repeated {self._count} more times", None, None)
            super().emit(note) if fmt else None
            self._count = 0
        self._last, self._ts = key, now
        super().emit(record)


# ---------------------------------------------------------------- ring buffer for the admin terminal
_BUF: deque = deque(maxlen=int(os.environ.get("DOWNFLOW_LOG_BUFFER", "3000") or 3000))
_LOCK = threading.Lock()
_SEQ = [0]
_QUIET = ("/api/v1/admin/logs", "/api/v1/admin/stats", "/api/v1/admin/tasks", "/api/v1/auth/me")


class _Ring(logging.Handler):
    """Keeps recent records in memory for the admin dashboard terminal."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            http = getattr(record, "http", None)
            if record.exc_info:
                msg += "\n" + logging.Formatter().formatException(record.exc_info)
            row = {"t": time.strftime("%H:%M:%S", time.localtime(record.created)) + f".{int(record.msecs):03d}",
                   "level": record.levelname, "name": _short(record.name),
                   "rid": (getattr(record, "rid", "") or request_id.get())[:6], "msg": msg}
            if http:
                row["http"] = http
            with _LOCK:
                _SEQ[0] += 1
                row["id"] = _SEQ[0]
                _BUF.append(row)
        except Exception:  # noqa: BLE001
            pass


class _DropPolling(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        http = getattr(record, "http", None)
        if http:
            return not any(http["path"].startswith(q) for q in _QUIET)
        return not any(q in record.getMessage() for q in _QUIET)


def get_logs(after: int = 0, limit: int = 500) -> list[dict]:
    out: list[dict] = []
    with _LOCK:
        for r in reversed(_BUF):          # newest first: stop as soon as we reach what the client already has
            if r["id"] <= after or len(out) >= limit:
                break
            out.append(r)
    out.reverse()
    return out


def clear_logs() -> None:
    with _LOCK:
        _BUF.clear()


def setup_logging(level: str | None = None) -> None:
    fmt = (os.environ.get("DOWNFLOW_LOG_FORMAT") or "pretty").lower()
    forced = os.environ.get("DOWNFLOW_COLOR")
    color = fmt == "pretty" and (forced == "1" or (forced != "0" and sys.stderr.isatty() and "NO_COLOR" not in os.environ))
    console = _Console()
    console.setFormatter(_Formatter("json" if fmt == "json" else "pretty", color))
    console.addFilter(_DropPolling())
    ring = _Ring()
    ring.addFilter(_DropPolling())
    root = logging.getLogger()
    root.handlers = [console, ring]
    root.setLevel((level or os.environ.get("DOWNFLOW_LOG_LEVEL", "INFO")).upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)      # the access log is written by helpers/middleware.py
    logging.getLogger("httpx").setLevel(logging.WARNING)
