"""Start the server and open the browser.  python run.py [--no-browser]"""
from __future__ import annotations

import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from backend.config import load  # noqa: E402

if __name__ == "__main__":
    cfg = load()
    host, port = cfg.get("host", "127.0.0.1"), int(cfg.get("port", 8000))
    url = f"http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}"
    if cfg.get("open_browser", True) and "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"cs2-watcher at {url}")
    uvicorn.run("backend.app:app", host=host, port=port, log_level="info")
