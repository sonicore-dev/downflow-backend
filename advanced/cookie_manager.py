# Cookie helpers: parsing Netscape cookie text and handing a platform's saved cookies to yt-dlp.
# The cookies themselves live in the database (see cookie_store.py); the server only writes a short-lived
# private copy for the duration of one request and deletes it afterwards.

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from .extractor_config import ExtractorConfig, get_config

__all__ = ["cookie_options", "cookies_active", "cli_args", "parse_netscape", "inspect_rows"]

log = logging.getLogger("downflow.cookies")

# cookie names that usually mean "this is a logged-in session"
_AUTH_NAME = re.compile(r"(sess|sid|token|auth|login|user|uid|jwt|ttwid)", re.I)


def parse_netscape(text: str) -> list[tuple]:
    # returns (domain, flag, path, secure, expires, name, value) tuples; accepts tabs or spaces and the #HttpOnly_ prefix
    rows = []
    for raw in text.splitlines():
        line = raw.strip("\r\n")
        if not line.strip():
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            parts = line.split(None, 6)
        if len(parts) != 7:
            continue
        d, flag, path, secure, exp, name, value = [x.strip() for x in parts]
        if not name or not re.fullmatch(r"-?\d+", exp or "x"):
            continue
        rows.append((d, flag.upper(), path or "/", secure.upper(), exp, name, value))
    return rows


def _dedupe(rows: list[tuple]) -> list[tuple]:
    # keep the latest expiry per (domain, path, name)
    best: dict[tuple, tuple] = {}
    for r in rows:
        k = (r[0].lower(), r[2], r[5])
        if k not in best or int(r[4]) >= int(best[k][4]):
            best[k] = r
    return list(best.values())


def _render(rows: list[tuple]) -> str:
    out = ["# Netscape HTTP Cookie File", ""]
    out += ["\t".join(r) for r in rows]
    return "\n".join(out) + "\n"


def inspect_rows(rows: list[tuple]) -> dict[str, Any]:
    # summary of a cookie set; never includes values
    now = time.time()
    auth = [r for r in rows if _AUTH_NAME.search(r[5])]
    exps = [int(r[4]) for r in auth if int(r[4]) > 0]
    soonest = min(exps) if exps else None
    return {
        "count": len(rows),
        "domains": sorted({r[0].lstrip(".") for r in rows})[:12],
        "logged_in_cookies": bool(auth),
        "earliest_auth_expiry": soonest,
        "expires_in_days": round((soonest - now) / 86400, 1) if soonest else None,
        "expired": bool(exps and max(exps) < now),
    }


def _store_entry(url: Optional[str]) -> Optional[dict[str, Any]]:
    if not url:
        return None
    try:
        from . import cookie_store
        return cookie_store.match(url)
    except Exception:  # noqa: BLE001
        return None


def cookies_active(config: Optional[ExtractorConfig] = None, url: Optional[str] = None) -> bool:
    # true when an enabled saved entry covers this URL's platform
    return _store_entry(url) is not None


@contextlib.contextmanager
def cookie_options(config: ExtractorConfig, url: Optional[str] = None) -> Iterator[dict[str, Any]]:
    # yields yt-dlp options for this URL's platform; only that platform's cookies are ever used
    entry = _store_entry(url)
    if not entry:
        yield {}
        return
    from . import cookie_store
    log.info("cookies: using saved %s cookies (%d) for %s", entry.get("label") or entry["domain"], entry["count"],
             cookie_store.host_of(url))
    ram = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None
    tmp_dir = tempfile.mkdtemp(prefix="downflow-cookies-", dir=ram)
    try:
        os.chmod(tmp_dir, 0o700)
        copy = Path(tmp_dir) / "cookies.txt"
        copy.write_text(entry["text"], encoding="utf-8")
        os.chmod(copy, 0o600)
        try:
            yield {"cookiefile": str(copy)}
        finally:
            with contextlib.suppress(Exception):
                cookie_store.writeback(entry["domain"], copy.read_text(encoding="utf-8", errors="ignore"))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def cli_args(opts: dict[str, Any]) -> list[str]:
    # the same options as yt-dlp command-line flags
    return ["--cookies", opts["cookiefile"]] if opts.get("cookiefile") else []
