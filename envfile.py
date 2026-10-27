"""Load KEY=VALUE lines from a .env file next to this file into os.environ (real environment variables win)."""

import os
from pathlib import Path


def load() -> None:
    path = Path(__file__).resolve().parent / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        if val[:1] in "\"'" and val[-1:] == val[:1] and len(val) >= 2:
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].strip()
        if key and key not in os.environ and not val.startswith("PASTE_YOUR_"):
            os.environ[key] = val


load()
