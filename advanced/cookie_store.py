"""Per-platform cookie store backed by the database (encrypted at rest).

Each entry is keyed by a registrable domain ("youtube.com", "tiktok.com", any site). The extractor/downloader looks up the
entry that matches the URL's host and uses only that entry's cookies, and only after the cookie-less routes failed.
Entries are cached in memory (loaded at start-up and after every change) so a database outage never breaks extraction.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Optional
from urllib.parse import urlsplit

from . import db

log = logging.getLogger("downflow.store")

# Hosts that belong to the same login as the entry's domain.
DEFAULT_ALIASES = {
    "youtube.com": ["youtu.be", "youtube-nocookie.com"],
    "x.com": ["twitter.com", "t.co"],
    "twitter.com": ["x.com", "t.co"],
    "facebook.com": ["fb.watch", "fb.com"],
    "tiktok.com": ["tiktokv.com"],
    "instagram.com": ["instagr.am"],
    "bilibili.com": ["b23.tv"],
    "reddit.com": ["redd.it"],
}
PLATFORM_NAMES = {
    "youtube.com": "YouTube", "youtu.be": "YouTube", "tiktok.com": "TikTok", "instagram.com": "Instagram",
    "facebook.com": "Facebook", "fb.watch": "Facebook", "x.com": "X (Twitter)", "twitter.com": "X (Twitter)",
    "reddit.com": "Reddit", "vimeo.com": "Vimeo", "twitch.tv": "Twitch", "dailymotion.com": "Dailymotion",
    "bilibili.com": "Bilibili", "soundcloud.com": "SoundCloud", "pinterest.com": "Pinterest", "linkedin.com": "LinkedIn",
    "snapchat.com": "Snapchat", "threads.net": "Threads", "telegram.org": "Telegram", "t.me": "Telegram",
    "spotify.com": "Spotify", "tumblr.com": "Tumblr", "vk.com": "VK", "ok.ru": "OK.ru", "rumble.com": "Rumble",
    "bsky.app": "Bluesky", "kick.com": "Kick", "patreon.com": "Patreon", "netflix.com": "Netflix",
}
_DOMAIN_RE = re.compile(r"^(?=.{3,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

_lock = threading.RLock()
_cache: dict[str, dict[str, Any]] = {}
_loaded = False


def norm_domain(raw: str) -> str:
    """'https://www.YouTube.com/x' -> 'youtube.com'. Raises ValueError when it is not a plausible domain."""
    s = (raw or "").strip().lower()
    if "://" in s:
        s = urlsplit(s).hostname or ""
    s = s.split("/")[0].split(":")[0].strip(".")
    if s.startswith("www."):
        s = s[4:]
    if not _DOMAIN_RE.match(s):
        raise ValueError("Enter a domain like youtube.com or tiktok.com")
    return s


def registrable(host: str) -> str:
    """'m.youtube.com' -> 'youtube.com' ('bbc.co.uk' style suffixes are kept together)."""
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "ac") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def platform_name(url_or_host: str) -> str:
    """Human platform name for logs/labels: a saved label, a well-known name, or the site's own name."""
    host = host_of(url_or_host) if ("/" in url_or_host or ":" in url_or_host) else url_or_host.lower()
    reg = registrable(host)
    if reg in PLATFORM_NAMES:
        return PLATFORM_NAMES[reg]
    return reg.split(".")[0].capitalize() if reg else "Unknown"


def infer_domain(text: str) -> str:
    """Most common registrable domain among the pasted cookies (so the domain field can stay empty)."""
    from collections import Counter
    from .cookie_manager import parse_netscape
    c = Counter(registrable(r[0].lstrip(".").lower()) for r in parse_netscape(text))
    return c.most_common(1)[0][0] if c else ""


def host_of(url: str) -> str:
    try:
        h = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower().strip(".")
    except ValueError:
        return ""
    return h


def _matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _row_to_entry(r: tuple) -> dict[str, Any]:
    keys = ("domain", "label", "aliases", "enabled", "data", "count", "logged_in", "expires_at", "created_at",
            "updated_at", "check_valid", "check_at", "check_detail", "test_url")
    e = dict(zip(keys, r))
    e["enabled"] = bool(e["enabled"])
    e["logged_in"] = bool(e["logged_in"])
    e["aliases"] = [a for a in (e["aliases"] or "").split(",") if a]
    try:
        e["text"] = db.unseal(e.pop("data")) if e.get("data") else ""
    except db.DbError as exc:
        log.warning("cookie entry %s unreadable: %s", e["domain"], exc)
        e["text"], e["data"] = "", None
        e["check_detail"] = str(exc)
    e.pop("data", None)
    return e


_COLS = ("domain, label, aliases, enabled, data, count, logged_in, expires_at, created_at, updated_at, "
         "check_valid, check_at, check_detail, test_url")


def load(force: bool = False) -> bool:
    """(Re)load all entries from the database into memory. Returns False if the database is unreachable."""
    global _loaded
    try:
        rows = db.one(f"SELECT {_COLS} FROM cookies")
    except db.DbError as exc:
        log.warning("cookie store: database unavailable (%s); using in-memory copy", exc)
        return False
    with _lock:
        _cache.clear()
        for r in rows:
            e = _row_to_entry(r)
            _cache[e["domain"]] = e
        _loaded = True
    log.info("check ok  cookie store · %d platform(s) loaded", len(_cache))
    return True


def _ensure() -> None:
    if not _loaded:
        load()


def public(e: dict[str, Any]) -> dict[str, Any]:
    """Entry without cookie values (safe for the admin API)."""
    out = {k: v for k, v in e.items() if k != "text"}
    out["aliases"] = list(e.get("aliases") or [])
    out["has_data"] = bool(e.get("text"))
    return out


def list_entries() -> list[dict[str, Any]]:
    _ensure()
    with _lock:
        return [public(e) for e in sorted(_cache.values(), key=lambda x: x["domain"])]


def get(domain: str) -> Optional[dict[str, Any]]:
    _ensure()
    with _lock:
        e = _cache.get(domain)
        return dict(e) if e else None


def match(url: str, enabled_only: bool = True) -> Optional[dict[str, Any]]:
    """Entry whose domain/aliases cover the URL's host (longest domain wins)."""
    _ensure()
    host = host_of(url)
    if not host:
        return None
    best: Optional[dict[str, Any]] = None
    with _lock:
        for e in _cache.values():
            if enabled_only and (not e["enabled"] or not e.get("text")):
                continue
            names = [e["domain"], *e["aliases"]]
            if any(_matches(host, n) for n in names):
                if best is None or len(e["domain"]) > len(best["domain"]):
                    best = e
    return dict(best) if best else None


def _summary(text: str) -> dict[str, Any]:
    from .cookie_manager import inspect_rows, parse_netscape, _dedupe
    rows = _dedupe(parse_netscape(text))
    if not rows:
        raise ValueError("No cookies found. Paste a Netscape cookies.txt export (7 tab-separated columns per line).")
    info = inspect_rows(rows)
    exps = [int(r[4]) for r in rows if int(r[4]) > 0]
    return {"rows": rows, "count": len(rows), "expires_at": min(exps) if exps else None,
            "logged_in": bool(info["logged_in_cookies"]) or len(rows) > 1}


def save(domain: str, text: str, label: str = "", aliases: Optional[list[str]] = None, enabled: bool = True,
         test_url: str = "", merge: bool = False) -> dict[str, Any]:
    """Create or replace (or merge into) a platform's cookies."""
    from .cookie_manager import _render, parse_netscape, _dedupe
    text = (text or "").strip().strip("\"'`").strip()
    if not (domain or "").strip():
        domain = infer_domain(text)
        if not domain:
            raise ValueError("Could not detect the platform from the cookies. Enter the main domain.")
    domain = norm_domain(domain)
    if len(text) > 2 * 1024 * 1024:
        raise ValueError("Cookie text is too large (max 2 MB).")
    _ensure()
    old = get(domain)
    if merge and old and old.get("text"):
        text = _render(_dedupe(parse_netscape(old["text"]) + parse_netscape(text)))
    s = _summary(text)
    body = _render(s["rows"])
    if aliases is None:
        aliases = old["aliases"] if old else DEFAULT_ALIASES.get(domain, [])
    aliases = [norm_domain(a) for a in aliases if a and a.strip()]
    now = int(time.time())
    db.one("""INSERT INTO cookies (domain,label,aliases,enabled,data,count,logged_in,expires_at,created_at,updated_at,test_url)
              VALUES (?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(domain) DO UPDATE SET label=excluded.label, aliases=excluded.aliases, enabled=excluded.enabled,
              data=excluded.data, count=excluded.count, logged_in=excluded.logged_in, expires_at=excluded.expires_at,
              updated_at=excluded.updated_at, test_url=excluded.test_url, check_valid=NULL, check_at=NULL, check_detail=NULL""",
           (domain, (label or "").strip() or (old or {}).get("label") or platform_name(domain), ",".join(aliases), int(enabled), db.seal(body), s["count"],
            int(s["logged_in"]), s["expires_at"], (old or {}).get("created_at") or now, now,
            test_url or (old or {}).get("test_url") or ""))
    load()
    return public(get(domain) or {})


def update_meta(domain: str, **fields: Any) -> dict[str, Any]:
    """Change label / aliases / enabled / test_url without touching the cookies."""
    _ensure()
    e = get(domain)
    if not e:
        raise KeyError(domain)
    label = fields.get("label", e["label"])
    aliases = fields.get("aliases", e["aliases"])
    aliases = [norm_domain(a) for a in aliases if a and a.strip()]
    enabled = bool(fields.get("enabled", e["enabled"]))
    test_url = fields.get("test_url", e.get("test_url") or "")
    db.one("UPDATE cookies SET label=?, aliases=?, enabled=?, test_url=?, updated_at=? WHERE domain=?",
           (label, ",".join(aliases), int(enabled), test_url, int(time.time()), domain))
    load()
    return public(get(domain) or {})


def delete(domain: str) -> bool:
    _ensure()
    db.one("DELETE FROM cookies WHERE domain = ?", (domain,))
    load()
    return True


def record_check(domain: str, valid: Optional[bool], detail: str) -> None:
    try:
        db.one("UPDATE cookies SET check_valid=?, check_at=?, check_detail=? WHERE domain=?",
               (None if valid is None else int(valid), int(time.time()), detail[:300], domain))
        with _lock:
            if domain in _cache:
                _cache[domain].update(check_valid=valid, check_at=int(time.time()), check_detail=detail[:300])
    except db.DbError:
        pass


def writeback(domain: str, text: str) -> None:
    """Persist cookies refreshed by yt-dlp during a request (best effort, never raises)."""
    from .cookie_manager import _render, parse_netscape, _dedupe, inspect_rows
    try:
        e = get(domain)
        if not e:
            return
        rows = _dedupe(parse_netscape(text))
        old = _dedupe(parse_netscape(e.get("text") or ""))
        if not rows or sorted(rows) == sorted(old) or len(rows) < max(1, int(len(old) * 0.8)):
            return
        s = _summary(_render(rows))
        db.one("UPDATE cookies SET data=?, count=?, expires_at=?, updated_at=? WHERE domain=?",
               (db.seal(_render(rows)), s["count"], s["expires_at"], int(time.time()), domain))
        with _lock:
            if domain in _cache:
                _cache[domain].update(text=_render(rows), count=s["count"], expires_at=s["expires_at"])
        log.info("cookies: saved refreshed %s cookies to the database (%d)", domain, len(rows))
    except Exception as exc:  # noqa: BLE001
        log.warning("cookies: could not save refreshed cookies (%s)", exc.__class__.__name__)


_LOGGED_IN = re.compile(r'"LOGGED_IN"\s*:\s*(true|false)', re.I)
_LOGIN_HINT = re.compile(r"(sign[\s_-]?in|log[\s_-]?in|login)", re.I)


def check(domain: str, test_url: str = "") -> dict[str, Any]:
    """Blocking test of a saved entry. With a test URL (a video/page you can access) yt-dlp extracts it with these cookies;
    otherwise the site is fetched with the cookies and inspected. Never returns cookie values."""
    import os
    import tempfile
    from pathlib import Path
    from .cookie_manager import inspect_rows, parse_netscape
    from .extractor_config import get_config
    e = get(domain)
    if not e or not e.get("text"):
        raise KeyError(domain)
    rows = parse_netscape(e["text"])
    info = inspect_rows(rows)
    res: dict[str, Any] = {"valid": None, "detail": "", "count": len(rows)}
    test_url = (test_url or e.get("test_url") or "").strip()
    if info["expired"]:
        res.update(valid=False, detail="login cookies have expired")
    elif test_url:
        try:
            import yt_dlp
            with tempfile.TemporaryDirectory(prefix="downflow-ck-") as d:
                f = Path(d) / "c.txt"
                f.write_text(e["text"], encoding="utf-8")
                os.chmod(f, 0o600)
                opts = {"quiet": True, "no_warnings": True, "skip_download": True, "cookiefile": str(f),
                        "socket_timeout": 20, "noplaylist": True, "extract_flat": False}
                proxy = get_config().proxy
                if proxy:
                    opts["proxy"] = proxy
                with yt_dlp.YoutubeDL(opts) as ydl:
                    data = ydl.extract_info(test_url, download=False)
                title = (data or {}).get("title") or "ok"
                res.update(valid=True, detail=f"extraction worked with these cookies ({str(title)[:60]})")
        except Exception as exc:  # noqa: BLE001
            msg = re.sub(r"\s+", " ", str(exc))[:200]
            res.update(valid=False, detail=f"extraction failed: {msg}")
    else:
        try:
            try:
                from curl_cffi import requests as http
                kw: dict[str, Any] = {"impersonate": "chrome"}
            except Exception:  # noqa: BLE001
                import requests as http  # type: ignore
                kw = {}
            jar = "; ".join(f"{r[5]}={r[6]}" for r in rows if domain.endswith(r[0].lstrip(".").lower()) or r[0].lstrip(".").lower().endswith(domain))
            # Request the www host directly: curl drops a manual Cookie header when a redirect changes the host.
            resp = http.get(f"https://www.{domain}/", timeout=20, allow_redirects=True,
                            headers={"Cookie": jar, "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}, **kw)
            m = _LOGGED_IN.search(resp.text or "")
            if m:
                res.update(valid=m.group(1).lower() == "true",
                           detail="signed in" if m.group(1).lower() == "true" else "site treats the cookies as signed out")
            elif resp.status_code >= 400:
                res.update(valid=None, detail=f"site answered HTTP {resp.status_code} (cookies look well-formed; add a test URL for a real check)")
            else:
                res.update(valid=None, detail=f"cookies look well-formed ({len(rows)} cookies, site answered HTTP {resp.status_code}); "
                                              "set a test URL (a page/video that needs login) for a real check")
        except Exception as exc:  # noqa: BLE001
            res.update(valid=None, detail=f"check failed: {exc.__class__.__name__}")
    record_check(domain, res["valid"], res["detail"])
    res["checked_at"] = time.time()
    return res
