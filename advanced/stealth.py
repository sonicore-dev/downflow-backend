"""Access strategies: ordered yt-dlp option overlays the engine tries until one gets through.

Most 403 / "sign in to confirm you're not a bot" errors come from the IP's reputation, TLS/HTTP2
fingerprint and the player client in use, not from a missing login. So each strategy varies those:

* browser TLS impersonation (curl_cffi: chrome / safari / firefox / edge)
* YouTube player clients (web_safari HLS, mweb + PO token, tv, android_vr, embedded, ios, ...)
* cookies: tried first, last or never (``DOWNFLOW_COOKIE_POLICY``). Cookie-less routes come first by
  default, which keeps the logged-in account away from datacenter traffic and is also faster.
* header / geo variations as a final fallback

Overlay keys starting with ``_`` are instructions for the engine, not yt-dlp options:
``_cookies`` False = run this strategy without the cookie file.

Private, paid or members-only content still needs real credentials.
"""

from __future__ import annotations

import logging
import random
from typing import Any
from urllib.parse import urlsplit

from . import pot_provider
from .cookie_manager import cookies_active
from .extractor_config import ExtractorConfig
from .header_manager import load_user_agents

__all__ = ["Strategy", "build_strategies", "impersonation_available", "impersonate_target", "youtube_args",
           "is_youtube", "COOKIE_CLIENTS"]

log = logging.getLogger("downflow.stealth")

Strategy = tuple[str, dict[str, Any]]

_YT_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "googlevideo.com")


def is_youtube(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _YT_HOSTS)


def impersonation_available() -> bool:
    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def impersonate_target(name: str) -> Any:
    """Return a yt-dlp ImpersonateTarget for ``name`` (e.g. 'chrome'), or None."""
    if not impersonation_available():
        return None
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        return ImpersonateTarget.from_str(name)
    except Exception:  # noqa: BLE001
        return None


# Player clients that accept logged-in cookies (android_vr / ios / tv_embedded ignore them).
COOKIE_CLIENTS = ["web_safari", "web", "mweb", "tv"]


def youtube_args(config: ExtractorConfig, **kw: Any) -> dict[str, Any]:
    """yt-dlp ``extractor_args``: YouTube player clients, PO token, visitor data and the PO provider URL."""
    args: dict[str, Any] = {"skip": ["translated_subs"], **kw}
    if config.yt_po_token:
        args["po_token"] = list(config.yt_po_token)
    if config.yt_visitor_data:
        args["visitor_data"] = [config.yt_visitor_data]
    return {"extractor_args": {"youtube": args, **pot_provider.extractor_args(config)}}


def _youtube(config: ExtractorConfig, imp, cookies: bool) -> list[Strategy]:
    forced = list(config.yt_player_clients)

    def yt(name: str, clients: list[str], tls: str = "chrome", use_cookies: bool = False, **ykw: Any) -> Strategy:
        ov: dict[str, Any] = {**imp(tls), **youtube_args(config, player_client=clients, **ykw)}
        if not use_cookies:
            ov["_cookies"] = False
        return name, ov

    pot = pot_provider.alive(config)
    plain: list[Strategy] = []
    if forced:
        plain.append(yt("yt-forced", forced, use_cookies=cookies))
    if pot:                                        # PO token server is up: the officially recommended route
        plain.append(yt("yt-mweb-pot", ["mweb", "web_safari"]))
    plain += [
        yt("yt-web_safari", ["web_safari"], "safari"),       # HLS formats, no GVS PO token needed
        yt("yt-tv", ["tv"]),
        # These clients do not need the watch page, so they still work while that page answers 429.
        yt("yt-android_vr-nopage", ["android_vr"], player_skip=["webpage", "configs"]),
        yt("yt-tv-nopage", ["tv"], player_skip=["webpage", "configs"]),
        yt("yt-android_vr", ["android_vr"]),
        yt("yt-embedded", ["web_embedded"]),
    ]
    if pot:
        plain.append(yt("yt-ios-pot", ["ios"], "safari"))
    else:
        plain.append(yt("yt-mweb", ["mweb"]))
    logged_in: list[Strategy] = []
    if cookies and config.cookie_policy != "never":
        logged_in = [
            yt("yt-cookies-safari", ["web_safari", "web"], "safari", use_cookies=True),
            yt("yt-cookies-tv", ["tv", "mweb"], use_cookies=True),
            yt("yt-cookies-embedded", ["web_embedded", "mweb"], use_cookies=True),
        ]
    order = logged_in + plain if config.cookie_policy == "first" else plain + logged_in
    # Different TLS identity and plain headers as the very last resort.
    ua = random.choice(load_user_agents(config.user_agent_file))
    order.append(("yt-rotate", {
        **imp("firefox"), "geo_bypass": True, "_cookies": False,
        "http_headers": {"User-Agent": ua, "Accept-Language": config.accept_language},
        **youtube_args(config, player_client=["web_embedded", "mweb", "tv"]),
    }))
    return order


def _generic(url: str, config: ExtractorConfig, imp) -> list[Strategy]:
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    uas = load_user_agents(config.user_agent_file)
    ref = {"Referer": origin + "/", "Origin": origin}
    return [
        ("chrome", {**imp("chrome")}),
        ("safari", {**imp("safari"), "http_headers": dict(ref)}),
        ("edge", {**imp("edge"), "http_headers": dict(ref)}),
        ("firefox-rotate", {**imp("firefox"), "geo_bypass": True,
                            "http_headers": {**ref, "User-Agent": random.choice(uas),
                                             "Accept-Language": config.accept_language, "Sec-Fetch-Mode": "navigate"}}),
        ("plain", {"geo_bypass": True, "http_headers": {**ref, "User-Agent": random.choice(uas)}}),
    ]


def build_strategies(url: str, config: ExtractorConfig) -> list[Strategy]:
    def imp(name: str) -> dict[str, Any]:
        target = impersonate_target(name)
        return {"impersonate": target} if target is not None else {}

    if is_youtube(url):
        return _youtube(config, imp, cookies_active(config))
    return _generic(url, config, imp)
