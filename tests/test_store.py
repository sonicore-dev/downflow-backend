"""Offline tests for the database-backed store (fake Turso server + local SQLite). Run: python -m pytest -q tests"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TOK = "test-token"
_conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
_lock = threading.Lock()


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.headers.get("Authorization") != f"Bearer {TOK}":
            self.send_response(401); self.end_headers(); self.wfile.write(b'{"error":"bad token"}'); return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        results = []
        for r in body["requests"]:
            if r["type"] != "execute":
                results.append({"type": "ok", "response": {"type": "close"}}); continue
            args = []
            for a in r["stmt"]["args"]:
                args.append(None if a["type"] == "null" else int(a["value"]) if a["type"] == "integer" else a["value"])
            with _lock:
                cur = _conn.execute(r["stmt"]["sql"], args)
                rows = cur.fetchall() if cur.description else []
            enc = lambda v: {"type": "null"} if v is None else {"type": "integer", "value": str(v)} if isinstance(v, int) else {"type": "text", "value": str(v)}
            results.append({"type": "ok", "response": {"type": "execute", "result": {"rows": [[enc(c) for c in row] for row in rows]}}})
        out = json.dumps({"results": results}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)


def _setup(tmp_path=None):
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["DOWNFLOW_TURSO_URL"] = f"http://127.0.0.1:{srv.server_port}"
    os.environ["DOWNFLOW_TURSO_TOKEN"] = TOK
    from advanced import db, cookie_store
    from advanced.extractor_config import get_config
    get_config.cache_clear()
    db.reset()
    cookie_store._cache.clear(); cookie_store._loaded = False
    return srv


def _cookies(domain, n=3, name="sessionid"):
    return "# Netscape HTTP Cookie File\n" + "".join(f".{domain}\tTRUE\t/\tTRUE\t4102444800\t{name}{i}\tv{i}\n" for i in range(n))


def test_turso_roundtrip_encrypted_and_per_platform():
    srv = _setup()
    from advanced import cookie_store, db
    from advanced.cookie_manager import cookie_options, cookies_active
    from advanced.extractor_config import ExtractorConfig
    assert db.status()["backend"] == "turso"
    cookie_store.save("https://www.TikTok.com/", _cookies("tiktok.com"), test_url="https://www.tiktok.com/@x/video/1")
    cookie_store.save("youtube.com", _cookies("youtube.com", name="SID"))
    # encrypted at rest
    raw = _conn.execute("SELECT data FROM cookies WHERE domain='tiktok.com'").fetchone()[0]
    assert "sessionid" not in raw and "tiktok" not in raw
    # survives a "restart"
    cookie_store._cache.clear(); cookie_store._loaded = False
    assert {e["domain"] for e in cookie_store.list_entries()} == {"tiktok.com", "youtube.com"}
    # platform matching, aliases, and no cross-platform leakage
    cfg = ExtractorConfig(cookie_source="none")
    assert cookies_active(cfg, "https://vm.tiktok.com/abc")
    assert cookies_active(cfg, "https://youtu.be/xyz")
    assert not cookies_active(cfg, "https://example.com/v")
    with cookie_options(cfg, "https://www.tiktok.com/@a/video/1") as o:
        txt = Path(o["cookiefile"]).read_text()
        assert "tiktok.com" in txt and "youtube.com" not in txt
    with cookie_options(cfg, "https://example.com/v") as o:
        assert o == {}
    # disable / delete
    cookie_store.update_meta("tiktok.com", enabled=False)
    assert not cookies_active(cfg, "https://www.tiktok.com/x")
    cookie_store.delete("tiktok.com")
    assert [e["domain"] for e in cookie_store.list_entries()] == ["youtube.com"]
    srv.shutdown()


def test_cookies_are_the_fallback_not_first():
    srv = _setup()
    from advanced import cookie_store
    from advanced.extractor_config import ExtractorConfig
    from advanced.stealth import build_strategies
    cookie_store.save("tiktok.com", _cookies("tiktok.com"))
    cfg = ExtractorConfig(cookie_source="none", cookie_policy="last")
    names = [n for n, _ in build_strategies("https://www.tiktok.com/@a/video/1", cfg)]
    firsts = [n for n, ov in build_strategies("https://www.tiktok.com/@a/video/1", cfg) if ov.get("_cookies", True) is not False]
    assert names[0] == "chrome" and all(n.startswith("cookies-") for n in firsts) and names[-1].startswith("cookies-")
    plain = [n for n, _ in build_strategies("https://example.org/v", cfg)]
    assert not any(n.startswith("cookies-") for n in plain)
    srv.shutdown()


def test_updates_and_kv_and_bad_token():
    srv = _setup()
    from advanced import db
    from helpers import update_check
    assert update_check.get_update_data()["version_name"]
    d = update_check.publish("1.2.0", 12, True, "test")
    assert d == {"version_name": "1.2.0", "version_code": 12, "force_update": True}
    assert update_check.get_update_data()["version_code"] == 12
    rel = update_check.list_releases()
    old = [r for r in rel if not r["active"]][0]
    assert update_check.activate(old["id"])["version_code"] == old["version_code"]
    db.kv_set("x", {"a": 1}); assert db.kv_get("x") == {"a": 1}
    os.environ["DOWNFLOW_TURSO_TOKEN"] = "wrong"
    from advanced.extractor_config import get_config
    get_config.cache_clear(); db.reset()
    try:
        db.one("SELECT 1"); raise AssertionError("should fail")
    except db.DbError as exc:
        assert "401" in str(exc)
    update_check.invalidate()
    assert update_check._cache["data"] is None
    srv.shutdown()


def test_sqlite_fallback(tmp_path=None):
    os.environ.pop("DOWNFLOW_TURSO_URL", None); os.environ.pop("DOWNFLOW_TURSO_TOKEN", None)
    from advanced import db
    from advanced.extractor_config import get_config
    get_config.cache_clear(); db.reset()
    assert db.status()["backend"] == "sqlite"
    db.kv_set("k", [1, 2]); assert db.kv_get("k") == [1, 2]


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
