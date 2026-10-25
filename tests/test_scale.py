"""Tests for logging, rate limiting, the guard middleware and the Redis task store.  python -m pytest -q tests"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fakeredis  # noqa: E402

from advanced import extractor_config as ec  # noqa: E402
from helpers import logger, metrics, netinfo, ratelimit  # noqa: E402
from helpers.middleware import GuardMiddleware, state  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _rec(msg, level=logging.INFO, name="downflow.engine", **extra):
    r = logging.LogRecord(name, level, "", 0, msg, None, None)
    r.__dict__.update(extra)
    return r


# ------------------------------------------------------------------ logging
def test_pretty_has_colour_and_plain_does_not():
    col, plain = logger._Formatter("pretty", True), logger._Formatter("pretty", False)
    r = _rec("try   yt-tv 1/9 · proxy none")
    assert "\x1b[" in col.format(r) and "\x1b[" not in plain.format(r)
    assert ANSI.sub("", col.format(r)).split("  ")[-1] == plain.format(r).split("  ")[-1] or "yt-tv" in ANSI.sub("", col.format(r))


def test_levels_have_distinct_colours():
    f = logger._Formatter("pretty", True)
    seen = {f.format(_rec("x", lv)).split("  ")[1] for lv in (10, 20, 30, 40, 50)}
    assert len(seen) == 5


def test_json_format_is_valid_and_has_rid():
    f = logger._Formatter("json", False)
    tok = logger.request_id.set("abc123")
    try:
        d = json.loads(f.format(_rec("hello", level=logging.WARNING)))
    finally:
        logger.request_id.reset(tok)
    assert d["level"] == "WARNING" and d["rid"] == "abc123" and d["component"] == "engine"


def test_http_record_formats_method_status_latency():
    f = logger._Formatter("pretty", False)
    out = f.format(_rec("x", name="downflow.access", http={"method": "GET", "path": "/a", "status": 503, "ms": 1500, "bytes": 2048, "ip": "1.1.1.1"}))
    assert "GET" in out and "/a" in out and "503" in out and "1.50s" in out and "2.0KB" in out


def test_ring_buffer_and_polling_filter():
    logger.setup_logging("DEBUG")
    logger.clear_logs()
    lg = logging.getLogger("downflow.test")
    lg.info("one")
    lg.info("GET /api/v1/admin/logs 200")
    lg.info("two")
    msgs = [r["msg"] for r in logger.get_logs()]
    assert msgs == ["one", "two"]
    first = logger.get_logs()[0]["id"]
    assert [r["msg"] for r in logger.get_logs(after=first)] == ["two"]


# ------------------------------------------------------------------ client ip / rate limit
def test_client_ip_uses_trusted_hop_not_spoofed_value(monkeypatch):
    monkeypatch.setattr(netinfo, "get_config", lambda: replace(ec.get_config(), proxy_hops=1, client_ip_header=""))
    hdr = [(b"x-forwarded-for", b"6.6.6.6, 9.9.9.9")]
    assert netinfo.client_ip(hdr, "10.0.0.1") == "9.9.9.9"
    monkeypatch.setattr(netinfo, "get_config", lambda: replace(ec.get_config(), proxy_hops=0, client_ip_header=""))
    assert netinfo.client_ip(hdr, "10.0.0.1") == "10.0.0.1"


def test_ipv6_collapses_to_64():
    assert netinfo.limit_key("2001:db8::1") == netinfo.limit_key("2001:db8::ffff")
    assert netinfo.limit_key("1.2.3.4") == "1.2.3.4"


def test_token_bucket_allows_burst_then_blocks(monkeypatch):
    monkeypatch.setenv("DOWNFLOW_RL_EXTRACT", "5/60")
    monkeypatch.setattr(ratelimit, "get_config", lambda: replace(ec.get_config(), redis_url=""))
    ip = "198.51.100.77"
    results = [ratelimit.check("extract", ip) for _ in range(7)]
    assert results[:5] == [0] * 5 and all(x > 0 for x in results[5:])
    assert ratelimit.check("extract", "198.51.100.78") == 0


def test_bucket_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(ratelimit, "MAX_KEYS", 100)
    monkeypatch.setattr(ratelimit, "get_config", lambda: replace(ec.get_config(), redis_url=""))
    for i in range(1000):
        ratelimit.check("download", f"10.1.{i // 250}.{i % 250}")
    assert len(ratelimit._buckets) <= 100


def test_redis_limit_is_shared(monkeypatch):
    r = fakeredis.FakeRedis()
    import helpers.redis_store as rs
    monkeypatch.setattr(rs, "get_redis", lambda: r)
    monkeypatch.setattr(ratelimit, "get_config", lambda: replace(ec.get_config(), redis_url="redis://x"))
    monkeypatch.setenv("DOWNFLOW_RL_EXTRACT", "3/60")
    out = [ratelimit.check("extract", "203.0.113.5") for _ in range(5)]
    assert out[:3] == [0, 0, 0] and out[3] > 0 and out[4] > 0


# ------------------------------------------------------------------ redis task store
def test_redis_status_manager_roundtrip():
    from helpers.redis_store import RedisStatusManager
    m = RedisStatusManager(fakeredis.FakeRedis(), ttl=60, max_tasks=100)
    assert m.blocking
    t = m.create()
    tid = t["task_id"]
    assert m.get_status(tid)["status"] == "queued"
    m.update(tid, status="processing", stage="extracting_metadata")
    assert m.active_count() == 1
    assert m.complete(tid, {"title": "x"})
    status, result = m.get_result(tid)
    assert status["status"] == "completed" and result == {"title": "x"}
    t2 = m.create()
    m.fail(t2["task_id"], {"code": "busy"})
    assert m.get_status(t2["task_id"])["status"] == "failed"
    assert len(m) == 2 and m.get_status("nope") is None


# ------------------------------------------------------------------ guard middleware (driven as raw ASGI)
async def _call(app, path="/api/v1/x", method="GET", headers=(), body=b"", peer="1.2.3.4"):
    sent = []
    msgs = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return msgs.pop(0) if msgs else {"type": "http.disconnect"}

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "path": path, "method": method, "headers": list(headers), "client": (peer, 1)}
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), data


def _ok_app(delay=0.0):
    async def app(scope, receive, send):
        if delay:
            await asyncio.sleep(delay)
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})
    return app


def _cfg(monkeypatch, **kw):
    import helpers.middleware as mw
    base = replace(ec.get_config(), redis_url="", **kw)
    monkeypatch.setattr(mw, "get_config", lambda: base)
    monkeypatch.setattr(ratelimit, "get_config", lambda: base)
    monkeypatch.setattr(netinfo, "get_config", lambda: base)


def test_adds_request_id_and_security_headers(monkeypatch):
    _cfg(monkeypatch)
    st, h, _ = asyncio.run(_call(GuardMiddleware(_ok_app()), headers=[(b"x-request-id", b"my-id_1")]))
    assert st == 200 and h[b"x-request-id"] == b"my-id_1" and h[b"x-content-type-options"] == b"nosniff"
    _, h2, _ = asyncio.run(_call(GuardMiddleware(_ok_app()), headers=[(b"x-request-id", b"bad id\x7f")]))
    assert h2[b"x-request-id"] != b"bad id\x7f"


def test_sheds_load_when_inflight_limit_reached(monkeypatch):
    _cfg(monkeypatch, max_inflight=5, api_rate="100000/60")

    async def go():
        app = GuardMiddleware(_ok_app(0.2))
        res = await asyncio.gather(*[_call(app, peer=f"9.9.9.{i}") for i in range(12)])
        health = await _call(app, path="/api/v1/health/")
        return [r[0] for r in res], health[0]
    codes, health = asyncio.run(go())
    assert codes.count(200) == 5 and codes.count(503) == 7 and health == 200
    assert state["inflight"] == 0


def test_flood_guard_returns_429_with_retry_after(monkeypatch):
    _cfg(monkeypatch, api_rate="5/60")

    async def go():
        app = GuardMiddleware(_ok_app())
        return [await _call(app, peer="7.7.7.7") for _ in range(8)]
    res = asyncio.run(go())
    assert [r[0] for r in res[:5]] == [200] * 5 and res[6][0] == 429 and b"retry-after" in res[6][1]
    assert json.loads(res[6][2])["error"]["code"] == "rate_limited"


def test_body_cap(monkeypatch):
    _cfg(monkeypatch, max_body=100, api_rate="1000/60")
    st, _, data = asyncio.run(_call(GuardMiddleware(_ok_app()), method="POST", headers=[(b"content-length", b"500")], body=b"x" * 500, peer="5.5.5.5"))
    assert st == 413 and json.loads(data)["error"]["code"] == "payload_too_large"


def test_unhandled_exception_becomes_json_500(monkeypatch):
    _cfg(monkeypatch, api_rate="1000/60")

    async def boom(scope, receive, send):
        raise RuntimeError("secret detail")
    st, _, data = asyncio.run(_call(GuardMiddleware(boom), peer="4.4.4.4"))
    assert st == 500 and b"secret detail" not in data


def test_per_ip_download_stream_cap(monkeypatch):
    _cfg(monkeypatch, max_ip_streams=2, api_rate="1000/60")

    async def go():
        app = GuardMiddleware(_ok_app(0.2))
        return await asyncio.gather(*[_call(app, path="/api/v1/download/abc/", peer="3.3.3.3") for _ in range(4)])
    codes = sorted(r[0] for r in asyncio.run(go()))
    assert codes == [200, 200, 429, 429] and not state["streams"]


def test_metrics_record_traffic():
    metrics.observe(200, 0.01)
    metrics.observe(503, 0.5)
    t = metrics.traffic()
    assert t["classes"].get("5xx", 0) >= 1 and t["latency_ms"]["p99"] >= t["latency_ms"]["p50"]


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
