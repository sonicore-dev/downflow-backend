"""Cookie-free access strategies.

Most 403 / "sign in to confirm you're not a bot" errors come from TLS and
client fingerprinting, not real logins. Each strategy below is an option
overlay for yt-dlp; the engine tries them in order until one works:

1. browser   - impersonate a real Chrome TLS/HTTP2 fingerprint (needs curl_cffi)
2. clients   - alternate YouTube player clients that don't need sign-in
3. mobile    - mobile/embedded clients + Safari fingerprint
4. rotate    - random user-agent, same-site Referer/Origin, geo-bypass

Private, paid or members-only content still needs real credentials.
"""

from __future__ import annotations

import logging
import random
from typing import Any
from urllib.parse import urlsplit

from .extractor_config import ExtractorConfig
from .header_manager import load_user_agents
from .cookie_manager import cookies_active

__all__ = ["Strategy", "build_strategies", "impersonation_available", "impersonate_target", "youtube_args", "COOKIE_CLIENTS"]

log = logging.getLogger("downflow.stealth")

Strategy = tuple[str, dict[str, Any]]


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
    """yt-dlp ``extractor_args`` for YouTube: player clients, PO token and visitor data from the config."""
    args: dict[str, Any] = {"skip": ["translated_subs"], **kw}
    if config.yt_po_token:
        args["po_token"] = list(config.yt_po_token)
    if config.yt_visitor_data:
        args["visitor_data"] = [config.yt_visitor_data]
    return {"extractor_args": {"youtube": args}}


def build_strategies(url: str, config: ExtractorConfig) -> list[Strategy]:
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    ua = random.choice(load_user_agents(config.user_agent_file))

    def imp(name: str) -> dict[str, Any]:
        target = impersonate_target(name)
        return {"impersonate": target} if target is not None else {}

    cookies = cookies_active(config)
    forced = list(config.yt_player_clients)
    clients = forced or (COOKIE_CLIENTS if cookies else ["tv", "web_safari", "mweb"])
    mobile = ["tv", "mweb", "web_embedded"] if cookies else ["android_vr", "ios", "tv_embedded"]
    _yt = lambda **kw: youtube_args(config, **kw)  # noqa: E731

    return [
        ("browser", {**imp("chrome"), **_yt(**({"player_client": forced} if forced else {}))}),
        ("clients", {**imp("chrome"), **_yt(player_client=clients)}),
        ("mobile", {**imp("safari"), **_yt(player_client=forced or mobile)}),
        ("rotate", {
            **imp("firefox"),
            "geo_bypass": True,
            "http_headers": {"User-Agent": ua, "Referer": origin + "/", "Origin": origin,
                             "Accept-Language": config.accept_language, "Sec-Fetch-Mode": "navigate"},
            **_yt(player_client=forced or ["web_embedded", "mweb"]),
        }),
    ]
