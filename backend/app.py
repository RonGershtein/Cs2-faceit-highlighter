"""FastAPI app: JSON API + static frontend."""
from __future__ import annotations

import hashlib
import json
import re
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
    from .launch import cleanup_replays   # CS2-folder copies of matches deleted while CS2 had them open
    threading.Thread(target=cleanup_replays, daemon=True, name="cleanup").start()
    from .record import recover_recordings   # clips CS2 recorded while the app was not watching
    threading.Thread(target=recover_recordings, daemon=True, name="recover").start()
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
    share_email: str | None = None
    share_app_password: str | None = None
    record_encoder: str | None = None
    montage_slow: str | None = None
    montage_swoop: bool | None = None
    montage_outro: bool | None = None
    montage_transition: str | None = None
    cine_grade: str | None = None
    cine_look: bool | None = None


CONFIG_KEYS = ("my_steamid", "cs2_path", "watch_folder", "export_folder", "tick_step", "watch_interval", "share_email",
               "record_encoder", "montage_slow", "montage_swoop", "montage_outro", "montage_transition", "cine_grade",
               "cine_look")
CHOICES = {"montage_slow": ("song", "all", "none"), "montage_transition": ("mixed", "whip", "zoom", "flash", "none"),
           "cine_grade": ("classic", "teal_orange", "dark")}


@app.get("/api/config")
def get_config():
    c = cfgmod.load()
    out = {k: c[k] for k in CONFIG_KEYS}
    out["default_export_folder"] = str(Path.home() / "Downloads" / "cs2-watcher")
    out["share_password_set"] = bool(c.get("share_app_password"))   # the password itself never leaves the PC
    out["auto_encoder"] = _auto_encoder_name()
    return out


def _auto_encoder_name() -> str | None:
    """What record_encoder "auto" was found to use on this PC (data/encoder.json), as shown in Settings."""
    try:
        from .record import ENCODER_NAMES
        j = json.loads((cfgmod.DATA_DIR / "encoder.json").read_text(encoding="utf-8"))
        return ENCODER_NAMES.get(j.get("encoder"))
    except Exception:  # noqa: BLE001
        return None


@app.post("/api/config")
def set_config(body: ConfigIn):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if changes.get("share_app_password") == "":
        changes.pop("share_app_password")   # an empty box keeps the saved password
    for k, ok in CHOICES.items():
        if k in changes and changes[k] not in ok:
            raise HTTPException(400, f"{k} must be one of {', '.join(ok)}")
    c = cfgmod.update(**changes)
    return {**{k: c[k] for k in CONFIG_KEYS}, "share_password_set": bool(c.get("share_app_password"))}


class PickIn(BaseModel):
    start: str | None = None
    title: str | None = None


class ShareIn(BaseModel):
    demo_id: str
    file: str
    url: str = ""


def _share_call(fn, *a):
    from . import share
    try:
        return fn(share, *a)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/share")
def share_info(demo_id: str, file: str):
    """Title and description suggested for YouTube, and the link saved when the video was shared."""
    return _share_call(lambda s: s.video_info(demo_id, file))


@app.post("/api/share/reveal")
def share_reveal(body: ShareIn):
    return _share_call(lambda s: s.reveal(body.demo_id, body.file) or {"ok": True})


@app.post("/api/share/link")
def share_link(body: ShareIn):
    return _share_call(lambda s: {"link": s.save_link(body.demo_id, body.file, body.url)})


@app.post("/api/share/email")
def share_email(body: ShareIn):
    return _share_call(lambda s: {"to": s.email_link(body.demo_id, body.file)})


@app.get("/api/storage")
def storage_report():
    """Every place the app keeps files and how big it is (Settings → Storage)."""
    from . import storage
    return storage.report()


class ClearIn(BaseModel):
    item: str


@app.post("/api/storage/clear")
def storage_clear(body: ClearIn):
    from . import storage
    try:
        return storage.clear(body.item)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


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
    if rec:
        _rec().stamp_match(demo_id)   # the saved clips keep the map / date / score after the demo is gone
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


@app.post("/api/cinematic/cancel")
def cinematic_cancel(all: bool = False):
    """Abort the running cinematic job (kills its ffmpeg); all=true also empties the queue behind it."""
    cleared = _rec().clear_cinematic_queue() if all else 0
    return {**_rec().cancel_cinematic(), "cleared": cleared}


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
    if not rec:
        return {"videos": [], "long": []}
    _rec().recover_recordings([demo_id])
    return _rec().videos_index(demo_id)


@app.post("/api/demos/{demo_id}/videos/export")
def export_videos(demo_id: str):
    """Copy all recorded clips of this demo to the export folder (Downloads by default)."""
    _demo_or_404(demo_id)
    idx = _rec().videos_index(demo_id)
    folder = _rec().export_clips(demo_id, idx.get("videos", []))
    return {"ok": True, "folder": str(folder), "count": len(idx.get("videos", []))}


class CineIn(BaseModel):
    highlight_ids: list[int] | None = None   # None -> every clip without a cinematic version
    settings: dict | None = None             # cine_* options; saved to config.toml and used for this job
    reel: bool = False                       # the 9:16 version (Shorts, Reels, TikTok) instead of the widescreen one


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
    have = "reel" if body.reel else "cine"
    todo = [v for v in idx.get("videos", []) if (body.highlight_ids is None and not v.get(have))
            or (body.highlight_ids is not None and v["highlight_id"] in body.highlight_ids)]
    if not todo:
        raise HTTPException(400, "no clips to process (record a highlight first)")
    if not r.FFMPEG_EXE.exists():
        raise HTTPException(400, "FFmpeg is not installed (Install HLAE + FFmpeg)")
    opts = cine.options(overrides=body.settings or {})
    if body.settings:
        cfgmod.update(**{k: opts[k] for k in cine.OPTION_KEYS if k in body.settings and k not in cine.PER_RUN_KEYS})
    try:
        job = r.start_cinematic(demo_id, todo, opts, reel=body.reel)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "count": job["total"], "queued": job["queued"], "music": [m.name for m in cine.music_files()], "music_dir": str(cine.MUSIC_DIR)}


@app.get("/api/clips")
def saved_clips():
    """Every saved video (clips, cinematic versions, long videos, montages) of every match, including deleted demos."""
    return _rec().saved_clips() if rec else []


@app.delete("/api/clips/{demo_id}/{name}")
def delete_saved_clip(demo_id: str, name: str):
    if not (re.fullmatch(r"[0-9a-f]{8,64}", demo_id) or demo_id == "montages") or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "bad name")
    try:
        return _rec().delete_saved(demo_id, name)
    except _rec().FileBusy as e:
        raise HTTPException(409, str(e))
    except RuntimeError as e:
        raise HTTPException(404, str(e))


class MontageIn(BaseModel):
    clips: list[dict]   # [{"demo_id", "file"}] in play order
    title: str = ""
    cinematic: bool = False        # every play made cinematic, one song over the whole montage (background job)
    music: str | None = None       # that song ("" = random); not saved as the default


@app.post("/api/clips/montage")
def make_montage(body: MontageIn):
    """Join saved clips of any matches into one video (synchronous; a few seconds to a few minutes). cinematic=True
    starts a background job instead; poll /api/squad/status (demo_id "montages")."""
    try:
        if body.cinematic:
            from . import cinematic as cine
            overrides = cine.options(overrides={"cine_music": body.music}) if body.music is not None else None
            return _rec().start_library_montage(body.clips, body.title, overrides)
        return _rec().compile_montage(body.clips, body.title)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


class RecapIn(BaseModel):
    date_from: str                 # YYYY-MM-DD, both days included
    date_to: str
    min_kills: int = 3
    best_of: int | None = None     # keep the N biggest plays
    title: str = ""


@app.get("/api/clips/recap")
def recap_preview(date_from: str, date_to: str, min_kills: int = 3, best_of: int | None = None):
    """What a recap of that period would hold (and the squad plays there that are not recorded yet)."""
    return _rec().recap_preview(date_from, date_to, max(2, min(5, min_kills)), best_of or None)


@app.post("/api/clips/recap")
def make_recap(body: RecapIn):
    """The squad's plays of a period in one video, one song per match; background job, poll /api/squad/status."""
    try:
        return _rec().start_recap(body.date_from, body.date_to, max(2, min(5, body.min_kills)), body.best_of or None, body.title)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


# ---- squad montage ---------------------------------------------------------------------------------

@app.get("/api/squad")
def get_squad():
    return {"members": _rec().load_squad(), **_rec().load_team()}


class SquadIn(BaseModel):
    members: list[dict] | None = None
    team: str | None = None        # the name on the montage intro card
    intro: bool | None = None      # montages start with the intro card


@app.put("/api/squad")
def put_squad(body: SquadIn):
    return {"members": _rec().save_squad(body.members, body.team, body.intro), **_rec().load_team()}


class SquadPlayersIn(BaseModel):
    members: list[dict]            # [{"name", "steamid", "card_name"}]


@app.get("/api/squad/players")
def get_squad_players():
    """The Squad tab: members, the name each one gets on the video's player card, and their photo."""
    return {"members": _rec().squad_players(), **_rec().load_team()}


@app.put("/api/squad/players")
def put_squad_players(body: SquadPlayersIn):
    return {"members": _rec().save_squad_players(body.members), **_rec().load_team()}


@app.post("/api/squad/players/photo")
async def upload_player_photo(file: UploadFile = File(...), steamid: str = Form(""), name: str = Form("")):
    """A member's photo for the player card (the tab sends a square crop)."""
    try:
        f = _rec().save_player_photo(steamid, name, await file.read(), Path(file.filename or "").suffix or ".png")
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"photo": f}


@app.delete("/api/squad/players/photo")
def delete_player_photo(steamid: str = "", name: str = ""):
    _rec().remove_player_photo(steamid, name)
    return {"ok": True}


@app.get("/api/players/photo/{name}")
def player_photo(name: str):
    from . import cinematic as cine
    p = cine.PLAYERS_DIR / Path(name).name
    if not p.is_file() or p.suffix.lower() not in cine.PHOTO_EXT:
        raise HTTPException(404, "no such photo")
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


class MultiSquadIn(BaseModel):
    demo_ids: list[str]
    min_kills: int = 3
    title: str = ""
    cinematic: bool = True         # False: the recorded clips joined as they are, no song
    music: str = ""                # one track for every match; "" = a fresh random track per match
    order: str = ""                # "song": the order that fits the song best, "date": match order, "" = config


@app.post("/api/squad/multi/preview")
def multi_squad_preview(body: MultiSquadIn):
    """Per selected match: the squad plays that would go in, and how many still need recording."""
    return {"matches": _rec().multi_squad_plan(body.demo_ids, max(2, min(5, body.min_kills)))}


@app.post("/api/squad/multi")
def multi_squad(body: MultiSquadIn):
    """One squad montage of several matches (records what is missing first); poll /api/squad/status."""
    try:
        return _rec().start_multi_squad(body.demo_ids, max(2, min(5, body.min_kills)), body.title, body.cinematic, body.music,
                                            body.order)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.get("/api/demos/{demo_id}/squad")
def squad_preview(demo_id: str, min_kills: int = 3):
    """The squad's highlights in this match that would go into the montage."""
    _demo_or_404(demo_id)
    r = _rec()
    r.recover_recordings([demo_id])
    have = {v["highlight_id"]: v for v in r.videos_index(demo_id).get("videos", []) if r.is_recorded(v)}
    hs = r.squad_highlights(db.get_highlights(demo_id), r.load_squad(), min_kills)
    return {"highlights": [{"id": h["id"], "description": h["description"], "player_name": h.get("player_name"),
                            "recorded": h["id"] in have, "cinematic": bool(have.get(h["id"], {}).get("cine"))} for h in hs]}


class SquadReelIn(BaseModel):
    min_kills: int = 3
    cinematic: bool = True
    settings: dict | None = None   # cine_* options from the Cinematic dialog; given -> every clip is remade with them
    highlight_ids: list[int] | None = None   # plays picked on the match page instead of the squad's min_kills+ plays
    music: str | None = None       # song for this montage only ("" = random); not saved as the default


@app.post("/api/demos/{demo_id}/squad")
def squad_reel(demo_id: str, body: SquadReelIn):
    _demo_or_404(demo_id)
    overrides = None
    if body.cinematic and body.settings:
        from . import cinematic as cine
        overrides = cine.options(overrides=body.settings)
        cfgmod.update(**{k: overrides[k] for k in cine.OPTION_KEYS if k in body.settings and k not in cine.PER_RUN_KEYS})
    if body.cinematic and body.music is not None:
        from . import cinematic as cine
        overrides = cine.options(overrides={**(overrides or {}), "cine_music": body.music})
    try:
        return _rec().start_squad_reel(demo_id, max(2, min(5, body.min_kills)), body.cinematic, overrides,
                                       body.highlight_ids)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.get("/api/squad/status")
def squad_status():
    return (_rec().squad_status() if rec else None) or {"status": "idle"}


@app.post("/api/squad/cancel")
def squad_cancel():
    _rec().cancel_squad_reel()
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
    try:
        _rec().delete_long_video(demo_id, name)
    except _rec().FileBusy as e:
        raise HTTPException(409, str(e))
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
    # cut the silence off its ends and find its beats and drops now, so the song editor opens at once and the first
    # video using it does not wait
    threading.Thread(target=_analyze_quietly, args=(dst,), daemon=True, name="song-analysis").start()
    return {"ok": True, "file": name}


def _ffmpeg_or_503() -> Path:
    from .record import FFMPEG_EXE

    if not FFMPEG_EXE.exists():
        raise HTTPException(503, "FFmpeg is not installed yet (Install HLAE + FFmpeg)")
    return FFMPEG_EXE


def _analyze_quietly(path: Path) -> None:
    from . import songs
    from .record import FFMPEG_EXE

    try:
        if FFMPEG_EXE.exists():
            songs.trim_silence(FFMPEG_EXE, path)   # analyses it on the way
            songs.analyze(FFMPEG_EXE, path)
    except Exception:  # noqa: BLE001
        pass   # the song editor or the next video analyses it again and reports what is wrong


def _song_path(name: str) -> Path:
    from . import cinematic as cine

    p = cine.MUSIC_DIR / Path(name).name
    if p.suffix.lower() not in cine.MUSIC_EXT or not p.is_file():
        raise HTTPException(404, "no such song")
    return p


@app.delete("/api/music/{name}")
def delete_music(name: str):
    from . import cinematic as cine, songs

    p = cine.MUSIC_DIR / Path(name).name
    if p.exists():
        p.unlink()
    songs.forget(p.name)
    return {"ok": True}


@app.get("/api/music/{name}/file")
def music_file(name: str):
    """The song itself, for the song editor's player (seekable)."""
    p = _song_path(name)
    return FileResponse(p, headers={"Cache-Control": "no-store"})


@app.get("/api/music/{name}/analysis")
def music_analysis(name: str):
    """What the app found in a song: tempo, beats, bars, drops (and the ones placed by hand), the parts a montage
    plays, and a waveform to draw."""
    from . import songs

    info = songs.analyze(_ffmpeg_or_503(), _song_path(name))
    if not info:
        raise HTTPException(422, "this file could not be read as audio")
    keep = ("duration", "bpm", "beat", "beats", "downbeats", "auto_drops", "auto_strengths", "drops", "strengths",
            "edited", "pieces", "gain_db", "wave", "punch", "random")
    return {"name": Path(name).name, "wave_step": songs.WAVE_STEP, "big_drop": songs.BIG_DROP, **{k: info.get(k) for k in keep}}


class DropsIn(BaseModel):
    drops: list[float] | None = None   # None: back to the detected drops


@app.put("/api/music/{name}/drops")
def music_set_drops(name: str, body: DropsIn):
    from . import songs

    p = _song_path(name)
    if body.drops is not None and len(body.drops) > 64:
        raise HTTPException(400, "too many drops")
    songs.set_marks(p.name, body.drops)
    return music_analysis(p.name)


class RandomIn(BaseModel):
    on: bool


@app.put("/api/music/{name}/random")
def music_set_random(name: str, body: RandomIn):
    """Whether the song may be picked when a video has no song chosen."""
    from . import songs

    p = _song_path(name)
    songs.set_in_random(p.name, body.on)
    return {"ok": True, "random": songs.in_random(p.name)}


@app.get("/api/music/ratings")
def music_ratings():
    """Every song's punch (how hard it hits, 0..1, from its cached analysis; None when not analysed yet) and
    whether it is in random picks, for the song list."""
    from . import cinematic as cine, songs

    out = {}
    for f in cine.music_files():
        info = None
        try:
            from .record import FFMPEG_EXE
            info = songs.analyze(FFMPEG_EXE, f) if FFMPEG_EXE.exists() else None
        except Exception:  # noqa: BLE001
            info = None
        out[f.name] = {"punch": (info or {}).get("punch"), "random": songs.in_random(f.name)}
    return out


# ---- sound effects (the hit, the whoosh and the riser of the cinematic edits) --------------------------------

@app.get("/api/sfx")
def list_sfx():
    from . import sfx

    return sfx.listing()


@app.post("/api/sfx/{slot}")
async def upload_sfx(slot: str, file: UploadFile = File(...)):
    from . import sfx

    data = await file.read()
    if len(data) > 30 * 1024 * 1024:
        raise HTTPException(400, "a sound effect should be a few seconds long (this file is over 30 MB)")
    try:
        name = sfx.save(slot, Path(file.filename or "effect").name, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "file": name, **sfx.listing()}


@app.delete("/api/sfx/{slot}")
def reset_sfx(slot: str):
    from . import sfx

    if slot not in sfx.SLOTS:
        raise HTTPException(404, "no such slot")
    sfx.reset(slot)
    return sfx.listing()


@app.get("/api/sfx/{slot}/file")
def sfx_file(slot: str):
    """The effect a slot uses now (the user's file or the built-in one), for the preview button."""
    from . import sfx

    if slot not in sfx.SLOTS:
        raise HTTPException(404, "no such slot")
    return FileResponse(sfx.file_for(slot), headers={"Cache-Control": "no-store"})


# ---- voice lines (sound bites for cinematic clips) --------------------------------------------

@app.get("/api/voice")
def list_voice():
    from . import voice

    return voice.listing()


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
