"""Download radar images into data/radars. Usage: python scripts/fetch_radars.py [map ...]"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.maps import DEFAULT_MAPS, MAP_DATA, RADAR_BASE_URL  # noqa: E402
from backend.config import RADARS_DIR  # noqa: E402


def fetch(name: str, force: bool = False) -> None:
    RADARS_DIR.mkdir(parents=True, exist_ok=True)
    files = [f"{name}.png"]
    if MAP_DATA.get(name, {}).get("lower"):
        files.append(f"{name}_lower.png")
    for fn in files:
        dst = RADARS_DIR / fn
        if dst.exists() and not force:
            print("exists", fn)
            continue
        url = RADAR_BASE_URL + fn
        print("fetching", url)
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                dst.write_bytes(r.read())
        except Exception as e:  # noqa: BLE001
            print("  FAILED", fn, e)


if __name__ == "__main__":
    names = sys.argv[1:] or DEFAULT_MAPS
    for n in names:
        fetch(n, force="--force" in sys.argv)
