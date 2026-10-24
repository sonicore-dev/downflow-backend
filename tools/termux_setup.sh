#!/data/data/com.termux/files/usr/bin/sh
# One-shot setup for running Downflow on Termux: JS runtime + challenge solver + newest yt-dlp.
set -e
pkg install -y nodejs ffmpeg python
node --version
pip install -U "yt-dlp[default]" yt-dlp-ejs
echo
echo "Now copy your YouTube cookies.txt to: $(cd "$(dirname "$0")/.." && pwd)/cookies/youtube.txt"
echo "Then restart:  python main.py   and check the 'doctor' lines: JavaScript runtime / challenge solver must say ok."
