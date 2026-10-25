from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

WEB_DIR = BASE_DIR / "web"
INDEX_FILE = WEB_DIR / "index.html"
UPDATE_CHECK_FILE = WEB_DIR / "update-check.html"
DOWNLOAD_VIDEO_FILE = WEB_DIR / "download_video.html"

DATA_DIR = BASE_DIR / "data"
UPDATES_DATA_FILE = DATA_DIR / "updates-data.json"
INSTANT_SAVE_FILE = WEB_DIR / "instant_save.html"
