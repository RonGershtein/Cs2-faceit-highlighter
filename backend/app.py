"""FastAPI app: JSON API + static frontend."""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config as cfgmod
from . import db, ingest
from .config import DEMOS_DIR, FRONTEND_DIR, PARSED_DIR, RADARS_DIR
from .maps import MAP_DATA, map_info

_stop = threading.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfgmod.load()
    db.conn()
    ingest.resume_unfinished()
    t = threading.Thread(target=ingest.watch_loop, args=(_stop,), daemon=True, name="watch")
    t.start()
    yield
    _stop.set()


app = FastAPI(title="cs2-watcher", lifespan=lifespan)


@app.middleware("http")
async def no_stale_frontend(request, call_next):
    """Frontend files change on every update; make browsers revalidate instead of serving stale JS/CSS."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _demo_or_404(demo_id: str) -> dict:
    d = db.get_demo(demo_id)
    if not d:
        raise HTTPException(404, "demo not found")
    return d


def _artifact(demo_id: str, name: str) -> Path:
    p = PARSED_DIR / demo_id / name
    if not p.exists():
        raise HTTPException(404, f"{name} not available (status: {(db.get_demo(demo_id) or {}).get('status')})")
    return p


# ---- config ----------------------------------------------------------------------

class ConfigIn(BaseModel):
    my_steamid: str | None = None
    cs2_path: str | None = None
    watch_folder: str | None = None
    export_folder: str | None = None


CONFIG_KEYS = ("my_steamid", "cs2_path", "watch_folder", "export_folder", "tick_step", "watch_interval")


@app.get("/api/config")
def get_config():
    c = cfgmod.load()
    out = {k: c[k] for k in CONFIG_KEYS}
    out["default_export_folder"] = str(Path.home() / "Downloads" / "cs2-watcher")
    return out


@app.post("/api/config")
def set_config(body: ConfigIn):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    c = cfgmod.update(**changes)
    return {k: c[k] for k in CONFIG_KEYS}


class PickIn(BaseModel):
    start: str | None = None
    title: str | None = None


@app.post("/api/pick-folder")
def pick_folder(body: PickIn):
    """Open a native folder dialog on the machine running the server and return the chosen path."""
    import platform
    import shutil
    import subprocess

    title = body.title or "Choose a folder"
    start = body.start or ""
    try:
        if platform.system() == "Windows":
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$f = New-Object System.Windows.Forms.Form; $f.TopMost = $true; "
                "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                f"$d.Description = '{title.replace(chr(39), '')}'; $d.ShowNewFolderButton = $true; "
                + (f"$d.SelectedPath = '{start.replace(chr(39), '')}'; " if start else "")
                + "if ($d.ShowDialog($f) -eq 'OK') { Write-Output $d.SelectedPath }"
            )
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, text=True,
                               timeout=600)
            path = r.stdout.strip()
        elif shutil.which("zenity"):
            r = subprocess.run(["zenity", "--file-selection", "--directory", f"--title={title}"]
                               + ([f"--filename={start}/"] if start else []), capture_output=True, text=True, timeout=600)
            path = r.stdout.strip()
        else:
            raise HTTPException(501, "no folder dialog available on this system; type the path instead")
    except subprocess.TimeoutExpired:
        path = ""
    return {"path": path or None}


# ---- demos ---------------------------------------------------------------------------

@app.post("/api/demos")
async def upload_demo(file: UploadFile = File(...), last_modified: int | None = Form(None)):
    try:
        ext = ingest.demo_ext(file.filename or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    tmp = DEMOS_DIR / f"upload_{uuid.uuid4().hex}{ext}"
    h = hashlib.sha1()
    with open(tmp, "wb") as out:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            out.write(chunk)
    date = None
    if last_modified:
        date = datetime.fromtimestamp(last_modified / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    demo_id = ingest.register_file(tmp, original_name=file.filename, date=date, move=True)
    if tmp.exists():
        tmp.unlink()
    d = db.get_demo(demo_id)
    if d and d["status"] != "done":
        ingest.start_background(demo_id)
    return {"id": demo_id, "status": (d or {}).get("status")}


@app.get("/api/demos")
def list_demos():
    cfg = cfgmod.load()
    me = cfg.get("my_steamid") or None
    out = []
    for d in db.list_demos():
        row = dict(d)
        row["me"] = db.get_player(d["id"], me) if me else None
        row["top_highlight"] = db.top_highlight(d["id"], me) if d["status"] == "done" else None
        if row["top_highlight"] is None and d["status"] == "done":
            row["top_highlight"] = db.top_highlight(d["id"])
        out.append(row)
    return out


@app.get("/api/demos/{demo_id}")
def get_demo(demo_id: str):
    d = _demo_or_404(demo_id)
    meta = None
    mp = PARSED_DIR / demo_id / "meta.json"
    if mp.exists():
        meta = json.loads(mp.read_text(encoding="utf-8"))
    return {"demo": d, "meta": meta, "players": db.get_players(demo_id),
            "map": map_info(d["map"]) if d.get("map") else None}


@app.get("/api/demos/{demo_id}/status")
def demo_status(demo_id: str):
    s = ingest.status(demo_id)
    if s is None:
        raise HTTPException(404, "demo not found")
    return s


@app.get("/api/demos/{demo_id}/frames")
def demo_frames(demo_id: str):
    p = _artifact(demo_id, "frames.json.gz")
    return FileResponse(p, media_type="application/json",
                        headers={"Content-Encoding": "gzip", "Cache-Control": "private, max-age=3600"})


@app.get("/api/demos/{demo_id}/events")
def demo_events(demo_id: str):
    return FileResponse(_artifact(demo_id, "events.json"), media_type="application/json")


@app.get("/api/demos/{demo_id}/meta")
def demo_meta(demo_id: str):
    return FileResponse(_artifact(demo_id, "meta.json"), media_type="application/json")


@app.get("/api/demos/{demo_id}/highlights")
def demo_highlights(demo_id: str, player: str | None = None, kills: bool = True):
    _demo_or_404(demo_id)
    return db.get_highlights(demo_id, player, include_kills=kills)


@app.delete("/api/demos/{demo_id}")
def delete_demo(demo_id: str):
    _demo_or_404(demo_id)
    ingest.delete(demo_id)
    return {"ok": True}


@app.post("/api/demos/{demo_id}/reprocess")
def reprocess(demo_id: str):
    _demo_or_404(demo_id)
    db.set_status(demo_id, "queued", "queued", 0.0)
    ingest.start_background(demo_id)
    return {"ok": True}


class LaunchIn(BaseModel):
    tick: int
    player_name: str | None = None
    player_steamid: str | None = None


@app.post("/api/demos/{demo_id}/launch")
def launch_demo(demo_id: str, body: LaunchIn):
    _demo_or_404(demo_id)
    from .launch import launch

    try:
        return launch(demo_id, body.tick, body.player_name, body.player_steamid)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e))


@app.get("/api/demos/{demo_id}/csdm-sequences")
def csdm_sequences(demo_id: str, player: str | None = None, min_score: float = 2):
    """Export highlights as CS Demo Manager sequences (JSON array of Sequence objects)."""
    d = _demo_or_404(demo_id)
    hs = [h for h in db.get_highlights(demo_id, player, include_kills=False) if h["score"] >= min_score]
    hs.sort(key=lambda h: h["start_tick"])
    seqs = []
    for i, h in enumerate(hs, 1):
        seqs.append({
            "number": i,
            "startTick": h["start_tick"],
            "endTick": h["end_tick"],
            "showXRay": False,
            "showAssists": True,
            "showOnlyDeathNotices": True,
            "playersOptions": [],
            "playerCameras": [{"tick": h["start_tick"], "playerSteamId": str(h["player"]),
                               "playerName": h.get("player_name", "")}],
            "cameras": [],
            "playerVoicesEnabled": False,
            "recordAudio": True,
            "deathNoticesDuration": 5,
        })
    return JSONResponse(seqs, headers={"Content-Disposition": f'attachment; filename="{d["map"]}_{demo_id[:8]}_sequences.json"'})


# ---- recording (Windows + HLAE + FFmpeg) ---------------------------------------------------

try:
    from . import record as rec  # noqa: E402
except ImportError:  # recorder module not installed
    rec = None  # type: ignore[assignment]


def _rec():
    if rec is None:
        raise HTTPException(501, "recording module (backend/record.py) is not installed")
    return rec


class RecordIn(BaseModel):
    highlight_ids: list[int]


@app.get("/api/record/tools")
def record_tools():
    return _rec().tools_status() if rec else {"windows": False, "hlae": False, "ffmpeg": False, "cs2": None, "ready": False, "missing_module": True}


@app.post("/api/record/tools/install")
def record_tools_install():
    return _rec().start_install_tools() or {"stage": "done"}


@app.get("/api/record/status")
def record_status():
    st = (_rec().status() if rec else None) or {"status": "idle"}
    if rec:
        st["cinematic"] = rec.cinematic_status()
    return st


@app.post("/api/record/cancel")
def record_cancel():
    _rec().cancel()
    return {"ok": True}


@app.post("/api/demos/{demo_id}/record")
def record_start(demo_id: str, body: RecordIn):
    _demo_or_404(demo_id)
    try:
        return _rec().start(demo_id, body.highlight_ids)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


@app.get("/api/demos/{demo_id}/videos")
def list_videos(demo_id: str):
    _demo_or_404(demo_id)
    return _rec().videos_index(demo_id) if rec else {"videos": [], "reel": None, "long": []}


@app.post("/api/demos/{demo_id}/videos/export")
def export_videos(demo_id: str):
    """Copy all recorded clips (and the reel) of this demo to the export folder (Downloads by default)."""
    _demo_or_404(demo_id)
    idx = _rec().videos_index(demo_id)
    folder = _rec().export_clips(demo_id, idx.get("videos", []), idx.get("reel"))
    return {"ok": True, "folder": str(folder), "count": len(idx.get("videos", [])) + (1 if idx.get("reel") else 0)}


class CineIn(BaseModel):
    highlight_ids: list[int] | None = None   # None -> every clip without a cinematic version
    settings: dict | None = None             # cine_* options; saved to config.toml and used for this job


@app.get("/api/cinematic/settings")
def cinematic_settings():
    """Current cine_* options plus the music and voice libraries (what the Cinematic dialog shows)."""
    from . import cinematic as cine, voice

    return {"settings": cine.options(), "music": [m.name for m in cine.music_files()], "music_dir": str(cine.MUSIC_DIR),
            "voice": voice.listing(), "font": bool(cine.find_font())}


@app.post("/api/demos/{demo_id}/videos/cinematic")
def make_cinematic_videos(demo_id: str, body: CineIn):
    """Create slow-motion + music versions of recorded clips (runs in the background; poll /videos)."""
    _demo_or_404(demo_id)
    r = _rec()
    from . import cinematic as cine

    idx = r.videos_index(demo_id)
    todo = [v for v in idx.get("videos", []) if (body.highlight_ids is None and not v.get("cine"))
            or (body.highlight_ids is not None and v["highlight_id"] in body.highlight_ids)]
    if not todo:
        raise HTTPException(400, "no clips to process (record a highlight first)")
    if not r.FFMPEG_EXE.exists():
        raise HTTPException(400, "FFmpeg is not installed (Install HLAE + FFmpeg)")
    opts = cine.options(overrides=body.settings or {})
    if body.settings:
        cfgmod.update(**{k: opts[k] for k in cine.OPTION_KEYS if k in body.settings})
    r.start_cinematic(demo_id, todo, opts)
    return {"ok": True, "count": len(todo), "music": [m.name for m in cine.music_files()], "music_dir": str(cine.MUSIC_DIR)}


# ---- full caster: commentary scripts per highlight -------------------------------------------

def _highlight_or_404(demo_id: str, highlight_id: int) -> dict:
    for h in db.get_highlights(demo_id):
        if h["id"] == highlight_id:
            return h
    raise HTTPException(404, "no such highlight")


def _clip_ticks(demo_id: str, h: dict) -> tuple[int, int]:
    """The recorded clip's tick range for a highlight (falls back to the highlight itself)."""
    for v in _rec().videos_index(demo_id).get("videos", []):
        if v["highlight_id"] == h["id"]:
            return int(v["start_tick"]), int(v.get("end_tick") or h["end_tick"])
    return int(h["start_tick"]), int(h["end_tick"])


@app.get("/api/demos/{demo_id}/highlights/{highlight_id}/commentary")
def get_commentary(demo_id: str, highlight_id: int):
    """The saved script for a highlight, or a freshly written one (saved=false)."""
    from . import caster, cinematic as cine

    _demo_or_404(demo_id)
    h = _highlight_or_404(demo_id, highlight_id)
    saved = cine.load_script(demo_id, highlight_id)
    if saved:
        return {"lines": saved, "saved": True, "duo": any(l.get("who") == 2 for l in saved)}
    a, b = _clip_ticks(demo_id, h)
    duo = bool(cfgmod.load().get("cine_tts_voice2"))
    return {"lines": caster.draft(demo_id, h, a, b, duo=duo), "saved": False, "duo": duo}


class CommentaryDraftIn(BaseModel):
    duo: bool = True


@app.post("/api/demos/{demo_id}/highlights/{highlight_id}/commentary/draft")
def draft_commentary(demo_id: str, highlight_id: int, body: CommentaryDraftIn):
    """Write a new script (random phrasing) without saving it."""
    from . import caster

    _demo_or_404(demo_id)
    h = _highlight_or_404(demo_id, highlight_id)
    a, b = _clip_ticks(demo_id, h)
    return {"lines": caster.draft(demo_id, h, a, b, duo=body.duo), "saved": False, "duo": body.duo}


class CommentaryIn(BaseModel):
    lines: list[dict]


@app.put("/api/demos/{demo_id}/highlights/{highlight_id}/commentary")
def save_commentary(demo_id: str, highlight_id: int, body: CommentaryIn):
    from . import caster, cinematic as cine

    _demo_or_404(demo_id)
    _highlight_or_404(demo_id, highlight_id)
    lines = caster.clean_script(body.lines)
    p = cine.script_path(demo_id, highlight_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"lines": lines}, indent=1), encoding="utf-8")
    return {"ok": True, "lines": lines}


@app.delete("/api/demos/{demo_id}/highlights/{highlight_id}/commentary")
def delete_commentary(demo_id: str, highlight_id: int):
    from . import cinematic as cine

    p = cine.script_path(demo_id, highlight_id)
    if p.exists():
        p.unlink()
    return {"ok": True}


class DeleteClipsIn(BaseModel):
    highlight_ids: list[int]
    downloads: bool = False   # also remove the copies in the export (Downloads) folder


@app.post("/api/demos/{demo_id}/videos/delete")
def delete_clips(demo_id: str, body: DeleteClipsIn):
    """Delete the recorded clips of the given highlights (they can be recorded again afterwards)."""
    _demo_or_404(demo_id)
    try:
        return _rec().delete_clips(demo_id, body.highlight_ids, body.downloads)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


class LongIn(BaseModel):
    highlight_ids: list[int] | None = None   # None -> every recorded clip (with cinematic: every cinematic clip)
    cinematic: bool = False


@app.post("/api/demos/{demo_id}/videos/long")
def make_long_video(demo_id: str, body: LongIn):
    """Join the recorded clips of the selected highlights into one long MP4 (synchronous; a few seconds to a minute)."""
    _demo_or_404(demo_id)
    try:
        return _rec().compile_long_video(demo_id, body.highlight_ids, body.cinematic)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


@app.delete("/api/demos/{demo_id}/videos/long/{name}")
def delete_long_video(demo_id: str, name: str):
    _demo_or_404(demo_id)
    if "/" in name or "\\" in name or ".." in name or not name.startswith("long_"):
        raise HTTPException(400, "bad name")
    _rec().delete_long_video(demo_id, name)
    return {"ok": True}


@app.get("/api/music")
def list_music():
    from . import cinematic as cine

    return {"dir": str(cine.MUSIC_DIR), "files": [m.name for m in cine.music_files()]}


@app.post("/api/music")
async def upload_music(file: UploadFile = File(...)):
    from . import cinematic as cine

    name = Path(file.filename or "track").name
    if Path(name).suffix.lower() not in cine.MUSIC_EXT:
        raise HTTPException(400, "unsupported audio format")
    cine.MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    dst = cine.MUSIC_DIR / name
    with open(dst, "wb") as out:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    return {"ok": True, "file": name}


@app.delete("/api/music/{name}")
def delete_music(name: str):
    from . import cinematic as cine

    p = cine.MUSIC_DIR / Path(name).name
    if p.exists():
        p.unlink()
    return {"ok": True}


# ---- voice lines (sound bites for cinematic clips) --------------------------------------------

@app.get("/api/voice")
def list_voice():
    from . import voice

    return voice.listing()


class TtsIn(BaseModel):
    text: str
    slot: str = "final"
    sub: str = ""          # situation sub-folder: highlight type for start/final, kill number for kill
    voice: str = ""        # TTS voice id (see voice.TTS_VOICES); empty -> the saved cine_tts_voice
    fx: bool | None = None  # stadium treatment; None -> the saved cine_tts_fx


@app.post("/api/voice/tts")
def voice_tts(body: TtsIn):
    """Generate a voice line from text with the Windows voice."""
    from . import voice

    if body.slot not in voice.SLOTS:
        raise HTTPException(400, "unknown slot")
    cfg = cfgmod.load()
    vid = body.voice or str(cfg.get("cine_tts_voice") or "")
    fx = bool(cfg.get("cine_tts_fx", True)) if body.fx is None else bool(body.fx)
    changes = {k: v for k, v in (("cine_tts_voice", body.voice or None), ("cine_tts_fx", body.fx)) if v is not None}
    if changes:
        cfgmod.update(**changes)
    try:
        p = voice.tts(body.text, body.slot, sub=body.sub, voice_id=vid, ffmpeg=_rec().FFMPEG_EXE, fx=fx)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    return {"ok": True, "file": (body.sub + "/" if body.sub else "") + p.name, "slot": body.slot}


@app.post("/api/voice/{slot}")
async def upload_voice(slot: str, file: UploadFile = File(...), sub: str = Form("")):
    from . import voice

    if slot not in voice.SLOTS:
        raise HTTPException(400, "unknown slot")
    name = Path(file.filename or "line").name
    if Path(name).suffix.lower() not in voice.VOICE_EXT:
        raise HTTPException(400, "unsupported audio format")
    try:
        dst = voice.slot_dir(slot, sub) / name
    except ValueError as e:
        raise HTTPException(400, str(e))
    with open(dst, "wb") as out:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    return {"ok": True, "file": name}


def _voice_path(slot: str, name: str) -> Path:
    from . import voice

    if slot not in voice.SLOTS:
        raise HTTPException(400, "unknown slot")
    try:
        sub, base = voice.split_name(name)
        return voice.slot_dir(slot, sub) / base
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/voice/{slot}/{name:path}")
def delete_voice(slot: str, name: str):
    p = _voice_path(slot, name)
    if p.exists():
        p.unlink()
    return {"ok": True}


@app.get("/api/voice/{slot}/{name:path}")
def get_voice(slot: str, name: str):
    p = _voice_path(slot, name)
    if not p.exists():
        raise HTTPException(404, "no such file")
    return FileResponse(p)


@app.get("/api/demos/{demo_id}/videos/{name}")
def get_video(demo_id: str, name: str):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "bad name")
    p = _rec().VIDEOS_DIR / demo_id / name
    if not p.exists():
        raise HTTPException(404, "no such video")
    return FileResponse(p, media_type="video/mp4", filename=name if "download" in name else None)


# ---- radars ---------------------------------------------------------------------------

@app.get("/api/radars/{name}.json")
def radar_json(name: str):
    info = map_info(name)
    if not info:
        raise HTTPException(404, "unknown map")
    return info


@app.get("/api/radars/{name}.png")
def radar_png(name: str):
    p = RADARS_DIR / f"{name}.png"
    if not p.exists() and name in MAP_DATA or name.endswith("_lower") and name[:-6] in MAP_DATA:
        try:
            from scripts.fetch_radars import fetch  # lazy: downloads on first use

            fetch(name[:-6] if name.endswith("_lower") else name)
        except Exception:  # noqa: BLE001
            pass
    if not p.exists():
        raise HTTPException(404, "no radar image")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(Path(__file__).resolve().parent.parent / "assets" / "icon.ico")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
