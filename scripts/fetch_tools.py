"""Download HLAE + FFmpeg into tools/ (Windows). Usage: python scripts/fetch_tools.py"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import record  # noqa: E402

if __name__ == "__main__":
    st = record.tools_status()
    if st["hlae"] and st["ffmpeg"]:
        print("HLAE and FFmpeg already installed in", record.TOOLS_DIR)
        sys.exit(0)
    if not st["windows"]:
        print("note: HLAE only works on Windows; downloading anyway for later use")
    record._tools_job = {"stage": "starting", "progress": 0}
    import threading

    t = threading.Thread(target=record.install_tools)
    t.start()
    last = ""
    while t.is_alive():
        j = record._tools_job or {}
        line = f"{j.get('stage', '')} {int(100 * j.get('progress', 0))}%"
        if line != last:
            print("\r" + line.ljust(50), end="", flush=True)
            last = line
        time.sleep(0.5)
    print()
    j = record._tools_job
    if j and j.get("stage") == "error":
        print("FAILED:", j.get("error"))
        sys.exit(1)
    st = record.tools_status()
    print("HLAE:", "ok" if st["hlae"] else "missing", "| FFmpeg:", "ok" if st["ffmpeg"] else "missing", "| in", record.TOOLS_DIR)
