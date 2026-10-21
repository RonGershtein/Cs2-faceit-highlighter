"""Check that everything the app needs is in place. Usage: python scripts/doctor.py"""
from __future__ import annotations

import importlib
import platform
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OK, WARN, BAD = "[ok]  ", "[warn]", "[FAIL]"
problems = 0


def report(level: str, msg: str) -> None:
    global problems
    if level == BAD:
        problems += 1
    print(level, msg)


def main() -> int:
    v = sys.version_info
    report(OK if (3, 11) <= v[:2] <= (3, 13) else BAD, f"Python {platform.python_version()} ({sys.executable})")
    if v[:2] > (3, 13):
        print("        demoparser2 may not have wheels for this version; use Python 3.11 or 3.12")
    for mod in ("demoparser2", "zstandard", "fastapi", "uvicorn", "pandas", "multipart"):
        try:
            importlib.import_module(mod)
            report(OK, f"module {mod}")
        except Exception as e:  # noqa: BLE001
            report(BAD, f"module {mod}: {e}  -> run: make install  (or scripts\\cs2w install)")

    from backend import config as cfgmod
    from backend.maps import DEFAULT_MAPS

    cfg = cfgmod.load()
    report(OK, f"config {cfgmod.CONFIG_PATH}")
    missing = [m for m in DEFAULT_MAPS if not (cfgmod.RADARS_DIR / f"{m}.png").exists()]
    report(OK if not missing else WARN, f"radar images: {len(DEFAULT_MAPS) - len(missing)}/{len(DEFAULT_MAPS)}"
           + (f" (missing {', '.join(missing)}; run scripts/fetch_radars.py)" if missing else ""))
    wf = cfg.get("watch_folder")
    report(OK if wf and Path(wf).is_dir() else WARN, f"watch folder: {wf or 'not set (Settings > Demo download folder)'}")
    report(OK if cfg.get("my_steamid") else WARN, f"my_steamid: {cfg.get('my_steamid') or 'not set (pick yourself in a match)'}")

    if platform.system() == "Windows":
        from backend import record
        from backend.launch import find_cs2_path

        cs2 = find_cs2_path()
        report(OK if cs2 else WARN, f"CS2 install: {cs2 or 'not found (set cs2_path in Settings)'}")
        st = record.tools_status()
        report(OK if st["hlae"] else WARN, "HLAE " + ("installed" if st["hlae"] else "missing (make tools / scripts\\cs2w tools, or the button in the app)"))
        report(OK if st["ffmpeg"] else WARN, "FFmpeg " + ("installed" if st["ffmpeg"] else "missing (make tools / scripts\\cs2w tools)"))
        if " " in str(cfgmod.ROOT):
            report(WARN, f"project path contains spaces ({cfgmod.ROOT}); recording uses a fallback folder, prefer a path without spaces")
    else:
        report(WARN, f"{platform.system()}: viewer + highlights work; CS2 launch and recording need Windows")

    free_gb = shutil.disk_usage(cfgmod.ROOT).free / 1e9
    report(OK if free_gb > 5 else WARN, f"free disk: {free_gb:.1f} GB (each demo keeps ~250 MB)")
    print()
    print("all good" if problems == 0 else f"{problems} problem(s) to fix")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
