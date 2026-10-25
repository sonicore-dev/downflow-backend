"""One pure-ASGI middleware that protects and observes the whole server.

Order of work for every HTTP request (cheapest checks first, so an overloaded server spends almost nothing on
traffic it is going to refuse):

 1. request id      reuse a sane ``X-Request-ID`` or mint one; exposed to every log line via a contextvar
 2. load shedding   in-flight >= ``DOWNFLOW_MAX_INFLIGHT``  -> 503 ``busy`` + Retry-After (admin / health exempt)
 3. flood guard     per-IP token bucket on /api/ (``DOWNFLOW_API_RATE``) -> 429 ``rate_limited``
 4. body cap        Content-Length (or streamed bytes) above ``DOWNFLOW_MAX_BODY`` -> 413
 5. stream cap      at most ``DOWNFLOW_MAX_IP_STREAMS`` concurrent file downloads per IP -> 429
 6. app             the request is served; unhandled exceptions become a JSON 500 (never a stack trace)
 7. response        security headers, X-Request-ID, X-Response-Time, metrics, access log

Everything is O(1) per request and allocation-light; no request body is buffered here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import Any

from advanced.extractor_config import get_config
from helpers import metrics, netinfo, ratelimit
from helpers.logger import request_id

__all__ = ["GuardMiddleware", "state"]

log = logging.getLogger("downflow.access")
guard = logging.getLogger("downflow.guard")

_EXEMPT = ("/api/v1/health", "/health", "/admin", "/api/v1/admin", "/api/v1/auth", "/login", "/logout")
_QUIET = ("/api/v1/extract/status/", "/api/v1/extract/result/", "/api/v1/admin", "/api/v1/health", "/health",
          "/static/", "/favicon")
_SEC = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"strict-origin-when-cross-origin"),
        (b"x-frame-options", b"SAMEORIGIN"), (b"permissions-policy", b"camera=(), microphone=(), geolocation=()")]
_RID_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")

state: dict[str, Any] = {"inflight": 0, "peak": 0, "shed": 0, "limited": 0, "too_large": 0, "stream_denied": 0,
                         "streams": {}}


_warned: dict[str, float] = {}


def _warn(key: str, msg: str, *args: Any) -> None:
    """Guard warnings at most once per key per 5 s, so an attack cannot flood the log (counters keep exact totals)."""
    now = time.monotonic()
    if now - _warned.get(key, 0.0) < 5.0:
        return
    if len(_warned) > 4096:
        _warned.clear()
    _warned[key] = now
    guard.warning(msg, *args)


def _json(status: int, code: str, message: str, retry: int = 0) -> tuple[list, bytes]:
    body = json.dumps({"status": "error", "error": {"code": code, "message": message, "retryable": bool(retry),
                                                    "retry_after": retry or None}}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if retry:
        headers.append((b"retry-after", str(retry).encode()))
    return headers, body


class GuardMiddleware:
    def __init__(self, app) -> None:
        self.app = app
        self._sec_bucket = 0
        self._sec_count = 0
        self._summary_at = time.monotonic()
        self._summary = [0, 0, 0.0]      # requests, errors, total seconds since last summary
        self._suppressed = 0

    async def _reject(self, send, status: int, code: str, msg: str, retry: int, extra: list) -> None:
        headers, body = _json(status, code, msg, retry)
        await send({"type": "http.response.start", "status": status, "headers": headers + extra})
        await send({"type": "http.response.body", "body": body})

    def _log(self, method: str, path: str, status: int, ms: float, nbytes: int, ip: str, note: str = "") -> None:
        now = time.monotonic()
        sec = int(now)
        if sec != self._sec_bucket:
            self._sec_bucket, self._sec_count = sec, 0
        self._sec_count += 1
        s = self._summary
        s[0] += 1
        s[1] += status >= 500
        s[2] += ms / 1000
        cfg = get_config()
        quiet = path.startswith(_QUIET) and status < 400
        slow = ms >= cfg.slow_request * 1000
        if self._sec_count > cfg.access_log_rps > 0 and status < 500 and not slow:
            self._suppressed += 1                      # flood: only failures + slow requests are logged individually
        else:
            level = logging.DEBUG if quiet else logging.ERROR if status >= 500 else logging.WARNING if (status >= 400 or slow) else logging.INFO
            if slow and not note:
                note = "slow"
            log.log(level, "%s %s %s", method, path, status,
                    extra={"http": {"method": method, "path": path, "status": status, "ms": round(ms, 1),
                                    "bytes": nbytes, "ip": ip, "note": note}})
        if now - self._summary_at >= 10 and s[0] > cfg.access_log_rps * 10:
            log.info("traffic %d requests in %.0fs · %.0f rps · %d server errors · avg %.0fms · %d lines suppressed",
                     s[0], now - self._summary_at, s[0] / (now - self._summary_at), s[1], 1000 * s[2] / s[0], self._suppressed)
            self._suppressed = 0
        if now - self._summary_at >= 10:
            self._summary, self._summary_at = [0, 0, 0.0], now

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        t0 = time.perf_counter()
        cfg = get_config()
        path: str = scope["path"]
        method: str = scope["method"]
        raw = scope.get("headers") or []
        peer = (scope.get("client") or ("?", 0))[0]
        ip = netinfo.client_ip(raw, peer)
        rid = ""
        for k, v in raw:
            if k == b"x-request-id":
                cand = v.decode("latin-1")[:32]
                if cand and set(cand) <= _RID_OK:
                    rid = cand
                break
        rid = rid or secrets.token_hex(4)
        token = request_id.set(rid)
        scope.setdefault("state", {})["request_id"] = rid
        exempt = path.startswith(_EXEMPT) or not path.startswith("/api/")
        rid_hdr = [(b"x-request-id", rid.encode())]
        status, sent = [500], [0]
        counted_stream = False
        try:
            if method == "OPTIONS":
                return await self.app(scope, receive, send)
            if path.startswith(_EXEMPT[:2]) or path.startswith("/admin") or path.startswith("/api/v1/admin"):
                shed_exempt = True
            else:
                shed_exempt = False
            if not shed_exempt and state["inflight"] >= cfg.max_inflight:
                state["shed"] += 1
                metrics.inc("shed")
                _warn("shed", "shed %s %s · %d in flight (limit %d) · %d shed so far", method, path, state["inflight"], cfg.max_inflight, state["shed"])
                status[0] = 503
                return await self._reject(send, 503, "busy", "The server is at capacity. Please retry shortly.", 3, rid_hdr)
            if not exempt:
                key = netinfo.limit_key(ip)
                wait = await asyncio.to_thread(ratelimit.check, "api", key) if cfg.redis_url else ratelimit.check("api", key)
                if wait:
                    state["limited"] += 1
                    metrics.inc("rate_limited")
                    _warn("api" + ip, "limit api · %s waits %ds · %d limited so far", ip, wait, state["limited"])
                    status[0] = 429
                    return await self._reject(send, 429, "rate_limited", "Too many requests. Please wait a moment.",
                                              int(wait), rid_hdr)
            if method in ("POST", "PUT", "PATCH") and not path.startswith("/api/v1/admin"):
                for k, v in raw:
                    if k == b"content-length":
                        try:
                            if int(v) > cfg.max_body:
                                state["too_large"] += 1
                                status[0] = 413
                                return await self._reject(send, 413, "payload_too_large", "Request body is too large.", 0, rid_hdr)
                        except ValueError:
                            pass
                        break
                seen = [0]
                inner = receive

                async def receive(_inner=inner):          # noqa: ANN202  stream-count bodies that lack Content-Length
                    msg = await _inner()
                    if msg["type"] == "http.request":
                        seen[0] += len(msg.get("body", b""))
                        if seen[0] > cfg.max_body:
                            raise _TooLarge()
                    return msg
            if path.startswith("/api/v1/download/") and cfg.max_ip_streams > 0:
                key = netinfo.limit_key(ip)
                streams = state["streams"]
                if streams.get(key, 0) >= cfg.max_ip_streams:
                    state["stream_denied"] += 1
                    _warn("st" + key, "limit streams · %s has %d active downloads", ip, streams[key])
                    status[0] = 429
                    return await self._reject(send, 429, "rate_limited", "Too many simultaneous downloads.", 5, rid_hdr)
                streams[key] = streams.get(key, 0) + 1
                counted_stream = True

            async def wrapped(msg) -> None:
                if msg["type"] == "http.response.start":
                    status[0] = msg["status"]
                    have = {k for k, _ in msg["headers"]}
                    msg = {**msg, "headers": list(msg["headers"]) + rid_hdr
                           + [(b"x-response-time", f"{(time.perf_counter() - t0) * 1000:.0f}ms".encode())]
                           + [h for h in _SEC if h[0] not in have]}
                elif msg["type"] == "http.response.body":
                    sent[0] += len(msg.get("body", b""))
                await send(msg)

            state["inflight"] += 1
            metrics.gauge("inflight", 1)
            state["peak"] = max(state["peak"], state["inflight"])
            try:
                await self.app(scope, receive, wrapped)
            finally:
                state["inflight"] -= 1
                metrics.gauge("inflight", -1)
        except _TooLarge:
            status[0] = 413
            if not sent[0]:
                await self._reject(send, 413, "payload_too_large", "Request body is too large.", 0, rid_hdr)
        except (asyncio.CancelledError, GeneratorExit):
            status[0] = 499                             # client went away
            raise
        except Exception:  # noqa: BLE001
            log.exception("unhandled error serving %s %s", method, path)
            status[0] = 500
            if not sent[0]:
                try:
                    await self._reject(send, 500, "internal_error", "Something went wrong on our side.", 0, rid_hdr)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            if counted_stream:
                key = netinfo.limit_key(ip)
                n = state["streams"].get(key, 1) - 1
                if n <= 0:
                    state["streams"].pop(key, None)
                else:
                    state["streams"][key] = n
            dt = time.perf_counter() - t0
            streaming = path.startswith("/api/v1/download/")
            metrics.observe(status[0], dt, counted=not streaming)
            self._log(method, path, status[0], dt * 1000, sent[0], ip, "client closed" if status[0] == 499 else "")
            request_id.reset(token)


class _TooLarge(Exception):
    pass
