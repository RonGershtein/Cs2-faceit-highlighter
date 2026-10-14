"""Ingest: copy/decompress a demo, parse it, write artifacts, detect highlights, store rows.

CLI for testing:  python -m backend.ingest path/to/match.dem.zst
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import zstandard

from . import db
from .config import DEMOS_DIR, PARSED_DIR, load as load_config
from .highlights import detect
from .parser import parse_demo

_active: dict[str, dict] = {}
_active_lock = threading.Lock()


def sha1_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def decompress_zst(src: Path, dst: Path) -> None:
    with open(src, "rb") as f, open(dst, "wb") as o:
        zstandard.ZstdDecompressor().copy_stream(f, o, read_size=1 << 20, write_size=1 << 20)


def stored_demo_path(demo_id: str) -> Path | None:
    for ext in (".dem.zst", ".dem", ".dem.gz", ".dem.bz2"):
        p = DEMOS_DIR / f"{demo_id}{ext}"
        if p.exists():
            return p
    return None


def ensure_dem(demo_id: str, dst: Path | None = None) -> Path:
    """Return a path to a plain .dem for this demo, decompressing if needed."""
    src = stored_demo_path(demo_id)
    if src is None:
        raise FileNotFoundError(demo_id)
    if src.suffix == ".dem":
        if dst and dst != src:
            shutil.copyfile(src, dst)
            return dst
        return src
    dst = dst or (DEMOS_DIR / f"{demo_id}.dem")
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    tmp = dst.with_suffix(".dem.part")
    if src.name.endswith(".zst"):
        decompress_zst(src, tmp)
    elif src.name.endswith(".gz"):
        with gzip.open(src, "rb") as f, open(tmp, "wb") as o:
            shutil.copyfileobj(f, o, 1 << 20)
    elif src.name.endswith(".bz2"):
        import bz2
        with bz2.open(src, "rb") as f, open(tmp, "wb") as o:
            shutil.copyfileobj(f, o, 1 << 20)
    os.replace(tmp, dst)
    return dst


def demo_ext(name: str) -> str:
    low = name.lower()
    for ext in (".dem.zst", ".dem.gz", ".dem.bz2", ".dem"):
        if low.endswith(ext):
            return ext
    raise ValueError(f"unsupported file: {name}")


def register_file(src: Path, original_name: str | None = None, date: str | None = None, move: bool = False) -> str:
    """Copy (or move) a demo into data/demos and create the DB row. Returns demo id."""
    src = Path(src)
    ext = demo_ext(original_name or src.name)
    demo_id = sha1_file(src)[:16]
    dst = DEMOS_DIR / f"{demo_id}{ext}"
    if not dst.exists():
        if move:
            shutil.move(str(src), str(dst))
        else:
            shutil.copyfile(src, dst)
    if date is None:
        try:
            date = datetime.fromtimestamp(src.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
        except OSError:
            date = db.now_iso()
    existing = db.get_demo(demo_id)
    if existing and existing.get("status") == "done":
        return demo_id
    db.upsert_demo(demo_id, filename=original_name or src.name, source_path=str(src), date=date,
                   size=dst.stat().st_size, status="queued", stage="queued", progress=0.0, error=None)
    return demo_id


def write_artifacts(demo_id: str, parsed: dict, highlights: list[dict]) -> Path:
    out = PARSED_DIR / demo_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "meta.json").write_text(json.dumps(parsed["meta"], separators=(",", ":")), encoding="utf-8")
    (out / "events.json").write_text(json.dumps(parsed["events"], separators=(",", ":")), encoding="utf-8")
    (out / "highlights.json").write_text(json.dumps(highlights, separators=(",", ":")), encoding="utf-8")
    (out / "stats.json").write_text(json.dumps(parsed["stats"], separators=(",", ":")), encoding="utf-8")
    tmp = out / "frames.json.gz.part"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        f.write(json.dumps(parsed["frames"], separators=(",", ":")))
    os.replace(tmp, out / "frames.json.gz")
    return out


def process(demo_id: str) -> None:
    """Run the whole pipeline for a registered demo. Safe to call in a thread."""
    cfg = load_config()
    t0 = time.time()

    def progress(stage: str, frac: float) -> None:
        with _active_lock:
            _active[demo_id] = {"stage": stage, "progress": frac}
        db.set_status(demo_id, "processing", stage, frac)

    dem_path: Path | None = None
    try:
        progress("decompress", 0.01)
        src = stored_demo_path(demo_id)
        if src is None:
            raise FileNotFoundError("demo file missing")
        dem_path = ensure_dem(demo_id)
        progress("parse", 0.05)
        parsed = parse_demo(str(dem_path), step=int(cfg.get("tick_step", 4)), progress=progress)
        progress("highlights", 0.86)
        ev = parsed["events"]
        highlights = detect(ev["kills"], parsed["meta"]["rounds"], ev["bomb"], parsed["pos"].pos_at,
                            parsed["meta"]["tickrate"])
        progress("write", 0.9)
        write_artifacts(demo_id, parsed, highlights)
        meta = parsed["meta"]
        db.replace_players(demo_id, parsed["stats"])
        db.replace_highlights(demo_id, highlights)
        db.upsert_demo(demo_id, map=meta["map"], server=meta["server"], tickrate=meta["tickrate"],
                       total_ticks=meta["last_tick"], rounds=len(meta["rounds"]),
                       score_a=meta["score"]["a"], score_b=meta["score"]["b"],
                       team_a=meta["team_a"]["name"], team_b=meta["team_b"]["name"],
                       status="done", stage="done", progress=1.0, error=None)
        print(f"[ingest] {demo_id} {meta['map']} rounds={len(meta['rounds'])} kills={len(ev['kills'])} "
              f"highlights={len(highlights)} in {time.time() - t0:.1f}s")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        db.set_status(demo_id, "error", "error", error=f"{type(e).__name__}: {e}")
    finally:
        with _active_lock:
            _active.pop(demo_id, None)
        if dem_path and not cfg.get("keep_decompressed") and dem_path.suffix == ".dem":
            src = stored_demo_path(demo_id)
            if src is not None and src != dem_path and src.exists():
                try:
                    dem_path.unlink()
                except OSError:
                    pass


def start_background(demo_id: str) -> None:
    with _active_lock:
        if demo_id in _active:
            return
        _active[demo_id] = {"stage": "queued", "progress": 0.0}
    threading.Thread(target=process, args=(demo_id,), daemon=True, name=f"ingest-{demo_id}").start()


def status(demo_id: str) -> dict | None:
    row = db.get_demo(demo_id)
    if row is None:
        return None
    with _active_lock:
        live = _active.get(demo_id)
    return {"id": demo_id, "status": row["status"], "stage": (live or {}).get("stage", row["stage"]),
            "progress": (live or {}).get("progress", row["progress"]), "error": row.get("error")}


def delete(demo_id: str) -> None:
    """Remove the app's copy of the demo, its parsed data and DB rows.
    Keeps: the original file in the watch/download folder and recorded videos in data/videos."""
    db.delete_demo(demo_id)
    try:  # the playback copy inside <CS2>/game/csgo/ (created by "Open in CS2" / recording)
        from .launch import find_cs2_path
        cs2 = find_cs2_path()
        if cs2:
            for p in (cs2 / "game" / "csgo").glob(f"replay_{demo_id[:8]}.dem*"):
                p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    for ext in (".dem.zst", ".dem", ".dem.gz", ".dem.bz2"):
        p = DEMOS_DIR / f"{demo_id}{ext}"
        if p.exists():
            p.unlink()
    shutil.rmtree(PARSED_DIR / demo_id, ignore_errors=True)


# ---- watch folder --------------------------------------------------------------

def scan_watch_folder(folder: str) -> list[str]:
    """Register every new, fully-written demo in the folder. Returns new demo ids."""
    new: list[str] = []
    if not folder:
        return new
    root = Path(folder)
    if not root.is_dir():
        return new
    for p in sorted(root.iterdir()):
        name = p.name.lower()
        if not (name.endswith(".dem") or name.endswith(".dem.zst") or name.endswith(".dem.gz") or name.endswith(".dem.bz2")):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size < 1 << 20:
            continue
        w = db.watched_get(str(p))
        if w and w["size"] == size:
            continue
        # still being downloaded? require size stable for one poll
        if w is None:
            db.watched_set(str(p), size, "")
            continue
        try:
            demo_id = register_file(p)
        except Exception as e:  # noqa: BLE001
            print("[watch] failed", p, e)
            continue
        db.watched_set(str(p), size, demo_id)
        d = db.get_demo(demo_id)
        if d and d.get("status") == "done":
            continue
        start_background(demo_id)
        new.append(demo_id)
    return new


def watch_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        cfg = load_config()
        try:
            scan_watch_folder(cfg.get("watch_folder", ""))
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        stop.wait(float(cfg.get("watch_interval", 15)))


def resume_unfinished() -> None:
    for d in db.list_demos():
        if d["status"] in ("queued", "processing"):
            start_background(d["id"])


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m backend.ingest <demo.dem|demo.dem.zst>")
        sys.exit(1)
    load_config()
    did = register_file(Path(sys.argv[1]))
    print("demo id", did)
    process(did)
    d = db.get_demo(did)
    print(json.dumps({k: d[k] for k in ("map", "rounds", "score_a", "score_b", "status", "error")}, indent=1))
    for p in db.get_players(did):
        print(f"  {p['name']:<20} {p['team_start']:<3} K{p['kills']:>3} D{p['deaths']:>3} A{p['assists']:>3} "
              f"HS{p['headshots']:>3} ADR{p['adr']:>6} KAST{p['kast']:>6} R{p['rating']:>5}")
    for h in db.get_highlights(did, include_kills=False)[:12]:
        print(f"  [{h['score']:>4}] r{h['round']:<2} {h['type']:<12} {h['description']}")
