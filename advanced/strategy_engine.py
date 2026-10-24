"""Runs access strategies with failover, hedged racing and per-site memory.

* **Memory** - the strategy that last worked for a site is tried first next time (persisted to
  ``data/strategy_memory.json``), and strategies that keep failing are pushed to the back for 10 minutes.
  A repeat request therefore usually succeeds on the first attempt instead of walking the whole ladder.
* **Hedging** - if the running attempt has not answered after ``DOWNFLOW_HEDGE_DELAY`` seconds, the next
  strategy starts in parallel on another proxy (up to ``DOWNFLOW_RACE_WIDTH`` at once). First success wins.
* **Instant failover** - a clear block (bot check, 403, 429) starts the next strategy immediately, on a
  different proxy / session id, and the failing proxy is parked for that site.
* **Proxy affinity** - the winning proxy is bound to the URL so the download uses the same exit IP.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import proxy_manager
from .error_handler import ErrorCode, ExtractionError, classify_exception
from .extractor_config import BASE_DIR, ExtractorConfig
from .stealth import Strategy, build_strategies, is_youtube

__all__ = ["run", "memory_snapshot"]

log = logging.getLogger("downflow.strategy")

# Anything not listed here is final (unsupported, unavailable, private, geo ...) and raised immediately.
_CONTINUE = {ErrorCode.AUTH_REQUIRED, ErrorCode.RATE_LIMITED, ErrorCode.NETWORK_ERROR, ErrorCode.TIMEOUT,
             ErrorCode.NO_FORMATS, ErrorCode.EXTRACTOR_OUTDATED, ErrorCode.INTERNAL_ERROR,
             ErrorCode.COOKIES_INVALID, ErrorCode.AGE_RESTRICTED, ErrorCode.PRIVATE_VIDEO,
             ErrorCode.FORMAT_NOT_AVAILABLE}      # on a blocked session YouTube strips every real format ("only images")
_NEEDS_COOKIES = {ErrorCode.AGE_RESTRICTED, ErrorCode.PRIVATE_VIDEO}
_BLOCKED = {ErrorCode.AUTH_REQUIRED, ErrorCode.RATE_LIMITED}
_BLOCKLIKE = _BLOCKED | {ErrorCode.FORMAT_NOT_AVAILABLE, ErrorCode.NO_FORMATS}
_MAX_BLOCKS_SAME_EXIT = 4        # stop hammering one blocked IP: more attempts from it only prolong the ban
_DEAD = {ErrorCode.NETWORK_ERROR, ErrorCode.TIMEOUT}
_UNINFORMATIVE = {ErrorCode.INTERNAL_ERROR, ErrorCode.EXTRACTOR_OUTDATED, ErrorCode.NO_FORMATS}
_BAD_AFTER, _BAD_FOR = 3, 600.0

Attempt = Callable[[str, dict[str, Any], Optional[str]], dict[str, Any]]


class _Memory:
    def __init__(self, path: Path) -> None:
        self.path, self.lock, self.saved = path, threading.Lock(), 0.0
        self.winner: dict[str, str] = {}
        self.fails: dict[str, list[float]] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.winner = {k: v for k, v in data.get("winner", {}).items() if isinstance(v, str)}
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        if time.monotonic() - self.saved < 5:
            return
        self.saved = time.monotonic()
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"winner": self.winner}), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def order(self, site: str, strategies: list[Strategy]) -> list[Strategy]:
        now = time.time()
        with self.lock:
            def bad(name: str) -> bool:
                f = self.fails.get(f"{site}|{name}")
                return bool(f and f[0] >= _BAD_AFTER and now - f[1] < _BAD_FOR)
            good = [s for s in strategies if not bad(s[0])]
            poor = [s for s in strategies if bad(s[0])]
            w = self.winner.get(site)
            good.sort(key=lambda s: s[0] != w)              # stable: winner first, rest keep their order
            return good + poor

    def win(self, site: str, name: str) -> None:
        with self.lock:
            self.winner[site] = name
            for k in [k for k in self.fails if k.startswith(site + "|")]:
                self.fails.pop(k, None)
            self._save()

    def lose(self, site: str, name: str) -> None:
        with self.lock:
            f = self.fails.setdefault(f"{site}|{name}", [0, 0.0])
            f[0] += 1
            f[1] = time.time()


_MEMORY = _Memory(BASE_DIR / "data" / "strategy_memory.json")


def memory_snapshot() -> dict[str, Any]:
    with _MEMORY.lock:
        return {"winners": dict(_MEMORY.winner),
                "failing": {k: int(v[0]) for k, v in _MEMORY.fails.items() if v[0] >= _BAD_AFTER}}


def _summarise(errors: list[ExtractionError]) -> ExtractionError:
    """Pick the error that tells the operator the most. Several blocks in a row = the IP is the problem."""
    blocks = sum(e.code in _BLOCKED for e in errors)
    if len(errors) >= 3 and ((blocks and all(e.code in _BLOCKLIKE for e in errors)) or blocks >= 2):
        return ExtractionError(ErrorCode.IP_BLOCKED, detail="; ".join(sorted({e.code.value for e in errors})))
    best = [e for e in errors if e.code not in _UNINFORMATIVE] or errors
    return best[-1]


def run(url: str, config: ExtractorConfig, attempt: Attempt, deadline: float,
        report: Callable[[str, str], None]) -> tuple[dict[str, Any], Optional[str], str]:
    """Return ``(info, proxy_used, strategy_name)`` or raise the most informative :class:`ExtractionError`."""
    site = proxy_manager.host_of(url)
    strategies = _MEMORY.order(site, build_strategies(url, config))
    cookie_names = {n for n, ov in strategies if ov.get("_cookies", True) is not False}
    pool_ = proxy_manager.pool(config)
    multi_exit = len(pool_) > 1 or any(proxy_manager.is_templated(p) for p in pool_)
    # Racing only helps when the parallel attempt leaves from a different IP; from one IP it just doubles the load.
    width = max(1, config.race_width) if multi_exit else 1
    hedge = config.hedge_delay if config.hedge_delay > 0 else 1e9
    block_streak = 0
    tried_logged_in = False

    pending = list(strategies)
    running: dict[cf.Future, tuple[str, Optional[str], float]] = {}
    tried_proxies: list[str] = []
    errors: list[ExtractionError] = []
    launched = 0
    pool = cf.ThreadPoolExecutor(max_workers=width, thread_name_prefix="strategy")

    def launch() -> None:
        nonlocal launched
        name, overlay = pending.pop(0)
        proxy = proxy_manager.pick_proxy(config, url, host=site, exclude=tried_proxies, attempt=launched)
        if proxy:
            tried_proxies.append(proxy)
        if launched:
            report("retrying", f"Trying another access method ({name})")
        log.info("try   %s %d/%d · proxy %s", name, launched + 1, len(strategies), proxy_manager.redact(proxy))
        launched += 1
        running[pool.submit(attempt, name, overlay, proxy)] = (name, proxy, time.monotonic())

    try:
        launch()
        while running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            can_hedge = bool(pending) and len(running) < width
            done, _ = cf.wait(list(running), timeout=min(hedge, remaining) if can_hedge else remaining,
                              return_when=cf.FIRST_COMPLETED)
            if not done:
                if can_hedge:
                    log.info("hedge slow attempt -> starting next strategy in parallel")
                    launch()
                continue
            giveup = False
            for fut in done:
                name, proxy, t0 = running.pop(fut)
                try:
                    info = fut.result()
                except Exception as exc:  # noqa: BLE001
                    err = classify_exception(exc)
                    log.warning("block %s -> %s (%.1fs)", name, err.code.value, time.monotonic() - t0)
                    errors.append(err)
                    _MEMORY.lose(site, name)
                    block_streak = block_streak + 1 if err.code in _BLOCKLIKE else 0
                    if err.code in _BLOCKED:
                        proxy_manager.report(proxy, site, "blocked" if err.code is ErrorCode.AUTH_REQUIRED
                                             else "rate_limited", config)
                    elif err.code in _DEAD:
                        proxy_manager.report(proxy, site, "dead", config)
                    if err.code not in _CONTINUE:
                        raise err
                    if err.code in _NEEDS_COOKIES:       # only a logged-in route can help
                        pending[:] = [s for s in pending if s[0] in cookie_names and s[1].get("_cookies") is not False]
                        if not pending and not running:
                            raise err
                    if block_streak >= _MAX_BLOCKS_SAME_EXIT and not multi_exit and not running:
                        logged = [x for x in pending if x[0].startswith("yt-cookies")]
                        if logged and not tried_logged_in:      # last resort before giving up: the signed-in routes
                            tried_logged_in, block_streak = True, 0
                            pending[:] = logged
                            log.info("cookie-less routes are blocked; trying the logged-in routes")
                            continue
                        log.warning("giving up after %d blocked attempts from the same IP (add a proxy or wait)", block_streak)
                        giveup = True
                        break
                    continue
                proxy_manager.report(proxy, site, "ok", config)
                _MEMORY.win(site, name)
                if launched > 1:
                    log.info("ok    unlocked via %s", name)
                return info, proxy, name
            if giveup:
                break
            if pending and len(running) < width and (not running or errors):
                launch()
        if errors:
            raise _summarise(errors)
        raise ExtractionError(ErrorCode.TIMEOUT)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
