#!/usr/bin/env bash
# Open Chrome/Chromium with a PERSISTENT profile so you can sign in to YouTube once.
# The profile keeps the session; Downflow (or tools/refresh_cookies.py) can then read cookies from it.
#
#   tools/browser_profile.sh [profile_dir]     (default: ./browser-profile)
#
# --password-store=basic stores cookies without the desktop keyring, which is what yt-dlp's
# "+basictext" keyring option expects. Use the same profile dir in DOWNFLOW_BROWSER_PROFILE_DIR.
set -euo pipefail
DIR="${1:-${DOWNFLOW_BROWSER_PROFILE_DIR:-$(cd "$(dirname "$0")/.." && pwd)/browser-profile}}"
BIN="$(command -v chromium || command -v chromium-browser || command -v google-chrome || command -v chrome || true)"
[ -n "$BIN" ] || { echo "Install chromium or google-chrome first." >&2; exit 1; }
mkdir -p "$DIR"; chmod 700 "$DIR"
echo "Profile: $DIR"
exec "$BIN" --user-data-dir="$DIR" --password-store=basic --no-first-run --no-default-browser-check \
  "${DOWNFLOW_LOGIN_URL:-https://accounts.google.com/ServiceLogin?service=youtube&continue=https://www.youtube.com/}"
