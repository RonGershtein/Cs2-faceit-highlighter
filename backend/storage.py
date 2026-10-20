"""Where the app keeps files, how much space each place takes, and clearing the ones that lose nothing for good.

Settings → Storage lists these. Only items marked "clear" can be emptied from the app, and only while nothing is
recording or rendering: the Downloads copies (only files the app named, in the folders it made), the cache of
rendered montage plays, CS2's unpacked demo copies and leftover raw recordings. Clips, montages and matches are
deleted where they are shown (Highlights tab, the match's ✕), never from here.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .config import DATA_DIR, ROOT, load as load_config

# names the app gives the videos it copies to the export folder (record.export_clips, _save_montage)
EXPORT_FILE = re.compile(r"^(\d\d_.+\.mp4|long_\d+clips.*\.mp4|(squad|montage|recap)_.+\.mp4)$")
EXPORT_DIR = re.compile(r"^[A-Za-z0-9_]+_(\d{4}-\d{2}-\d{2})?_[0-9a-f]{8}$|^montages$")   # <map>_<date>_<id8>


def dir_size(p: Path, pattern=None) -> tuple[int, int]:
    """(bytes, files) under p; with `pattern` only the files whose name matches it."""
    total = n = 0
    if not p.exists():
        return 0, 0
    for root, _, files in os.walk(p):
        for f in files:
            if pattern is not None and not pattern.match(f):
                continue
            try:
                total += (Path(root) / f).stat().st_size
                n += 1
            except OSError:
                pass
    return total, n


def export_base(cfg: dict | None = None) -> Path:
    cfg = cfg if cfg is not None else load_config()
    return Path(cfg["export_folder"]) if cfg.get("export_folder") else Path.home() / "Downloads" / "cs2-watcher"


def export_files(base: Path) -> list[Path]:
    """The videos the app copied to the export folder: app-named files inside the per-match and montages folders
    it made. Anything else there (your own files) is not touched. Pure on the file system; unit-tested."""
    out = []
    if not base.is_dir():
        return out
    for d in base.iterdir():
        if d.is_dir() and EXPORT_DIR.match(d.name):
            out += [f for f in d.iterdir() if f.is_file() and EXPORT_FILE.match(f.name)]
    return out


def busy() -> str | None:
    """Why clearing has to wait (CS2 open, a recording or montage running), or None."""
    from . import record
    from .launch import cs2_running
    if record._job and record._job.get("status") in ("preparing", "recording", "encoding"):
        return "a recording is running"
    if record._squad_job and record._squad_job.get("status") == "running":
        return "a montage is being made"
    if record.cinematic_busy():
        return "cinematic clips are being made"
    if cs2_running():
        return "CS2 is open"
    return None


def _raw_dirs() -> list[Path]:
    """Leftover raw recordings (HLAE's video and audio before they became clips), except those still waiting to be
    turned into clips (planned.json, record.recover_recordings)."""
    from . import record
    vids = DATA_DIR / "videos"
    out = []
    for d in (vids.iterdir() if vids.is_dir() else []):
        raw = record.raw_dir_for(d.name)
        if raw.is_dir() and not (raw / record.PLANNED_NAME).exists():
            out.append(raw)
    return out


def _cs2_replays() -> list[Path]:
    from .launch import find_cs2_path
    cs2 = find_cs2_path()
    return sorted((cs2 / "game" / "csgo").glob("replay_*.dem")) if cs2 else []


def report() -> dict:
    """Every place the app keeps files: {"items": [{"id", "label", "path", "bytes", "files", "note", "clear"}],
    "total", "busy"}."""
    from . import cinematic
    cfg = load_config()
    items = []

    def add(id_, label, path, size, note, clear=False):
        items.append({"id": id_, "label": label, "path": str(path), "bytes": size[0], "files": size[1],
                      "note": note, "clear": clear})
    base = export_base(cfg)
    ex = export_files(base)
    add("exports", "Copies in your clips folder", base, (sum(f.stat().st_size for f in ex), len(ex)),
        "Copies of every clip and montage. The app keeps its own, so these can go.", clear=True)
    add("cache", "Rendered montage plays", cinematic.PIECE_CACHE, dir_size(cinematic.PIECE_CACHE),
        f"Reused by the next montage or recap. Clearing only means rendering them again (kept up to "
        f"{cfg.get('render_cache_gb', 20)} GB).", clear=True)
    rep = _cs2_replays()
    add("replays", "Demo copies in the CS2 folder", rep[0].parent if rep else "CS2 folder",
        (sum(f.stat().st_size for f in rep), len(rep)), "Unpacked demos CS2 plays while recording; made again when needed.",
        clear=True)
    raws = _raw_dirs()
    sizes = [dir_size(r) for r in raws]
    add("raw", "Leftover raw recordings", DATA_DIR / "videos", (sum(s[0] for s in sizes), sum(s[1] for s in sizes)),
        "Video and sound of recordings that did not become clips. Normally empty.", clear=True)
    vids = dir_size(DATA_DIR / "videos")
    add("videos", "Clips and montages", DATA_DIR / "videos", (vids[0] - sum(s[0] for s in sizes), vids[1] - sum(s[1] for s in sizes)),
        "Delete them in the Highlights tab (✕, or Select → Delete).")
    add("demos", "Match demos", DATA_DIR / "demos", dir_size(DATA_DIR / "demos"),
        "The app's copy of each demo (needed to record more of it). Removing a match frees it.")
    add("parsed", "Replays and stats", DATA_DIR / "parsed", dir_size(DATA_DIR / "parsed"), "Removed with the match.")
    add("music", "Music, caster lines and photos", DATA_DIR,
        tuple(map(sum, zip(*(dir_size(DATA_DIR / x) for x in ("music", "voice", "players"))))), "Yours: kept.")
    add("tools", "HLAE and FFmpeg", ROOT / "tools", dir_size(ROOT / "tools"), "Needed to record.")
    return {"items": items, "total": sum(i["bytes"] for i in items), "busy": busy()}


def clear(item: str) -> dict:
    """Empty one clearable place; {"freed": bytes, "files": n}. Refuses while busy()."""
    from . import cinematic
    why = busy()
    if why:
        raise RuntimeError(f"not now: {why}")
    freed = n = 0

    def rm(p: Path) -> None:
        nonlocal freed, n
        try:
            size = p.stat().st_size
            p.unlink()
            freed += size
            n += 1
        except OSError:
            pass
    if item == "exports":
        base = export_base()
        for f in export_files(base):
            rm(f)
        for d in base.iterdir() if base.is_dir() else []:   # the app's folders, once empty
            if d.is_dir() and EXPORT_DIR.match(d.name) and not any(d.iterdir()):
                d.rmdir()
    elif item == "cache":
        for f in cinematic.PIECE_CACHE.glob("*") if cinematic.PIECE_CACHE.is_dir() else []:
            if f.is_file():
                rm(f)
    elif item == "replays":
        for f in _cs2_replays():
            rm(f)
    elif item == "raw":
        for r in _raw_dirs():
            b, c = dir_size(r)
            shutil.rmtree(r, ignore_errors=True)
            if not r.exists():
                freed, n = freed + b, n + c
    else:
        raise RuntimeError(f"{item} cannot be cleared from here")
    return {"freed": freed, "files": n}
