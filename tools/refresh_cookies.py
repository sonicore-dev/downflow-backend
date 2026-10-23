#!/usr/bin/env python3
"""Export fresh YouTube cookies from the persistent browser profile into cookies/youtube.txt.

Run it by hand or from cron (close the browser first so its cookie DB is not locked):

    python tools/refresh_cookies.py                      # uses DOWNFLOW_BROWSER_PROFILE_DIR / defaults
    python tools/refresh_cookies.py chromium+basictext:/data/browser-profile

The running service reads the new file on its very next request (hot reload).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from advanced import get_config  # noqa: E402
from advanced.cookie_manager import browser_spec_string, install_cookie_text, parse_browser_spec  # noqa: E402


def main() -> int:
    cfg = get_config()
    if len(sys.argv) > 1:
        spec = sys.argv[1]
    elif cfg.cookies_from_browser:
        spec = cfg.cookies_from_browser
    elif cfg.browser_profile_dir:
        spec = f"{cfg.browser_name}+{cfg.browser_keyring}:{cfg.browser_profile_dir}"
    else:
        print("Set DOWNFLOW_BROWSER_PROFILE_DIR or pass a spec like chromium+basictext:/path/to/profile", file=sys.stderr)
        return 2
    spec = browser_spec_string(parse_browser_spec(spec))
    tmp = ROOT / "cookies" / ".export.tmp"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--skip-download", "--quiet", "--no-warnings",
           "--cookies-from-browser", spec, "--cookies", str(tmp), "https://www.youtube.com/robots.txt"]
    subprocess.run(cmd, check=False)
    if not tmp.is_file() or tmp.stat().st_size == 0:
        print("No cookies exported. Is the profile path right and the browser closed?", file=sys.stderr)
        return 1
    try:
        st = install_cookie_text(tmp.read_text(encoding="utf-8", errors="ignore"), cfg)
    finally:
        tmp.unlink(missing_ok=True)
    print(f"Installed {st.get('count')} cookies; login cookies: {st.get('logged_in_cookies')}; "
          f"expires in {st.get('expires_in_days')} days")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
