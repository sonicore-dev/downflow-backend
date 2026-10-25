"""Instant Save: one call that finds the single best format for a link and returns it ready to download.

No format picker, no playlist walking, no full format table: the selector asks yt-dlp for exactly one
file (best single file first so nothing has to be merged, merge only when the site offers nothing else).
Up to ``instant_attempts`` tries share one time budget; every retry asks the strategy engine for a fresh
route / proxy session, and pointless retries (an IP block from the only IP we have) are skipped so a
blocked address is not hammered.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Optional

from advanced import ErrorCode, ExtractionError, ExtractorConfig, proxy_manager
from engine.metadata_extractor import extract

__all__ = ["instant_extract", "summarise", "KINDS"]

log = logging.getLogger("downflow.instant")

KINDS = ("video", "audio")
_TRANSIENT = {ErrorCode.RATE_LIMITED, ErrorCode.TIMEOUT, ErrorCode.NETWORK_ERROR, ErrorCode.BUSY,
              ErrorCode.NO_MEDIA_URL, ErrorCode.NO_FORMATS, ErrorCode.FORMAT_NOT_AVAILABLE,
              ErrorCode.EXTRACTOR_OUTDATED, ErrorCode.INTERNAL_ERROR}
_NEEDS_ANOTHER_EXIT = {ErrorCode.IP_BLOCKED, ErrorCode.AUTH_REQUIRED, ErrorCode.COOKIES_INVALID}


def _has_streams(result: dict[str, Any]) -> bool:
    return bool(((result.get("media") or {}).get("streams")) or result.get("formats"))


def instant_extract(url: str, kind: str, config: ExtractorConfig, *, attempt0: int = 0,
                    cancelled: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
    """Blocking. Returns the extraction result for the best ``kind`` ("video" | "audio") or raises."""
    selector = config.instant_audio_format if kind == "audio" else config.instant_video_format
    pool = proxy_manager.pool(config)
    multi_exit = len(pool) > 1 or any(proxy_manager.is_templated(p) for p in pool)
    deadline = time.monotonic() + config.instant_budget
    last: Optional[ExtractionError] = None
    for i in range(config.instant_attempts):
        remaining = deadline - time.monotonic()
        if remaining < 4:
            break
        try:
            result = extract(url, format=selector, single=True, config=config, timeout=remaining,
                             attempt=attempt0 + i, cancelled=cancelled)
            if not _has_streams(result):          # playlist / channel page / no media
                raise ExtractionError(ErrorCode.NO_MEDIA_URL,
                                      "This link has no single video. Paste the link of one video.")
            if i:
                log.info("ok    instant succeeded on attempt %d", i + 1)
            return result
        except ExtractionError as err:
            last = err
            retry = err.code in _TRANSIENT or (multi_exit and err.code in _NEEDS_ANOTHER_EXIT)
            if err.code is ErrorCode.NO_MEDIA_URL and err.message.startswith("This link has no single"):
                retry = False
            if not retry or i == config.instant_attempts - 1:
                raise
            pause = min(0.35 * (i + 1) + random.random() * 0.25, max(0.0, remaining / 4))
            log.warning("retry instant %d/%d after %s in %.1fs", i + 2, config.instant_attempts, err.code.value, pause)
            time.sleep(pause)
    raise last or ExtractionError(ErrorCode.TIMEOUT)


def summarise(result: dict[str, Any], kind: str) -> dict[str, Any]:
    """Small, UI-friendly description of what will be downloaded."""
    streams = (result.get("media") or {}).get("streams") or []
    video = next((s for s in streams if s.get("role") in ("video", "combined")), None)
    ext = (streams[0].get("ext") if len(streams) == 1 else None) or ("m4a" if kind == "audio" else "mp4")
    return {
        "title": result.get("title"),
        "uploader": result.get("uploader"),
        "duration": result.get("duration"),
        "thumbnail": result.get("thumbnail"),
        "platform": (result.get("platform") or {}).get("name"),
        "kind": kind,
        "ext": ext,
        "resolution": (video or {}).get("resolution") if kind == "video" else None,
        "merged": len(streams) > 1,
    }
