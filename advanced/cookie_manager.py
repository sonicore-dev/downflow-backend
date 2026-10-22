"""Operator-supplied cookie handling.

Cookies are never collected automatically. The operator may configure either:

* ``DOWNFLOW_COOKIE_FILE`` -- a Netscape-format cookies.txt, or
* ``DOWNFLOW_COOKIES_FROM_BROWSER`` -- ``browser[:profile]`` for a browser
  profile on the machine running the server, named explicitly.

yt-dlp rewrites its cookie file on exit, so each extraction works on a private
temporary copy that is deleted afterwards; the original is never modified and
cookie contents are never logged or returned.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterator

from .error_handler import ErrorCode, ExtractionError
from .extractor_config import ExtractorConfig

__all__ = ["cookie_options", "validate_cookie_file", "parse_browser_spec"]

log = logging.getLogger("downflow.cookies")

MAX_COOKIE_FILE_BYTES = 5 * 1024 * 1024
SUPPORTED_BROWSERS = {"brave", "chrome", "chromium", "edge", "firefox", "opera", "safari", "vivaldi", "whale"}
_NETSCAPE_HEADERS = ("# netscape http cookie file", "# http cookie file")


def validate_cookie_file(path_str: str) -> Path:
    """Validate a configured cookie file path; raise a sanitised error if unusable."""
    path = Path(path_str).expanduser()

    def fail(reason: str) -> ExtractionError:
        # The path is logged server-side only; the client message is generic.
        return ExtractionError(ErrorCode.CONFIG_ERROR, "The configured cookie file is missing or invalid.",
                               detail=f"cookie file problem: {reason}")

    try:
        if not path.is_file():
            raise fail("not a file / does not exist")
        size = path.stat().st_size
        if size == 0 or size > MAX_COOKIE_FILE_BYTES:
            raise fail(f"unexpected size ({size} bytes)")
        if not os.access(path, os.R_OK):
            raise fail("not readable")
        with path.open("r", encoding="utf-8", errors="strict") as fh:
            first = fh.readline().strip().lower()
            if not first.startswith(_NETSCAPE_HEADERS):
                # Header is optional for yt-dlp; require at least one 7-field line instead.
                ok = False
                for line in [first] + [fh.readline() for _ in range(50)]:
                    line = line.strip()
                    if line and not line.startswith("#") and len(line.split("\t")) == 7:
                        ok = True
                        break
                if not ok:
                    raise fail("not in Netscape cookie format")
    except ExtractionError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise fail(exc.__class__.__name__) from exc
    return path


def parse_browser_spec(spec: str) -> tuple:
    """Parse ``browser[:profile]`` into the tuple yt-dlp expects."""
    browser, _, profile = spec.partition(":")
    browser = browser.strip().lower()
    if browser not in SUPPORTED_BROWSERS:
        raise ExtractionError(ErrorCode.CONFIG_ERROR, "The configured cookie browser is not supported.",
                              detail=f"unsupported browser {browser!r}")
    profile = profile.strip()
    return (browser, profile) if profile else (browser,)


@contextlib.contextmanager
def cookie_options(config: ExtractorConfig) -> Iterator[dict[str, Any]]:
    """Yield yt-dlp option fragments for the configured cookie source (if any)."""
    if config.cookie_file and config.cookies_from_browser:
        log.warning("Both cookie file and browser cookies configured; using the cookie file only.")
    if config.cookie_file:
        src = validate_cookie_file(config.cookie_file)
        tmp_dir = tempfile.mkdtemp(prefix="downflow-cookies-")
        try:
            os.chmod(tmp_dir, 0o700)
            copy = Path(tmp_dir) / "cookies.txt"
            shutil.copyfile(src, copy)
            os.chmod(copy, 0o600)
            yield {"cookiefile": str(copy)}
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    elif config.cookies_from_browser:
        yield {"cookiesfrombrowser": parse_browser_spec(config.cookies_from_browser)}
    else:
        yield {}
