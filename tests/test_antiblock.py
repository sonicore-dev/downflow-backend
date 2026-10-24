"""Offline tests for the anti-blocking layer. Run:  python -m pytest -q tests   (or: python tests/test_antiblock.py)"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from advanced import ErrorCode, ExtractionError, proxy_manager, strategy_engine  # noqa: E402
from advanced.extractor_config import ExtractorConfig  # noqa: E402
from advanced.stealth import build_strategies, is_youtube  # noqa: E402

YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
COOKIES = ("# Netscape HTTP Cookie File\n"
           + "".join(f".youtube.com\tTRUE\t/\tTRUE\t4102444800\t{n}\tvalue{i}\n"
                     for i, n in enumerate(["SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID", "LOGIN_INFO", "HSID"])))


def cfg(**kw) -> ExtractorConfig:
    kw.setdefault("cookie_source", "none")
    return ExtractorConfig(**kw)


def fresh_memory() -> None:
    strategy_engine._MEMORY = strategy_engine._Memory(Path(tempfile.mkdtemp()) / "m.json")


def reset_proxies() -> None:
    for d in (proxy_manager._health, proxy_manager._host_block, proxy_manager._affinity):
        d.clear()


# ---------------------------------------------------------------- proxies
def test_sticky_is_deterministic_and_templated_sid_changes_per_attempt():
    reset_proxies()
    c = cfg(proxy="http://u-{sid}:p@gate.example.com:7000")
    a = proxy_manager.pick_proxy(c, YT)
    assert a == proxy_manager.pick_proxy(c, YT)                    # same video -> same session id
    assert a != proxy_manager.pick_proxy(c, YT, attempt=1)         # retry -> fresh exit IP
    assert "{sid}" not in a and "gate.example.com" in a
    assert proxy_manager.redact(a) == "http://gate.example.com:7000"


def test_blocked_proxy_is_parked_per_site_and_pool_fails_over():
    reset_proxies()
    p1, p2 = "http://a.example.com:1", "http://b.example.com:1"
    c = cfg(proxy_list=(p1, p2), proxy_cooldown=60)
    first = proxy_manager.pick_proxy(c, YT)
    proxy_manager.report(first, "youtube.com", "blocked", c)
    other = p2 if first == p1 else p1
    assert proxy_manager.pick_proxy(c, YT + "2", host="youtube.com") == other
    # the parked proxy is still fine for other sites
    assert {proxy_manager.pick_proxy(c, "https://vimeo.com/%d" % i, host="vimeo.com") for i in range(20)} == {p1, p2}


def test_single_proxy_is_never_starved_and_direct_fallback_respected():
    reset_proxies()
    p = "http://only.example.com:1"
    c = cfg(proxy=p)
    proxy_manager.report(p, "youtube.com", "dead", c)
    assert proxy_manager.pick_proxy(c, YT, host="youtube.com") == p
    assert proxy_manager.pick_proxy(c, YT, host="youtube.com", exclude=[p], attempt=1) is None   # direct fallback
    assert proxy_manager.pick_proxy(cfg(), YT) is None


def test_affinity_returns_the_proxy_that_worked():
    reset_proxies()
    c = cfg(proxy_list=("http://a.example.com:1", "http://b.example.com:1"))
    worked = "http://b.example.com:1"
    proxy_manager.bind(YT, worked)
    assert proxy_manager.pick_proxy(c, YT) == worked


# ---------------------------------------------------------------- strategies
def test_youtube_strategy_order_follows_cookie_policy():
    path = Path(tempfile.mkdtemp()) / "youtube.txt"
    path.write_text(COOKIES)
    base = dict(cookie_source="file", cookie_file=str(path))
    last = [n for n, _ in build_strategies(YT, cfg(cookie_policy="last", **base))]
    first = [n for n, _ in build_strategies(YT, cfg(cookie_policy="first", **base))]
    never = [n for n, _ in build_strategies(YT, cfg(cookie_policy="never", **base))]
    assert last.index("yt-web_safari") < last.index("yt-cookies-safari")
    assert first.index("yt-cookies-safari") < first.index("yt-web_safari")
    assert not any(n.startswith("yt-cookies") for n in never)
    strategies = dict(build_strategies(YT, cfg(cookie_policy="last", **base)))
    assert strategies["yt-tv"]["_cookies"] is False and "_cookies" not in strategies["yt-cookies-tv"]


def test_generic_sites_get_impersonation_ladder_not_youtube_clients():
    names = [n for n, _ in build_strategies("https://vimeo.com/123", cfg())]
    assert names[0] == "chrome" and "plain" in names and not any(n.startswith("yt-") for n in names)
    assert is_youtube("https://youtu.be/x") and not is_youtube("https://notyoutube.com/x")


# ---------------------------------------------------------------- engine
def _err(code):
    return ExtractionError(code, detail="test")


def test_engine_falls_through_blocks_and_remembers_the_winner():
    fresh_memory(); reset_proxies()
    calls = []

    def attempt(name, overlay, proxy):
        calls.append(name)
        if name != "yt-android_vr-nopage":
            raise _err(ErrorCode.AUTH_REQUIRED)
        return {"id": "x"}

    c = cfg(race_width=1, hedge_delay=0)
    info, _, won = strategy_engine.run(YT, c, attempt, time.monotonic() + 30, lambda *a: None)
    assert won == "yt-android_vr-nopage" and info == {"id": "x"} and len(calls) > 1
    calls.clear()
    strategy_engine.run(YT, c, attempt, time.monotonic() + 30, lambda *a: None)
    assert calls == ["yt-android_vr-nopage"]                              # second request: first try wins


def test_engine_hedges_a_slow_attempt():
    fresh_memory(); reset_proxies()

    def attempt(name, overlay, proxy):
        if name == "chrome":
            time.sleep(3)
            return {"slow": True}
        return {"fast": True}

    c = cfg(race_width=2, hedge_delay=0.3, proxy_list=("http://a.example.com:1", "http://b.example.com:1"))
    t = time.monotonic()
    info, _, won = strategy_engine.run("https://vimeo.com/1", c, attempt, time.monotonic() + 20, lambda *a: None)
    assert info == {"fast": True} and time.monotonic() - t < 2


def test_no_parallel_race_from_a_single_ip():
    fresh_memory(); reset_proxies()
    running, peak = [0], [0]

    def attempt(name, overlay, proxy):
        running[0] += 1; peak[0] = max(peak[0], running[0])
        time.sleep(0.5)
        running[0] -= 1
        if name != "yt-tv":
            raise _err(ErrorCode.AUTH_REQUIRED)
        return {"ok": 1}

    strategy_engine.run(YT, cfg(race_width=2, hedge_delay=0.1), attempt, time.monotonic() + 20, lambda *a: None)
    assert peak[0] == 1                                            # same IP: sequential, never doubled


def test_format_not_available_falls_through_and_single_ip_gives_up_after_four_blocks():
    fresh_memory(); reset_proxies()
    calls = []

    def attempt(name, overlay, proxy):
        calls.append(name)
        raise _err(ErrorCode.FORMAT_NOT_AVAILABLE)                  # YouTube strips all formats on a blocked session

    try:
        strategy_engine.run(YT, cfg(race_width=1), attempt, time.monotonic() + 20, lambda *a: None)
        raise AssertionError("expected error")
    except ExtractionError as e:
        assert e.code is ErrorCode.FORMAT_NOT_AVAILABLE
    assert len(calls) == 4                                         # tried several routes, then stopped hammering

    calls.clear()
    c = cfg(race_width=1, proxy_list=("http://a.example.com:1", "http://b.example.com:1"))
    try:
        strategy_engine.run(YT, c, attempt, time.monotonic() + 20, lambda *a: None)
    except ExtractionError:
        pass
    assert len(calls) > 4                                          # with several exits it keeps going


def test_repeated_blocks_from_one_ip_report_ip_blocked():
    fresh_memory(); reset_proxies()

    def attempt(name, overlay, proxy):
        raise _err(ErrorCode.RATE_LIMITED if name == "yt-web_safari" else ErrorCode.FORMAT_NOT_AVAILABLE)

    try:
        strategy_engine.run(YT, cfg(race_width=1), attempt, time.monotonic() + 20, lambda *a: None)
        raise AssertionError("expected error")
    except ExtractionError as e:
        assert e.code is ErrorCode.IP_BLOCKED and e.retryable and e.http_status == 503


def test_logged_in_routes_are_tried_before_giving_up():
    fresh_memory(); reset_proxies()
    path = Path(tempfile.mkdtemp()) / "youtube.txt"
    path.write_text(COOKIES)
    calls = []

    def attempt(name, overlay, proxy):
        calls.append(name)
        if name.startswith("yt-cookies"):
            return {"ok": 1}
        raise _err(ErrorCode.AUTH_REQUIRED)

    c = cfg(race_width=1, cookie_source="file", cookie_file=str(path))
    _, _, won = strategy_engine.run(YT, c, attempt, time.monotonic() + 20, lambda *a: None)
    assert won.startswith("yt-cookies") and len([n for n in calls if not n.startswith("yt-cookies")]) == 4


def test_ip_blocked_headline_even_with_a_stray_outdated_error():
    errs = [_err(ErrorCode.FORMAT_NOT_AVAILABLE), _err(ErrorCode.EXTRACTOR_OUTDATED),
            _err(ErrorCode.AUTH_REQUIRED), _err(ErrorCode.AUTH_REQUIRED)]
    assert strategy_engine._summarise(errs).code is ErrorCode.IP_BLOCKED


def test_nopage_variants_exist_for_youtube_and_doctor_runs():
    names = [n for n, _ in build_strategies(YT, cfg())]
    assert "yt-android_vr-nopage" in names
    from advanced import doctor
    checks = doctor.run_checks(cfg())
    assert {c["check"] for c in checks} >= {"yt-dlp", "JavaScript runtime", "challenge solver", "proxy"}


def test_engine_stops_on_final_errors_and_reports_most_informative():
    fresh_memory(); reset_proxies()
    calls = []

    def gone(name, overlay, proxy):
        calls.append(name)
        raise _err(ErrorCode.UNAVAILABLE)

    try:
        strategy_engine.run(YT, cfg(race_width=1), gone, time.monotonic() + 10, lambda *a: None)
        raise AssertionError("expected error")
    except ExtractionError as e:
        assert e.code is ErrorCode.UNAVAILABLE and len(calls) == 1

    def mixed(name, overlay, proxy):
        raise _err(ErrorCode.AUTH_REQUIRED if name == "yt-tv" else ErrorCode.INTERNAL_ERROR)

    try:
        strategy_engine.run(YT, cfg(race_width=1), mixed, time.monotonic() + 10, lambda *a: None)
        raise AssertionError("expected error")
    except ExtractionError as e:
        assert e.code is ErrorCode.AUTH_REQUIRED                  # not the useless INTERNAL_ERROR


def test_engine_failover_moves_to_another_proxy():
    fresh_memory(); reset_proxies()
    seen = []

    def attempt(name, overlay, proxy):
        seen.append(proxy)
        if len(seen) < 3:
            raise _err(ErrorCode.AUTH_REQUIRED)
        return {"ok": 1}

    c = cfg(proxy_list=("http://a.example.com:1", "http://b.example.com:1", "http://c.example.com:1"),
            race_width=1, hedge_delay=0)
    strategy_engine.run(YT, c, attempt, time.monotonic() + 10, lambda *a: None)
    assert len(set(seen)) == 3                                     # every retry used a different proxy


# ---------------------------------------------------------------- extractor
def test_identical_concurrent_requests_share_one_extraction():
    from engine import metadata_extractor as me

    calls = []
    me._CACHE.clear()
    saved = (me.validate_url, me.resolve_redirects, me._run_strategies, me.normalize_result)
    me.validate_url = lambda u, c: u
    me.resolve_redirects = lambda u, c: None

    def slow(url, opts, policy, deadline, config, on_retry, report):
        calls.append(1)
        time.sleep(0.5)
        return {"id": "v"}, None

    me._run_strategies = slow
    me.normalize_result = lambda info, c, **kw: {"platform": {"key": "t"}, "title": "T"}
    try:
        out = []
        ts = [threading.Thread(target=lambda: out.append(me.extract("https://example.com/v", config=cfg()))) for _ in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        assert len(out) == 6 and len(calls) == 1
        me.extract("https://example.com/v", config=cfg())          # cache hit
        assert len(calls) == 1
    finally:
        me.validate_url, me.resolve_redirects, me._run_strategies, me.normalize_result = saved


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print("PASS", name)
            except Exception as exc:  # noqa: BLE001
                failed += 1; print("FAIL", name, repr(exc))
    sys.exit(1 if failed else 0)
