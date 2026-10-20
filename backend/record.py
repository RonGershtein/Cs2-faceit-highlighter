"""Record highlights to MP4 by driving CS2 under HLAE (Windows only).

Pipeline (same building blocks as CS Demo Manager, but without its server plugin):
  1. make sure a plain .dem is inside <CS2>/game/csgo/
  2. write <CS2>/game/csgo/cfg/cs2watcher_rec.cfg: per highlight an alias + `mirv_cmd addAtTick` entries that
     jump to the highlight, spectate the player, `mirv_streams record start/end`, then `quit` at the end
  3. launch CS2 through HLAE with the demo and `+exec cs2watcher_rec`   -> launch_game(), see below
  4. poll until cs2.exe exits (or timeout); HLAE wrote <raw>/seqN/video.mp4 + take0000/audio.wav
  5. mux audio with FFmpeg into data/videos/<demo>/<n>_<type>_r<round>_<player>.mp4
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import traceback
import urllib.request
import zipfile
from pathlib import Path

from . import db
from .config import DATA_DIR, ROOT, load as load_config
from .ingest import ensure_dem
from .launch import cs2_running, find_cs2_path

TOOLS_DIR = ROOT / "tools"
HLAE_DIR = TOOLS_DIR / "hlae"
HLAE_EXE = HLAE_DIR / "HLAE.exe"
HOOK_DLL = HLAE_DIR / "x64" / "AfxHookSource2.dll"
FFMPEG_DIR = TOOLS_DIR / "ffmpeg"
FFMPEG_EXE = FFMPEG_DIR / "bin" / "ffmpeg.exe"
VIDEOS_DIR = DATA_DIR / "videos"
CFG_NAME = "cs2watcher_rec"
FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n9.0-latest-win64-gpl-shared-9.0.zip"

_lock = threading.Lock()
_job: dict | None = None
_cancel = threading.Event()
_tools_job: dict | None = None


def is_windows() -> bool:
    return platform.system() == "Windows"


def launch_game(demo_path: Path, cfg: dict) -> subprocess.Popen | None:
    """Start CS2 under HLAE's custom loader with the demo and `+exec cs2watcher_rec`.

    Same flags CS Demo Manager uses for demo recording. `-insecure` only disables VAC for this session
    (the game refuses VAC-secured servers until restarted normally from Steam); the user approved this.
    """
    cs2 = find_cs2_path()
    cs2_exe = cs2 / "game" / "bin" / "win64" / "cs2.exe"
    w, h = int(cfg.get("record_width", 1920)), int(cfg.get("record_height", 1080))
    game_args = f'-insecure -novid -condebug +playdemo \\"{demo_path}\\" -sw -width {w} -height {h} +exec {CFG_NAME}'
    cmd = (f'"{HLAE_EXE}" -customLoader -noGui -autoStart -noConfig -afxDisableSteamStorage '
           f'-hookDllPath "{HOOK_DLL}" -programPath "{cs2_exe}" -cmdLine "{game_args}"')
    env = dict(os.environ)
    usr = DATA_DIR / "cs2cfg"  # keep the user's own CS2 config untouched while recording
    usr.mkdir(exist_ok=True)
    env["USRLOCALCSGO"] = str(usr)
    if _job is not None:
        _job["command"] = cmd
    return subprocess.Popen(cmd, cwd=str(HLAE_DIR), env=env)


def tools_status() -> dict:
    cs2 = find_cs2_path() if is_windows() else None
    return {
        "windows": is_windows(),
        "hlae": HLAE_EXE.exists() and HOOK_DLL.exists(),
        "ffmpeg": FFMPEG_EXE.exists(),
        "cs2": str(cs2) if cs2 else None,
        "ready": is_windows() and HLAE_EXE.exists() and HOOK_DLL.exists() and FFMPEG_EXE.exists() and cs2 is not None,
        "installing": _tools_job,
    }


# ---- tools install -------------------------------------------------------------

def _download(url: str, dst: Path, label: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "cs2-watcher"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dst, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if _tools_job is not None:
                _tools_job.update({"stage": label, "progress": (done / total) if total else 0})


def install_tools() -> None:
    global _tools_job
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if not (HLAE_EXE.exists() and HOOK_DLL.exists()):
            api = json.loads(urllib.request.urlopen(
                urllib.request.Request("https://api.github.com/repos/advancedfx/advancedfx/releases/latest",
                                       headers={"User-Agent": "cs2-watcher"}), timeout=60).read())
            asset = next(a for a in api["assets"] if re.match(r"hlae_[\d_]+\.zip$", a["name"]))
            zpath = TOOLS_DIR / "hlae.zip"
            _download(asset["browser_download_url"], zpath, "downloading HLAE")
            shutil.rmtree(HLAE_DIR, ignore_errors=True)
            zipfile.ZipFile(zpath).extractall(HLAE_DIR)
            zpath.unlink()
        if not FFMPEG_EXE.exists():
            zpath = TOOLS_DIR / "ffmpeg.zip"
            _download(FFMPEG_URL, zpath, "downloading FFmpeg")
            z = zipfile.ZipFile(zpath)
            top = z.namelist()[0].split("/")[0]
            z.extractall(TOOLS_DIR)
            z.close()
            shutil.rmtree(FFMPEG_DIR, ignore_errors=True)
            os.rename(TOOLS_DIR / top, FFMPEG_DIR)
            zpath.unlink()
        register_ffmpeg()
        _tools_job = None
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        _tools_job = {"stage": "error", "progress": 0, "error": f"{type(e).__name__}: {e}"}


def register_ffmpeg() -> None:
    """HLAE finds FFmpeg through <hlae>/ffmpeg/ffmpeg.ini."""
    (HLAE_DIR / "ffmpeg").mkdir(parents=True, exist_ok=True)
    (HLAE_DIR / "ffmpeg" / "ffmpeg.ini").write_text(f"[Ffmpeg]\r\nPath={FFMPEG_EXE}\r\n", encoding="utf-8")


def start_install_tools() -> dict:
    global _tools_job
    if _tools_job and _tools_job.get("stage") not in (None, "error"):
        return _tools_job
    _tools_job = {"stage": "starting", "progress": 0}
    threading.Thread(target=install_tools, daemon=True, name="install-tools").start()
    return _tools_job


# ---- job -------------------------------------------------------------------------

def status() -> dict | None:
    return dict(_job) if _job else None


def cancel() -> None:
    """Stop waiting for the game. The user closes CS2 themselves."""
    _cancel.set()


def videos_index(demo_id: str) -> dict:
    p = VIDEOS_DIR / demo_id / "videos.json"
    if p.exists():
        idx = json.loads(p.read_text(encoding="utf-8"))
        idx.setdefault("long", [])
        return idx
    return {"videos": [], "long": []}


def _safe(s: str, n: int = 24) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s)[:n].strip("_") or "x"


def _fwd(p: Path) -> str:
    return str(p).replace("\\", "/")


def raw_dir_for(demo_id: str) -> Path:
    """Where FFmpeg writes video.mp4 (absolute path is fine: it is passed inside {QUOTE}s)."""
    if " " not in str(ROOT):
        return VIDEOS_DIR / demo_id / "raw"
    return Path(os.environ.get("SystemDrive", "C:") + "/cs2watcher_rec") / demo_id


def hlae_rel_name(demo_id: str, n: int) -> str:
    """`mirv_streams record name` cannot be quoted inside an alias and the console cuts "C:/..." at the colon,
    so use a relative name; HLAE resolves it against cs2.exe's folder (<CS2>/game/bin/win64)."""
    return f"cs2watcher/{demo_id[:8]}/seq{n}"


def hlae_take_root(cs2: Path, demo_id: str) -> Path:
    return cs2 / "game" / "bin" / "win64" / "cs2watcher" / demo_id[:8]


def start(demo_id: str, highlight_ids: list[int], opts: dict | None = None) -> dict:
    global _job
    opts = opts or {}
    with _lock:
        if _job and _job["status"] in ("preparing", "recording", "encoding"):
            raise RuntimeError("a recording job is already running")
        ts = tools_status()
        if not ts["ready"]:
            missing = [k for k in ("windows", "hlae", "ffmpeg", "cs2") if not ts[k]]
            raise RuntimeError("recording not available: missing " + ", ".join(missing))
        if cs2_running():
            raise RuntimeError("CS2 is running; close it first")
        hs = [h for h in db.get_highlights(demo_id) if h["id"] in set(highlight_ids)]
        if not hs:
            raise RuntimeError("no highlights selected")
        hs.sort(key=lambda h: h["start_tick"])
        _cancel.clear()
        _job = {"demo_id": demo_id, "status": "preparing", "progress": 0.0, "stage": "preparing", "current": 0,
                "total": len(hs), "started": time.time(), "error": None, "outputs": [], "hint": "",
                "highlight_ids": [h["id"] for h in hs]}
        threading.Thread(target=_run, args=(demo_id, hs, opts), daemon=True, name="record").start()
        return dict(_job)


def _set(**kw) -> None:
    if _job is not None:
        _job.update(kw)


def hud_cvars(mode: str) -> list[str]:
    """What the recording shows of CS2's HUD. "health_ammo": the spectated player's health, armour and ammo
    plus the kill feed; radar, name tags over players and the target id are hidden. "killfeed": the kill feed
    only. Pure function; unit-tested."""
    if mode == "killfeed":
        return ["cl_draw_only_deathnotices 1"]
    return ["cl_draw_only_deathnotices 0", "cl_drawhud_force_radar -1", "cl_drawhud_force_teamid_overhead -1",
            "hud_showtargetid 0", "cl_drawhud_force_deathnotices 1"]


def build_cfg(hs: list[dict], raw_dir: Path, names: dict[str, str], cfg: dict, tickrate: int,
              demo_id_for_cfg: str = "demo") -> tuple[str, list[dict]]:
    """Return (cfg text, sequences). Pure function; unit-tested."""
    fps = int(cfg.get("record_fps", 60))
    crf = int(cfg.get("record_crf", 20))
    lines = [
        f'echo "[cs2watcher] {CFG_NAME} loaded"',
        "sv_cheats 1", "volume 1", "cl_hud_telemetry_frametime_show 0", "cl_hud_telemetry_net_misdelivery_show 0",
        "cl_hud_telemetry_ping_show 0", "cl_hud_telemetry_serverrecvmargin_graph_show 0",
        "cl_trueview_show_status 0", "r_show_build_info 0", "cl_demo_predict 0",
        *hud_cvars(str(cfg.get("record_hud", "health_ammo"))),
        "mirv_deathmsg lifetime 5", "spec_show_xray 0", "mp_display_kill_assists 1",
        "mirv_streams record screen enabled 1", "mirv_streams record startMovieWav 1",
        f"mirv_streams record fps {fps}",
        "mirv_cmd clear", "mirv_cmd enabled 1",
    ]
    seqs = []
    prev_end = 0
    for i, h in enumerate(hs, 1):
        start, end = int(h["start_tick"]), int(h["end_tick"])
        if start < prev_end + tickrate:
            start = prev_end + tickrate
        end = max(end, start + tickrate)
        setup = max(96, start - 2 * tickrate)      # 2 s of pre-roll after the seek so the spectator switch sticks
        jump_from = 100 if i == 1 else prev_end + 16
        folder = raw_dir / f"seq{i}"
        rel = hlae_rel_name(demo_id_for_cfg, i)
        player = names.get(str(h["player"]), h.get("player_name", ""))
        token = player.split()[0] if player else ""
        try:   # Steam3 account id: robust against names CS2 cannot match (spaces, symbols)
            acc = int(h["player"]) - 76561197960265728
        except (TypeError, ValueError, KeyError):
            acc = 0
        spec = (f"spec_mode 1; spec_player_by_accountid {acc}; spec_player {token}" if acc > 0
                else f"spec_mode 1; spec_player {token}")
        name = f"{i:02d}_{_safe(h['type'])}_r{h['round']}_{_safe(player, 16)}"
        preset = f"p{i}"
        lines += [
            f'mirv_streams settings add ffmpeg {preset} "-c:v libx264 -preset veryfast -crf {crf} -pix_fmt yuv420p '
            f'{{QUOTE}}{_fwd(folder)}\\\\video.mp4{{QUOTE}}"',
            f'alias hl_s{i} "mirv_streams record name {rel}; mirv_streams record screen settings {preset}; '
            f'{spec}; echo [cs2watcher] seq {i} ready"',
            f'alias hl_p{i} "{spec}"',
            f"mirv_cmd addAtTick {jump_from} demo_gototick {setup - 1}",
            f"mirv_cmd addAtTick {setup} hl_s{i}",
            # the seek can swallow the first spectator switch (CS2 is still rebuilding entities): repeat it
            f"mirv_cmd addAtTick {setup + tickrate // 2} hl_p{i}",
            f"mirv_cmd addAtTick {max(setup + 1, start - 8)} hl_p{i}",
        ]
        if i == 1:
            # the demo playback panel is open while the demo plays; `demoui` toggles it, so close it once, on a
            # demo tick (straight from the cfg it would fire before the demo has loaded). From PR #1 by slipknot94.
            lines.append(f"mirv_cmd addAtTick {setup} demoui")
        lines += [
            f"mirv_cmd addAtTick {start} mirv_streams record start",
            f"mirv_cmd addAtTick {end} mirv_streams record end",
        ]
        seqs.append({"n": i, "highlight_id": h["id"], "name": name, "folder": folder, "rel": rel, "start": start, "end": end,
                     "type": h["type"], "round": h["round"], "player": player, "description": h["description"],
                     "player_steamid": str(h["player"])})
        prev_end = end
    if cfg.get("record_close_game", True):
        lines.append(f"mirv_cmd addAtTick {prev_end + 2 * tickrate} quit")
    lines.append('echo "[cs2watcher] commands scheduled"')
    return "\r\n".join(lines) + "\r\n", seqs


def find_wavs(folder: Path) -> list[Path]:
    """HLAE normally writes <record name>/take0000/audio.wav; search the whole folder to be safe."""
    return sorted(folder.rglob("*.wav"), key=lambda p: p.stat().st_mtime)


def _tree(folder: Path, limit: int = 60) -> list[str]:
    out = []
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            out.append(f"{p.relative_to(folder)} ({p.stat().st_size} B)")
            if len(out) >= limit:
                break
    return out


def _console_tail(csgo_dir: Path, lines: int = 400) -> list[str]:
    """Relevant lines of game/csgo/console.log (written because of -condebug)."""
    log = csgo_dir / "console.log"
    if not log.exists():
        return []
    try:
        text = log.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    keys = ("cs2watcher", "mirv", "afx", "record", "startmovie", "endmovie", "wav", "demo_gototick", "spec_player",
            "unknown command", "failed", "error")
    out = [ln for ln in text[-4000:] if any(k in ln.lower() for k in keys)]
    return out[-lines:]


def encode_outputs(seqs: list[dict], out_dir: Path, tickrate: int, progress=None) -> list[dict]:
    """Mux HLAE's video.mp4 + take*/audio.wav into final clips. Returns the outputs list."""
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for i, s in enumerate(seqs):
        video = Path(s["folder"]) / "video.mp4"
        if not video.exists():
            continue
        wavs = find_wavs(Path(s["folder"]))
        if not wavs and s.get("take_dir"):
            wavs = find_wavs(Path(s["take_dir"]))
        out = out_dir / f"{s['name']}.mp4"
        args = [str(FFMPEG_EXE), "-y", "-i", str(video)]
        if wavs:
            args += ["-i", str(wavs[-1]), "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-map", "0:v:0",
                     "-map", "1:a:0", "-shortest"]
        else:
            args += ["-c:v", "copy"]
        args += ["-movflags", "+faststart", str(out)]
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            shutil.copyfile(video, out)
        outputs.append({"highlight_id": s["highlight_id"], "file": out.name, "type": s["type"], "round": s["round"],
                        "player": s["player"], "player_steamid": s["player_steamid"],
                        "description": s["description"], "start_tick": s["start"], "end_tick": s["end"],
                        "size": out.stat().st_size, "seconds": round((s["end"] - s["start"]) / tickrate, 1),
                        "audio": bool(wavs)})
        if progress:
            progress((i + 1) / len(seqs), outputs)
    return outputs


def long_video_name(n: int, cinematic: bool, when: float | None = None) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(when))
    return f"long_{n}clips{'_cinematic' if cinematic else ''}_{stamp}.mp4"


def concat_copy_args(files: list[Path], list_file: Path, out: Path) -> list[str]:
    """Stream-copy concat (fast, lossless); only valid when every clip has the same codecs/streams."""
    list_file.write_text("".join(f"file '{f.as_posix()}'\n" for f in files), encoding="utf-8")
    return [str(FFMPEG_EXE), "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy",
            "-movflags", "+faststart", str(out)]


def concat_reencode_args(clips: list[dict], out: Path, width: int, height: int, fps: int, crf: int = 20) -> list[str]:
    """Re-encode concat that tolerates mixed clips (cinematic + plain, missing audio, different sizes).

    clips: [{"path": Path, "audio": bool, "seconds": float}]. Pure function; unit-tested.
    """
    args = [str(FFMPEG_EXE), "-y"]
    graph = []
    labels = []
    n_in = 0
    for k, c in enumerate(clips):
        args += ["-i", str(c["path"])]
        vi = n_in
        n_in += 1
        if c.get("audio", True):
            ai = vi
        else:
            args += ["-f", "lavfi", "-t", f"{max(0.1, float(c.get('seconds') or 1)):.3f}", "-i",
                     "anullsrc=channel_layout=stereo:sample_rate=48000"]
            ai = n_in
            n_in += 1
        graph.append(f"[{vi}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                     f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v{k}]")
        graph.append(f"[{ai}:a]aresample=48000,aformat=channel_layouts=stereo[a{k}]")
        labels.append(f"[v{k}][a{k}]")
    graph.append("".join(labels) + f"concat=n={len(clips)}:v=1:a=1[v][a]")
    args += ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", str(crf), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    return args


def pick_long_clips(index: dict, highlight_ids: list[int] | None, cinematic: bool) -> list[dict]:
    """Recorded clips for the given highlights, in match order; cinematic versions when asked and available.
    highlight_ids None -> every recorded clip (with cinematic=True: only the ones that have a cinematic version)."""
    if highlight_ids is None:
        vids = [v for v in index.get("videos", []) if v.get("cine") or not cinematic]
    else:
        want = set(highlight_ids)
        vids = [v for v in index.get("videos", []) if v["highlight_id"] in want]
    vids = sorted(vids, key=lambda v: v["start_tick"])
    clips = []
    for v in vids:
        use_cine = bool(cinematic and v.get("cine"))
        clips.append({"highlight_id": v["highlight_id"], "file": v["cine"] if use_cine else v["file"], "cine": use_cine,
                      "audio": True if use_cine else bool(v.get("audio", True)),
                      "seconds": float((v.get("cine_info") or {}).get("seconds") or v.get("seconds") or 0) if use_cine
                      else float(v.get("seconds") or 0),
                      "description": v.get("description", ""), "player": v.get("player", "")})
    return clips


def compile_long_video(demo_id: str, highlight_ids: list[int] | None, cinematic: bool = False) -> dict:
    """Join the recorded clips of the selected highlights (None = all) into one MP4 and register it in videos.json."""
    if not FFMPEG_EXE.exists():
        raise RuntimeError("FFmpeg is not installed (Install HLAE + FFmpeg)")
    out_dir = VIDEOS_DIR / demo_id
    index = videos_index(demo_id)
    clips = pick_long_clips(index, highlight_ids, cinematic)
    clips = [c for c in clips if (out_dir / c["file"]).exists()]
    if len(clips) < 2:
        raise RuntimeError("at least two cinematic clips are needed" if highlight_ids is None and cinematic
                           else "select at least two highlights that already have a recorded clip")
    cfg = load_config()
    out = out_dir / long_video_name(len(clips), cinematic)
    files = [out_dir / c["file"] for c in clips]
    uniform = all(c["audio"] for c in clips) and len({c["cine"] for c in clips}) == 1
    ok = False
    if uniform:
        lst = out_dir / "long_list.txt"
        r = subprocess.run(concat_copy_args(files, lst, out), capture_output=True, text=True)
        lst.unlink(missing_ok=True)
        ok = r.returncode == 0 and out.exists() and out.stat().st_size > 0
    if not ok:
        args = concat_reencode_args([{"path": f, "audio": c["audio"], "seconds": c["seconds"]} for f, c in zip(files, clips)],
                                    out, int(cfg.get("record_width", 1920)), int(cfg.get("record_height", 1080)),
                                    int(cfg.get("record_fps", 60)), int(cfg.get("record_crf", 20)))
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError("ffmpeg failed: " + r.stderr[-800:])
    entry = {"file": out.name, "size": out.stat().st_size, "seconds": round(sum(c["seconds"] for c in clips), 1),
             "highlight_ids": [c["highlight_id"] for c in clips], "clips": [c["file"] for c in clips],
             "cinematic": cinematic, "created": time.time(),
             "description": f"{len(clips)} highlights" + (" (cinematic)" if cinematic else "")}
    index = videos_index(demo_id)
    index["long"] = [e for e in index.get("long", []) if (out_dir / e["file"]).exists()] + [entry]
    (out_dir / "videos.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    try:
        entry["exported"] = str(export_clips(demo_id, [entry], cfg))
    except Exception as e:  # noqa: BLE001
        entry["exported"] = f"export failed: {e}"
    return entry


def delete_long_video(demo_id: str, name: str) -> None:
    out_dir = VIDEOS_DIR / demo_id
    index = videos_index(demo_id)
    index["long"] = [e for e in index.get("long", []) if e["file"] != name]
    (out_dir / name).unlink(missing_ok=True)
    (out_dir / "videos.json").write_text(json.dumps(index, indent=1), encoding="utf-8")


def delete_clips(demo_id: str, highlight_ids: list[int], downloads: bool = False) -> dict:
    """Delete the recorded clip (and its cinematic version) of each highlight; optionally the Downloads copies too.

    Long videos built earlier are left alone: they are finished files of their own.
    """
    out_dir = VIDEOS_DIR / demo_id
    index = videos_index(demo_id)
    want = set(highlight_ids)
    gone = [v for v in index.get("videos", []) if v["highlight_id"] in want]
    if not gone:
        raise RuntimeError("none of the selected highlights has a recorded clip")
    names = [n for v in gone for n in (v.get("file"), v.get("cine")) if n]
    removed = 0
    folders = [out_dir] + ([export_folder_for(demo_id)] if downloads else [])
    for folder in folders:
        for n in names:
            f = folder / n
            if f.exists():
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    index["videos"] = [v for v in index.get("videos", []) if v["highlight_id"] not in want]
    (out_dir / "videos.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    return {"deleted": [v["highlight_id"] for v in gone], "files": removed}


def export_folder_for(demo_id: str, cfg: dict | None = None) -> Path:
    """Folder in the user's Downloads (or cfg.export_folder) that receives copies of finished clips."""
    cfg = cfg or load_config()
    base = Path(cfg["export_folder"]) if cfg.get("export_folder") else Path.home() / "Downloads" / "cs2-watcher"
    d = db.get_demo(demo_id) or {}
    date = (d.get("date") or "")[:10]
    return base / f"{d.get('map', 'match')}_{date}_{demo_id[:8]}"


def export_clips(demo_id: str, outputs: list[dict], cfg: dict | None = None) -> Path:
    dst = export_folder_for(demo_id, cfg)
    dst.mkdir(parents=True, exist_ok=True)
    src_dir = VIDEOS_DIR / demo_id
    for name in (o["file"] for o in outputs):
        src = src_dir / name
        if src.exists():
            target = dst / name
            if not target.exists() or target.stat().st_size != src.stat().st_size:
                shutil.copyfile(src, target)
    return dst


def write_index(demo_id: str, outputs: list[dict]) -> dict:
    out_dir = VIDEOS_DIR / demo_id
    index = videos_index(demo_id)
    by_id = {v["highlight_id"]: v for v in index.get("videos", [])}
    for o in outputs:
        by_id[o["highlight_id"]] = o
    index = {"videos": sorted(by_id.values(), key=lambda v: v["start_tick"]), "long": index.get("long", []),
             "updated": time.time(), "export_folder": str(export_folder_for(demo_id)),
             "match": match_info(demo_id) or index.get("match")}
    (out_dir / "videos.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    return index


def match_highlight(v: dict, highlights: list[dict]) -> dict | None:
    """The highlight a recorded clip belongs to after detection ran again: same player and round, and the
    clip's tick range overlapping the highlight's (the most overlap wins, the same type breaks ties)."""
    best, best_key = None, None
    for h in highlights:
        if str(h.get("player")) != str(v.get("player_steamid")) or h.get("round") != v.get("round"):
            continue
        a, b = max(h["start_tick"], int(v.get("start_tick") or 0)), min(h["end_tick"], int(v.get("end_tick") or 0))
        if b <= a:
            continue
        k = (b - a, h.get("type") == v.get("type"))
        if best_key is None or k > best_key:
            best, best_key = h, k
    return best


def relink_videos(demo_id: str, highlights: list[dict]) -> int:
    """Point every recorded clip of a demo at its highlight's new id (and type / description). Returns how many
    clips changed."""
    p = VIDEOS_DIR / demo_id / "videos.json"
    if not p.exists():
        return 0
    index = videos_index(demo_id)
    changed = 0
    for v in index.get("videos", []):
        h = match_highlight(v, highlights)
        if h is None:
            continue
        new = {"highlight_id": h["id"], "type": h["type"], "description": h["description"]}
        if any(v.get(k) != x for k, x in new.items()):
            v.update(new)
            changed += 1
    if changed:
        p.write_text(json.dumps(index, indent=1), encoding="utf-8")
    return changed


# ---- saved clips across all matches (kept when a demo is deleted) ------------------------------------

MATCH_KEYS = ("map", "date", "team_a", "team_b", "score_a", "score_b", "filename")


def match_info(demo_id: str) -> dict | None:
    """The match details the clip gallery shows, from the database (None once the demo is deleted)."""
    d = db.get_demo(demo_id)
    return {k: d.get(k) for k in MATCH_KEYS} if d else None


def stamp_match(demo_id: str) -> None:
    """Copy the match details into data/videos/<demo>/videos.json so the clips keep them after the demo is deleted,
    and each clip its kill timing and highlight ("ctx"), so it can still be made cinematic (slow motion on the
    kills, title, player card) without the match."""
    from . import cinematic as cine
    p = VIDEOS_DIR / demo_id / "videos.json"
    info = match_info(demo_id)
    if not p.exists() or not info:
        return
    index = videos_index(demo_id)
    changed = index.get("match") != info
    index["match"] = info
    for v in index.get("videos", []):
        if (v.get("ctx") or {}).get("highlight_id") == v.get("highlight_id"):
            continue   # already stamped (re-stamped when detection ran again and the clip got a new highlight)
        try:
            ctx = cine.clip_context(demo_id, v["highlight_id"], int(v["start_tick"]), v.get("end_tick"))
        except (OSError, ValueError, KeyError):
            continue
        if ctx.get("highlight"):
            v["ctx"] = {**ctx, "highlight_id": v["highlight_id"]}
            changed = True
    if changed:
        p.write_text(json.dumps(index, indent=1), encoding="utf-8")


def _match_from_folder(export_folder: str) -> dict:
    """Fallback for old indexes without "match": the export folder is named <map>_<YYYY-MM-DD>_<id8>."""
    m = re.match(r"^(.+)_(\d{4}-\d{2}-\d{2})_[0-9a-f]{8}$", Path(str(export_folder or "").replace("\\", "/")).name)
    return {"map": m.group(1), "date": m.group(2)} if m and m.group(1) != "match" else {}


def saved_clips() -> list[dict]:
    """Every finished video in data/videos, newest first: highlight clips, their cinematic versions and long
    videos. Works whether or not the demo is still in the app (demo_exists tells which)."""
    out: list[dict] = []
    if not VIDEOS_DIR.is_dir():
        return out
    for d in VIDEOS_DIR.iterdir():
        if not (d / "videos.json").is_file():
            continue
        demo_id = d.name
        try:
            stamp_match(demo_id)
            index = videos_index(demo_id)
        except (OSError, ValueError):
            continue
        live = db.get_demo(demo_id) is not None
        match = index.get("match") or _match_from_folder(index.get("export_folder", ""))

        def add(file: str | None, kind: str, v: dict, seconds=None) -> None:
            p = d / file if file else None
            if not p or not p.is_file():
                return
            st = p.stat()
            out.append({"demo_id": demo_id, "file": file, "kind": kind, "demo_exists": live,
                        "map": match.get("map"), "date": match.get("date"), "match": match,
                        "highlight_id": v.get("highlight_id"), "type": v.get("type"), "round": v.get("round"),
                        "player": v.get("player"), "player_steamid": v.get("player_steamid"),
                        "description": v.get("description") or file, "seconds": seconds if seconds is not None else v.get("seconds"),
                        "size": st.st_size, "created": st.st_mtime})
        for v in index.get("videos", []):
            add(v.get("file"), "clip", v)
            add(v.get("cine"), "cinematic", v, (v.get("cine_info") or {}).get("seconds"))
        for lv in index.get("long", []):
            add(lv.get("file"), "long", lv)
    for m in montages_index():
        p = MONTAGE_DIR / m["file"]
        if p.is_file():
            st = p.stat()
            out.append({"demo_id": MONTAGE_ID, "file": m["file"], "kind": "montage", "demo_exists": False,
                        "map": None, "date": None, "match": {}, "maps": m.get("maps", []), "description": m["description"],
                        "seconds": m.get("seconds"), "size": st.st_size, "created": st.st_mtime})
    out.sort(key=lambda c: c["created"], reverse=True)
    return out


def delete_saved(demo_id: str, name: str) -> dict:
    """Montages (demo_id "montages") are removed from their own index.
    Delete one file from the clip gallery. A highlight's original clip takes its cinematic version with it
    (that entry is removed from the index); deleting only the cinematic version keeps the original."""
    if demo_id == MONTAGE_ID:
        ms = montages_index()
        if not any(m["file"] == name for m in ms):
            raise RuntimeError("no such clip")
        (MONTAGE_DIR / name).unlink(missing_ok=True)
        _write_montages([m for m in ms if m["file"] != name])
        return {"ok": True, "files": [name]}
    out_dir = VIDEOS_DIR / demo_id
    index = videos_index(demo_id)
    names: list[str] = []
    for v in list(index.get("videos", [])):
        if v.get("file") == name:
            names += [n for n in (v.get("file"), v.get("cine")) if n]
            index["videos"].remove(v)
        elif v.get("cine") == name:
            names.append(name)
            v.pop("cine", None)
            v.pop("cine_info", None)
    for lv in list(index.get("long", [])):
        if lv.get("file") == name:
            names.append(name)
            index["long"].remove(lv)
    if not names:
        raise RuntimeError("no such clip")
    for n in names:
        (out_dir / n).unlink(missing_ok=True)
    (out_dir / "videos.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    return {"ok": True, "files": names}


# ---- montages: clips of different matches joined into one video ----------------------------------------

MONTAGE_ID = "montages"                 # served like a demo's videos: /api/demos/montages/videos/<file>
MONTAGE_DIR = VIDEOS_DIR / MONTAGE_ID


def montages_index() -> list[dict]:
    p = MONTAGE_DIR / "montages.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("montages", []) if p.is_file() else []
    except (OSError, ValueError):
        return []


def _write_montages(ms: list[dict]) -> None:
    MONTAGE_DIR.mkdir(parents=True, exist_ok=True)
    (MONTAGE_DIR / "montages.json").write_text(json.dumps({"montages": ms}, indent=1), encoding="utf-8")


def compile_montage(items: list[dict], title: str = "") -> dict:
    """Join saved videos of any matches, in the given order, into data/videos/montages/montage_<n>clips_<time>.mp4.
    items: [{"demo_id", "file"}] as listed by saved_clips(). Stream copy when every piece is the same kind of video
    (all cinematic or all plain recordings), otherwise a re-encode that evens out size, frame rate and audio."""
    if not FFMPEG_EXE.exists():
        raise RuntimeError("FFmpeg is not installed (Install HLAE + FFmpeg)")
    known = {(c["demo_id"], c["file"]): c for c in saved_clips()}
    clips = []
    for it in items:
        c = known.get((str(it.get("demo_id")), str(it.get("file"))))
        if c is None:
            raise RuntimeError(f"clip not found: {it.get('file')}")
        clips.append(c)
    if len(clips) < 2:
        raise RuntimeError("pick at least two clips")
    MONTAGE_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = MONTAGE_DIR / f"montage_{len(clips)}clips_{stamp}.mp4"
    files = [(MONTAGE_DIR if c["demo_id"] == MONTAGE_ID else VIDEOS_DIR / c["demo_id"]) / c["file"] for c in clips]
    audio = [c["kind"] != "clip" or bool(_clip_has_audio(c)) for c in clips]
    ok = False
    if len({c["kind"] for c in clips}) == 1 and all(audio) and len({_stream_sig(f) for f in files}) == 1:
        lst = MONTAGE_DIR / f"list_{stamp}.txt"
        r = subprocess.run(concat_copy_args(files, lst, out), capture_output=True, text=True)
        lst.unlink(missing_ok=True)
        ok = r.returncode == 0 and out.exists() and out.stat().st_size > 0
    if not ok:
        args = concat_reencode_args([{"path": f, "audio": a, "seconds": float(c.get("seconds") or 0)}
                                     for f, a, c in zip(files, audio, clips)],
                                    out, int(cfg.get("record_width", 1920)), int(cfg.get("record_height", 1080)),
                                    int(cfg.get("record_fps", 60)), int(cfg.get("record_crf", 20)))
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            out.unlink(missing_ok=True)
            raise RuntimeError("ffmpeg failed: " + r.stderr[-800:])
    maps = sorted({c["map"] for c in clips if c.get("map")})
    entry = {"file": out.name, "seconds": round(sum(float(c.get("seconds") or 0) for c in clips), 1),
             "description": (title.strip()[:80] or f"Montage · {len(clips)} highlights"), "maps": maps,
             "clips": [{"demo_id": c["demo_id"], "file": c["file"], "description": c["description"]} for c in clips],
             "created": time.time()}
    _write_montages(montages_index() + [entry])
    try:
        base = Path(cfg["export_folder"]) if cfg.get("export_folder") else Path.home() / "Downloads" / "cs2-watcher"
        (base / "montages").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out, base / "montages" / out.name)
        entry["exported"] = str(base / "montages")
    except Exception as e:  # noqa: BLE001
        entry["exported"] = f"export failed: {e}"
    return entry


def stream_sig(ffmpeg_stderr: str) -> tuple:
    """(video codec, size, audio codec, sample rate, channels) from `ffmpeg -i` output: clips that differ in any of
    these cannot be joined by stream copy. Pure function; unit-tested."""
    v = re.search(r"Video: (\w+).*?, (\d{2,5}x\d{2,5})", ffmpeg_stderr)
    a = re.search(r"Audio: (\w+).*?, (\d+) Hz, ([^,]+)", ffmpeg_stderr)
    return (v.groups() if v else None, a.groups() if a else None)


def _stream_sig(path: Path) -> tuple:
    r = subprocess.run([str(FFMPEG_EXE), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return stream_sig(r.stderr)


def _clip_has_audio(c: dict) -> bool:
    for v in videos_index(c["demo_id"]).get("videos", []):
        if v.get("file") == c["file"]:
            return bool(v.get("audio", True))
    return True


def _run(demo_id: str, hs: list[dict], opts: dict) -> None:
    cfg = load_config()
    raw_dir = None
    try:
        cs2 = find_cs2_path()
        csgo_dir = cs2 / "game" / "csgo"
        meta = json.loads((DATA_DIR / "parsed" / demo_id / "meta.json").read_text(encoding="utf-8"))
        names = {p["steamid"]: p["name"] for p in meta["players"]}
        tickrate = int(meta.get("tickrate", 64))

        _set(stage="preparing demo")
        dem = ensure_dem(demo_id, csgo_dir / f"replay_{demo_id[:8]}.dem")

        out_dir = VIDEOS_DIR / demo_id
        out_dir.mkdir(parents=True, exist_ok=True)
        raw_dir = raw_dir_for(demo_id)
        shutil.rmtree(raw_dir, ignore_errors=True)
        cfg_text, seqs = build_cfg(hs, raw_dir, names, cfg, tickrate, demo_id)
        take_root = hlae_take_root(cs2, demo_id)
        shutil.rmtree(take_root, ignore_errors=True)
        for s in seqs:
            Path(s["folder"]).mkdir(parents=True, exist_ok=True)
            s["take_dir"] = take_root / f"seq{s['n']}"
        (csgo_dir / "cfg").mkdir(exist_ok=True)
        (csgo_dir / "cfg" / f"{CFG_NAME}.cfg").write_text(cfg_text, encoding="utf-8")
        (out_dir / "last_record.cfg").write_text(cfg_text, encoding="utf-8")
        register_ffmpeg()

        _set(stage="launching CS2 under HLAE", status="recording",
             hint="CS2 is starting. If nothing records within a minute, open the console and type: exec " + CFG_NAME)
        try:
            (csgo_dir / "console.log").unlink(missing_ok=True)
        except OSError:
            pass
        launch_game(dem, cfg)
        time.sleep(5)

        # ---- monitor: wait for the game to record every sequence and quit ------------------
        expected = sum((s["end"] - s["start"]) / tickrate for s in seqs)
        timeout = 240 + expected * 6 + 25 * len(seqs)
        seen_running = False
        t0 = time.time()
        while True:
            if _cancel.is_set():
                raise RuntimeError("cancelled")
            running = cs2_running()
            seen_running = seen_running or running
            done = [s for s in seqs if (Path(s["folder"]) / "video.mp4").exists()]
            cur = len(done)
            _set(current=cur, progress=0.05 + 0.75 * cur / max(1, len(seqs)),
                 stage=f"recording highlight {min(cur + 1, len(seqs))} of {len(seqs)}" if running else "waiting for CS2")
            if seen_running and not running:
                break
            if time.time() - t0 > timeout:
                if not done:
                    raise RuntimeError("timed out: CS2 did not record anything. Close CS2 and try again, or type "
                                       f"'exec {CFG_NAME}' in the CS2 console if the game sat idle.")
                break
            time.sleep(2)
        time.sleep(2)

        _set(status="encoding", stage="muxing audio", raw_files=_tree(raw_dir) + (_tree(take_root) if take_root.exists() else []),
             console=_console_tail(csgo_dir))
        outputs = encode_outputs(seqs, out_dir, tickrate,
                                 progress=lambda f, outs: _set(progress=0.8 + 0.15 * f, outputs=outs))
        if not outputs:
            raise RuntimeError("no video files were produced")
        write_index(demo_id, outputs)
        _set(stage="copying to Downloads")
        try:
            exported = str(export_clips(demo_id, outputs, cfg))
        except Exception as e:  # noqa: BLE001
            exported = f"export failed: {e}"
        _set(exported=exported)
        if all(o.get("audio") for o in outputs):
            shutil.rmtree(raw_dir, ignore_errors=True)
            shutil.rmtree(take_root, ignore_errors=True)
        else:
            (out_dir / "raw_files.txt").write_text("\n".join(_tree(raw_dir, 500)), encoding="utf-8")
            (out_dir / "console_tail.txt").write_text("\n".join(_console_tail(csgo_dir, 2000)), encoding="utf-8")
        _set(status="done", stage="done", progress=1.0, outputs=outputs,
             warning=None if all(o.get("audio") for o in outputs) else "no game audio was found for some clips; raw files kept in " + str(raw_dir))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        _set(status="error", stage="error", error=str(e))


# ---- cinematic post-processing job (slow-motion kills + music) ------------------------------

_cine_job: dict | None = None


_cine_queue: list[dict] = []          # jobs waiting for the running one: {"demo_id", "videos", "overrides"}
_cine_lock = threading.Lock()


def cinematic_status() -> dict | None:
    """The current (or last) job plus the queue behind it: queued = [{"demo_id", "total"}]."""
    if not _cine_job and not _cine_queue:
        return None
    st = dict(_cine_job or {"status": "idle"})
    st["queued"] = [{"demo_id": q["demo_id"], "total": len(q["videos"])} for q in _cine_queue]
    return st


_cine_thread: threading.Thread | None = None


def cinematic_busy() -> bool:
    """True while a cinematic job is really running (a job whose thread died is cleared, not kept forever)."""
    global _cine_job
    if not (_cine_job and _cine_job.get("status") == "running"):
        return False
    if _cine_thread is not None and not _cine_thread.is_alive():
        _cine_job.update({"status": "error", "error": "the job stopped unexpectedly"})
        return False
    return True


def cancel_cinematic() -> dict:
    """Abort the running cinematic job: kill its ffmpeg, mark the job cancelled."""
    from . import cinematic as cine

    killed = cine.cancel_running_ffmpeg()
    if _cine_job and _cine_job.get("status") == "running":
        _cine_job.update({"status": "error", "error": "cancelled", "stage": "cancelled", "eta": 0})
    return {"ok": True, "killed": killed}


def start_cinematic(demo_id: str, videos: list[dict], overrides: dict | None = None) -> dict:
    """Process the given clips in a background thread; `overrides` are cine_* options on top of config.toml.
    While another job runs the request waits in a queue (any match) and starts by itself afterwards; clips that
    are already queued or being processed are not added twice. Returns the job, with "queued": position (0 = now)."""
    with _cine_lock:
        if cinematic_busy() or _cine_queue:
            taken = {(q["demo_id"], v["highlight_id"]) for q in _cine_queue for v in q["videos"]}
            if cinematic_busy():
                taken |= {(_cine_job["demo_id"], h) for h in (_cine_job.get("highlight_ids") or [])}
            videos = [v for v in videos if (demo_id, v["highlight_id"]) not in taken]
            if not videos:
                raise RuntimeError("these clips are already being made or waiting in the queue")
            _cine_queue.append({"demo_id": demo_id, "videos": videos, "overrides": overrides})
            return {"demo_id": demo_id, "status": "queued", "total": len(videos), "queued": len(_cine_queue)}
        return {**_launch_cinematic(demo_id, videos, overrides), "queued": 0}


def _start_next_cinematic() -> None:
    with _cine_lock:
        if _cine_queue and not cinematic_busy():
            q = _cine_queue.pop(0)
            _launch_cinematic(q["demo_id"], q["videos"], q["overrides"])


def clear_cinematic_queue() -> int:
    with _cine_lock:
        n = len(_cine_queue)
        _cine_queue.clear()
        return n


def _launch_cinematic(demo_id: str, videos: list[dict], overrides: dict | None = None) -> dict:
    global _cine_job, _cine_thread
    _cine_job = {"demo_id": demo_id, "status": "running", "total": len(videos), "done": 0, "current": videos[0]["file"],
                 "error": None, "stage": "starting", "clip_progress": 0.0, "progress": 0.0, "eta": None,
                 "started": time.time(), "highlight_ids": [v["highlight_id"] for v in videos]}

    def run():
        global _cine_job
        from . import cinematic as cine
        ffprobe = FFMPEG_DIR / "bin" / "ffprobe.exe"
        clip_times: list[float] = []
        try:
            for i, v in enumerate(videos):
                if _cine_job.get("status") != "running":
                    return
                t_clip = time.time()
                _cine_job.update({"current": v["file"], "done": i, "stage": "starting", "clip_progress": 0.0})

                def on_progress(stage: str, frac: float | None, i=i, t_clip=t_clip) -> None:
                    # the encode is ~85 % of a clip's time; the preparation (speech, music analysis) the rest
                    cp = 0.15 + 0.85 * frac if frac is not None else (0.1 if stage != "done" else 1.0)
                    overall = (i + cp) / len(videos)
                    elapsed = time.time() - t_clip
                    per_clip = (sum(clip_times) / len(clip_times)) if clip_times else None
                    if frac is not None and frac > 0.05:
                        this_left = elapsed / cp * (1 - cp)
                    elif per_clip:
                        this_left = max(0.0, per_clip - elapsed)
                    else:
                        this_left = None
                    rest = (len(videos) - i - 1) * (per_clip if per_clip else (elapsed / cp if (frac and frac > 0.2) else 60.0))
                    eta = (this_left + rest) if this_left is not None else None
                    _cine_job.update({"stage": stage, "clip_progress": round(cp, 3), "progress": round(overall, 3),
                                      "eta": round(eta) if eta is not None else None})
                nv = cine.cinematic_for_video(demo_id, v, FFMPEG_EXE, ffprobe, VIDEOS_DIR, overrides=overrides,
                                              progress=on_progress)
                clip_times.append(time.time() - t_clip)
                idx = videos_index(demo_id)
                idx["videos"] = [nv if x["highlight_id"] == nv["highlight_id"] else x for x in idx["videos"]]
                (VIDEOS_DIR / demo_id / "videos.json").write_text(json.dumps(idx, indent=1), encoding="utf-8")
                try:
                    export_clips(demo_id, [{"file": nv["cine"]}])
                except Exception:  # noqa: BLE001
                    pass
            _cine_job.update({"status": "done", "done": len(videos), "stage": "done", "progress": 1.0, "eta": 0})
        except Exception as e:  # noqa: BLE001
            if _cine_job.get("error") != "cancelled":
                traceback.print_exc()
                _cine_job.update({"status": "error", "error": str(e)})
        finally:
            threading.Thread(target=_start_next_cinematic, daemon=True).start()   # after this thread has ended

    _cine_thread = threading.Thread(target=run, daemon=True, name="cinematic")
    _cine_thread.start()
    return dict(_cine_job)


# ---- squad montage: every 3K+ of you and your friends in a match, recorded, made cinematic and joined ----------

SQUAD_PATH = DATA_DIR / "squad.json"
DEFAULT_SQUAD = [{"name": "qwqwqwqwwqwq", "steamid": "76561197970573523"}, {"name": "invis", "steamid": "76561197979720388"},
                 {"name": "holyshnizel", "steamid": "76561198197366093"}, {"name": "psycz", "steamid": "76561197991944768"},
                 {"name": "vALenk", "steamid": "76561197999213896"}, {"name": "Ldr", "steamid": "76561197990897981"},
                 {"name": "Tartaruga46", "steamid": "76561198007120582"}, {"name": "Axolotl2", "steamid": "76561197992168498"}]


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def load_squad() -> list[dict]:
    try:
        members = json.loads(SQUAD_PATH.read_text(encoding="utf-8")).get("members") if SQUAD_PATH.is_file() else None
    except (OSError, ValueError, AttributeError):
        members = None
    return members if isinstance(members, list) else [dict(m) for m in DEFAULT_SQUAD]


# ---- the Squad tab: who is in the squad, and how each one appears on the player card in videos ------------

PHOTO_TYPES = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}


def _players_reg() -> dict:
    from . import cinematic as cine
    try:
        reg = json.loads(cine.PLAYERS_JSON.read_text(encoding="utf-8")) if cine.PLAYERS_JSON.is_file() else {}
    except (OSError, ValueError):
        reg = {}
    return reg if isinstance(reg, dict) else {}


def squad_players() -> list[dict]:
    """The squad with each member's card name (players.json) and photo file (data/players), for the Squad tab."""
    from . import cinematic as cine
    reg = _players_reg()
    out = []
    for m in load_squad():
        sid, name = m.get("steamid") or "", m.get("name") or ""
        entry = reg.get(sid) if sid and isinstance(reg.get(sid), dict) else {}
        photo = None
        if entry.get("photo") and (cine.PLAYERS_DIR / Path(str(entry["photo"])).name).is_file():
            photo = Path(str(entry["photo"])).name
        elif cine.PLAYERS_DIR.is_dir():
            f = cine._photo_named(sid) or cine._photo_named(name)
            photo = f.name if f else None
        out.append({"name": name, "steamid": sid, "card_name": str(entry.get("name") or ""), "photo": photo})
    return out


def save_squad_players(members: list[dict]) -> list[dict]:
    """Save the Squad tab: members (name, steamid) to squad.json, card names to players.json (by Steam ID)."""
    from . import cinematic as cine
    save_squad([{"name": m.get("name"), "steamid": m.get("steamid")} for m in members])
    reg = _players_reg()
    for m in members:
        sid = re.sub(r"\D", "", str(m.get("steamid") or ""))[:20]
        if not sid:
            continue
        e = reg.get(sid) if isinstance(reg.get(sid), dict) else {}
        card = str(m.get("card_name") or "").strip()[:32]
        if card:
            e["name"] = card
        else:
            e.pop("name", None)
        if e:
            reg[sid] = e
        else:
            reg.pop(sid, None)
    cine.PLAYERS_JSON.parent.mkdir(parents=True, exist_ok=True)
    cine.PLAYERS_JSON.write_text(json.dumps(reg, indent=1), encoding="utf-8")
    return squad_players()


def photo_key(steamid: str, name: str) -> str:
    """A member's photo file name: the Steam ID, else the name with only letters and digits. Pure function."""
    sid = re.sub(r"\D", "", str(steamid or ""))[:20]
    return sid or re.sub(r"[^A-Za-z0-9_-]", "", str(name or ""))[:32]


def save_player_photo(steamid: str, name: str, data: bytes, ext: str) -> str:
    """Store a member's photo as data/players/<key>.<ext>, replacing older ones of the same member."""
    from . import cinematic as cine
    ext = ext.lower()
    if ext not in PHOTO_TYPES:
        raise RuntimeError("use a PNG, JPG or WebP picture")
    if len(data) > 8 * 1024 * 1024:
        raise RuntimeError("the picture is too large (8 MB at most)")
    key = photo_key(steamid, name)
    if not key:
        raise RuntimeError("give the player a name or a Steam ID first")
    remove_player_photo(steamid, name)
    cine.PLAYERS_DIR.mkdir(parents=True, exist_ok=True)
    (cine.PLAYERS_DIR / f"{key}{ext}").write_bytes(data)
    sid = re.sub(r"\D", "", str(steamid or ""))
    if sid:
        reg = _players_reg()
        reg.setdefault(sid, {})["photo"] = f"{key}{ext}"
        cine.PLAYERS_JSON.write_text(json.dumps(reg, indent=1), encoding="utf-8")
    return f"{key}{ext}"


def remove_player_photo(steamid: str, name: str) -> None:
    from . import cinematic as cine
    if not cine.PLAYERS_DIR.is_dir():
        return
    keys = {cine._photo_key(k) for k in (steamid, name, photo_key(steamid, name)) if cine._photo_key(k or "")}
    for f in cine.PLAYERS_DIR.iterdir():
        if f.is_file() and f.suffix.lower() in cine.PHOTO_EXT and cine._photo_key(f.stem) in keys:
            f.unlink(missing_ok=True)
    sid = re.sub(r"\D", "", str(steamid or ""))
    reg = _players_reg()
    if sid and isinstance(reg.get(sid), dict) and reg[sid].pop("photo", None) is not None:
        if not reg[sid]:
            reg.pop(sid)
        cine.PLAYERS_JSON.write_text(json.dumps(reg, indent=1), encoding="utf-8")


def load_team() -> dict:
    """{"team": the squad's name for the intro card, "intro": whether montages start with it}."""
    try:
        j = json.loads(SQUAD_PATH.read_text(encoding="utf-8")) if SQUAD_PATH.is_file() else {}
    except (OSError, ValueError):
        j = {}
    j = j if isinstance(j, dict) else {}
    return {"team": str(j.get("team") or "")[:40], "intro": bool(j.get("intro", True))}


def save_squad(members: list[dict] | None = None, team: str | None = None, intro: bool | None = None) -> list[dict]:
    clean = []
    for m in (members if members is not None else load_squad()):
        name, sid = str(m.get("name") or "").strip()[:40], re.sub(r"\D", "", str(m.get("steamid") or ""))[:20]
        if name or sid:
            clean.append({"name": name, "steamid": sid})
    SQUAD_PATH.parent.mkdir(parents=True, exist_ok=True)
    t = load_team()
    if team is not None:
        t["team"] = str(team).strip()[:40]
    if intro is not None:
        t["intro"] = bool(intro)
    SQUAD_PATH.write_text(json.dumps({"members": clean, **t}, indent=1), encoding="utf-8")
    return clean


def in_squad(steamid: str, player_name: str, members: list[dict]) -> bool:
    """A player is in the squad by Steam64 ID, or by name (case and symbols ignored) when the member has no ID."""
    sid, nm = str(steamid or ""), _norm(player_name)
    return any((m.get("steamid") and m["steamid"] == sid) or (_norm(m.get("name")) and _norm(m.get("name")) == nm)
               for m in members)


def squad_highlights(highlights: list[dict], members: list[dict], min_kills: int = 3) -> list[dict]:
    """Highlights of squad players with at least `min_kills` kills (3K, 4K, ace, a clutch with 3+ kills), in
    match order. Pure function; unit-tested."""
    big = ("multikill", "ace", "clutch")
    picked = [h for h in highlights if h.get("type") in big and len(h.get("kills") or []) >= min_kills
              and in_squad(h.get("player"), h.get("player_name"), members)]
    return sorted(picked, key=lambda h: h["start_tick"])


_squad_job: dict | None = None


def squad_progress(st: dict, rec_progress: float = 0.0) -> float:
    """Overall 0..1 of a montage job: recording the missing clips (when there are any) takes the first 35 %, then
    rendering the plays one by one up to 88 % of the rest, joining them and laying the song the last part.
    Pure function; unit-tested."""
    if st.get("status") == "done":
        return 1.0
    rec = 0.35 if st.get("needs_recording") else 0.0
    stage = st.get("stage")
    if stage == "recording":
        n = max(1, int(st.get("rec_n") or 1))   # several matches recorded one after the other
        return round(rec * (int(st.get("rec_i") or 0) + max(0.0, min(1.0, rec_progress))) / n, 3)
    if stage == "cinematic":
        n = max(1, int(st.get("total") or 1))
        done = (int(st.get("piece_i") or 0) + max(0.0, min(1.0, float(st.get("piece_frac") or 0)))) / n
        return round(rec + (1 - rec) * 0.88 * done, 3)
    if stage == "joining":
        return round(rec + (1 - rec) * (0.97 if st.get("piece") == "laying the song" else 0.9), 3)
    return round(rec if stage not in (None, "starting") else 0.0, 3)


def squad_status() -> dict | None:
    if not _squad_job:
        return None
    st = dict(_squad_job)
    if st["status"] == "running":
        if st["stage"] == "recording" and _job:
            st["detail"] = (f"match {int(st.get('rec_i') or 0) + 1}/{st['rec_n']} ({st.get('rec_match', '')}) · " if (st.get("rec_n") or 0) > 1 else "") \
                + f"{_job.get('stage', '')} {round(100 * float(_job.get('progress') or 0))}%"
        elif st["stage"] in ("cinematic", "joining") and st.get("piece"):
            st["detail"] = st["piece"]
        st["progress"] = squad_progress(st, float((_job or {}).get("progress") or 0))
        # time left: from how fast the rendering went so far (recording time is not counted, it runs at game speed)
        t0, p0 = st.get("render_started"), st.get("render_from")
        if t0 is not None and st["progress"] - p0 > 0.03:
            rate = (time.time() - t0) / (st["progress"] - p0)
            st["eta"] = round(rate * (1 - st["progress"]))
    return st


def start_squad_reel(demo_id: str, min_kills: int = 3, cinematic: bool = True, overrides: dict | None = None,
                     highlight_ids: list[int] | None = None) -> dict:
    """Record what is missing, make the cinematic versions (cinematic=True), join them into one long video, all in a
    background thread. With `overrides` (cine_* options from the dialog) every clip is made again with them, so
    the whole montage has one look; without, existing cinematic versions are reused.
    highlight_ids: the plays picked by hand on the match page instead of the squad's min_kills+ plays."""
    global _squad_job
    if _squad_job and _squad_job.get("status") == "running":
        raise RuntimeError("a montage is already being made")
    if highlight_ids is not None:
        want = set(highlight_ids)
        hs = sorted((h for h in db.get_highlights(demo_id) if h["id"] in want), key=lambda h: h["start_tick"])
        if len(hs) < 2:
            raise RuntimeError("pick at least two highlights")
        label = "Montage"
    else:
        hs = squad_highlights(db.get_highlights(demo_id), load_squad(), min_kills)
        if not hs:
            raise RuntimeError(f"no {min_kills}K+ highlights of your squad in this match")
        label = "Squad highlights"
    ids = [h["id"] for h in hs]
    _squad_job = {"demo_id": demo_id, "status": "running", "stage": "starting", "highlight_ids": ids, "total": len(ids),
                  "error": None, "result": None, "started": time.time(), "cinematic": cinematic, "label": label,
                  "picked": highlight_ids is not None}

    def wait(busy) -> None:
        while busy():
            if _squad_job.get("status") != "running":
                raise RuntimeError("cancelled")
            time.sleep(2)

    def run() -> None:
        try:
            have = {v["highlight_id"] for v in videos_index(demo_id).get("videos", [])}
            need = [i for i in ids if i not in have]
            _squad_job["needs_recording"] = bool(need)
            if need:
                _squad_job["stage"] = "recording"
                start(demo_id, need)
                wait(lambda: (_job or {}).get("status") in ("preparing", "recording", "encoding"))
                if (_job or {}).get("status") != "done":
                    raise RuntimeError("recording did not finish: " + str((_job or {}).get("error") or (_job or {}).get("status")))
            if cinematic:
                _squad_job.update({"stage": "cinematic", "piece_i": 0, "piece_frac": 0.0, "render_started": time.time()})
                _squad_job["render_from"] = squad_progress(_squad_job)
                _squad_job.update({"status": "done", "stage": "done", "result": _squad_cinematic(demo_id, ids, overrides)})
                return
            _squad_job["stage"] = "joining"
            recorded = [i for i in ids if i in {v["highlight_id"] for v in videos_index(demo_id).get("videos", [])}]
            if not recorded:
                raise RuntimeError("none of the highlights has a recorded clip")
            if len(recorded) == 1:   # one clip is already the whole montage
                v = next(v for v in videos_index(demo_id)["videos"] if v["highlight_id"] == recorded[0])
                f = (v.get("cine") if cinematic else None) or v["file"]
                _squad_job.update({"status": "done", "stage": "done", "result": {"file": f, "description": v["description"],
                                                                                  "seconds": v.get("seconds"), "single": True}})
                return
            entry = compile_long_video(demo_id, recorded, cinematic=cinematic)
            idx = videos_index(demo_id)
            for e in idx.get("long", []):
                if e["file"] == entry["file"]:
                    e["description"] = entry["description"] = f"{label} · {len(recorded)} plays" + (" (cinematic)" if cinematic else "")
            (VIDEOS_DIR / demo_id / "videos.json").write_text(json.dumps(idx, indent=1), encoding="utf-8")
            _squad_job.update({"status": "done", "stage": "done", "result": entry})
        except Exception as e:  # noqa: BLE001
            if _squad_job.get("status") == "running":
                traceback.print_exc()
                _squad_job.update({"status": "error", "stage": "error", "error": str(e)})

    threading.Thread(target=run, daemon=True, name="squad").start()
    return {**_squad_job, "highlights": [{"id": h["id"], "description": h["description"]} for h in hs]}


def montage_timeline(infos: list[dict]) -> tuple[float, float | None, list[tuple[float, float]]]:
    """(total seconds, first slow-motion kill, voice-line windows) of pieces played one after the other, from the
    make_cinematic info of each piece. Pure function; unit-tested."""
    t, hit, duck = 0.0, None, []
    for inf in infos:
        if hit is None and inf.get("hits"):
            hit = t + float(inf["hits"][0])
        duck += [(t + float(a), t + float(b)) for a, b in inf.get("duck") or []]
        t += float(inf.get("seconds") or 0)
    return round(t, 3), (round(hit, 3) if hit is not None else None), duck


def _join(files: list[Path], infos: list[dict], out: Path, list_file: Path) -> None:
    """Join same-format videos by stream copy; when that fails (pieces that do not fit together), re-encode."""
    r = subprocess.run(concat_copy_args(files, list_file, out), capture_output=True, text=True)
    list_file.unlink(missing_ok=True)
    if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
        return
    cfg = load_config()
    r = subprocess.run(concat_reencode_args([{"path": f, "audio": True, "seconds": float(i.get("seconds") or 0)}
                                             for f, i in zip(files, infos)], out,
                                            int(cfg.get("record_width", 1920)), int(cfg.get("record_height", 1080)),
                                            int(cfg.get("record_fps", 60)), int(cfg.get("record_crf", 20))),
                       capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        out.unlink(missing_ok=True)
        raise RuntimeError("joining the videos failed: " + r.stderr[-400:])


def _render_song_montage(plays: list[tuple[str, dict]], overrides: dict | None, work_dir: Path, out: Path,
                         base: int = 0, last: bool = True, intro: dict | None = None) -> dict:
    """Every play (demo_id, its videos.json entry) rendered cinematic without music, joined in the given order,
    then ONE song over the whole video (its drop on the first slow-motion kill, dipping under the voice lines).
    Progress goes to _squad_job; base / last: this is one part (plays base.. of the job's total) of a recap.
    intro: {"team", "sub", "stats"} puts the intro card first (the song already plays under it).
    Returns {"seconds", "music"}. The clips' own cinematic versions are left alone."""
    from . import cinematic as cine
    ffprobe = FFMPEG_DIR / "bin" / "ffprobe.exe"
    pieces: list[Path] = []
    infos: list[dict] = []
    stamp = time.strftime("%Y%m%d_%H%M%S")
    joined = work_dir / f"montage_joined_{stamp}.mp4"
    try:
        for i, (demo_id, v) in enumerate(plays):
            if _squad_job.get("status") != "running":
                raise RuntimeError("cancelled")

            def prog(stage: str, frac: float | None, i=i) -> None:
                n = int(_squad_job.get("total") or len(plays))
                _squad_job["piece"] = f"play {base + i + 1}/{n} · {stage}" + (f" {round(100 * frac)}%" if frac is not None else "")
                _squad_job["piece_i"] = base + i
                if frac is not None:
                    _squad_job["piece_frac"] = frac
            nv = cine.cinematic_for_video(demo_id, v, FFMPEG_EXE, ffprobe, VIDEOS_DIR, overrides=overrides, progress=prog,
                                          montage_piece=True)
            pieces.append(VIDEOS_DIR / demo_id / nv["cine"])
            _squad_job.update({"piece_i": base + i + 1, "piece_frac": 0.0})
            infos.append(nv["cine_info"])
        if intro and pieces:
            _squad_job["piece"] = "making the intro"
            card = work_dir / f"montage_intro_{stamp}.mp4"
            at = float((infos[0].get("hits") or [1.0])[0])   # a frame of the first slow-motion kill behind the title
            secs = cine.make_intro(FFMPEG_EXE, ffprobe, pieces[0], at, card, intro.get("team", ""), intro.get("sub", ""),
                                   intro.get("stats", ""), int(load_config().get("record_crf", 20)))
            if secs:
                pieces.insert(0, card)
                infos.insert(0, {"seconds": secs})
            else:
                card.unlink(missing_ok=True)
        if last:
            _squad_job.update({"stage": "joining", "piece": "joining the plays"})
        total, hit, duck = montage_timeline(infos)
        _join(pieces, infos, joined, work_dir / f"montage_list_{stamp}.txt")
        opts = cine.options(load_config(), overrides)
        music = cine.pick_music(opts)
        song = None
        if music and float(opts.get("cine_music_volume", 0.7)) > 0:
            if last:
                _squad_job["piece"] = "laying the song"
            offset = cine.music_offset(FFMPEG_EXE, music, hit, total) if (opts.get("cine_music_sync") and hit is not None) else 0.0
            r = subprocess.run(cine.montage_music_args(FFMPEG_EXE, joined, music, out, total, offset,
                                                       float(opts["cine_music_volume"]), duck), capture_output=True, text=True)
            if r.returncode != 0 or not out.exists():
                raise RuntimeError("adding the song failed: " + r.stderr[-400:])
            song = music.name
        else:
            joined.replace(out)
        return {"seconds": round(total, 1), "music": song}
    finally:
        for p_ in pieces:
            p_.unlink(missing_ok=True)
        joined.unlink(missing_ok=True)


def _squad_cinematic(demo_id: str, ids: list[int], overrides: dict | None) -> dict:
    """The cinematic montage of plays of one match, saved as one of its long videos."""
    vids = sorted((v for v in videos_index(demo_id).get("videos", []) if v["highlight_id"] in set(ids)),
                  key=lambda v: v["start_tick"])
    if not vids:
        raise RuntimeError("none of the highlights has a recorded clip")
    out_dir = VIDEOS_DIR / demo_id
    out = out_dir / long_video_name(len(vids), True)
    plays = [(demo_id, v) for v in vids]
    made = _render_song_montage(plays, overrides, out_dir, out,
                                intro=squad_intro("Squad montage" if _squad_job.get("label") != "Montage" else "Montage",
                                                  plays, {demo_id: (db.get_demo(demo_id) or {}).get("date")}))
    song = made["music"]
    entry = {"file": out.name, "size": out.stat().st_size, "seconds": made["seconds"],
             "highlight_ids": [v["highlight_id"] for v in vids], "clips": [v["file"] for v in vids], "cinematic": True,
             "created": time.time(), "music": song,
             "description": f"{_squad_job.get('label', 'Squad highlights')} · {len(vids)} plays (cinematic)" + (f" · {Path(song).stem}" if song else "")}
    index = videos_index(demo_id)
    index["long"] = [e for e in index.get("long", []) if (out_dir / e["file"]).exists()] + [entry]
    (out_dir / "videos.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    try:
        entry["exported"] = str(export_clips(demo_id, [entry], load_config()))
    except Exception as e:  # noqa: BLE001
        entry["exported"] = f"export failed: {e}"
    return entry


def montage_plays(items: list[dict]) -> list[tuple[str, dict]]:
    """The recorded plays behind gallery items picked for a cinematic montage: an original clip or a cinematic
    version both go back to the original recording. Long videos and montages cannot be made cinematic again."""
    plays = []
    for it in items:
        demo_id, name = str(it.get("demo_id")), str(it.get("file"))
        v = next((v for v in videos_index(demo_id).get("videos", []) if name in (v.get("file"), v.get("cine"))), None) \
            if demo_id != MONTAGE_ID else None
        if v is None:
            raise RuntimeError(f"{name} is not a highlight clip (long videos and montages cannot be made cinematic again)")
        plays.append((demo_id, v))
    return plays


def start_library_montage(items: list[dict], title: str = "", overrides: dict | None = None) -> dict:
    """The library's cinematic montage of clips of any matches, in the given order, with one song; background job
    reported through squad_status() with demo_id "montages"."""
    global _squad_job
    if _squad_job and _squad_job.get("status") == "running":
        raise RuntimeError("a montage is already being made")
    if not FFMPEG_EXE.exists():
        raise RuntimeError("FFmpeg is not installed (Install HLAE + FFmpeg)")
    plays = montage_plays(items)
    if len(plays) < 2:
        raise RuntimeError("pick at least two clips")
    _squad_job = {"demo_id": MONTAGE_ID, "status": "running", "stage": "cinematic", "total": len(plays), "error": None,
                  "result": None, "started": time.time(), "cinematic": True, "label": "Montage", "picked": True,
                  "needs_recording": False, "piece_i": 0, "piece_frac": 0.0, "render_started": time.time(), "render_from": 0.0}

    def run() -> None:
        try:
            MONTAGE_DIR.mkdir(parents=True, exist_ok=True)
            out = MONTAGE_DIR / f"montage_{len(plays)}clips_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
            dates = {d: (videos_index(d).get("match") or {}).get("date") or (db.get_demo(d) or {}).get("date") for d, _ in plays}
            made = _render_song_montage(plays, overrides, MONTAGE_DIR, out, intro=squad_intro("Montage", plays, dates))
            song = made["music"]
            maps = sorted({(db.get_demo(d) or {}).get("map") or "" for d, _ in plays} - {""})
            entry = {"file": out.name, "seconds": made["seconds"], "maps": maps, "cinematic": True, "music": song,
                     "description": (title.strip()[:80] or f"Montage · {len(plays)} highlights (cinematic)")
                     + (f" · {Path(song).stem}" if song and not title.strip() else ""),
                     "clips": [{"demo_id": d, "file": v["file"], "description": v.get("description")} for d, v in plays],
                     "created": time.time()}
            _save_montage(out, entry)
            _squad_job.update({"status": "done", "stage": "done", "result": {**entry, "demo_id": MONTAGE_ID}})
        except Exception as e:  # noqa: BLE001
            if _squad_job.get("status") == "running":
                traceback.print_exc()
                _squad_job.update({"status": "error", "stage": "error", "error": str(e)})

    threading.Thread(target=run, daemon=True, name="montage").start()
    return dict(_squad_job)


# ---- recap: the squad's plays of a month / a year in one video ------------------------------------------

BIG_PLAYS = ("multikill", "ace", "clutch")


def play_kills(v: dict, h: dict | None) -> int:
    """Kills of a recorded play: from its highlight, else guessed from the type (old clips of deleted matches)."""
    if h and h.get("kills") is not None:
        return len(h["kills"])
    return 5 if v.get("type") == "ace" else 3 if v.get("type") in BIG_PLAYS else 0


def play_score(v: dict, h: dict | None) -> int:
    """How big a play is, for "best of": kills first, a clutch a bit more than the same kills without one."""
    return 10 * play_kills(v, h) + (5 if v.get("type") == "clutch" else 0)


def recap_plays(matches: list[dict], members: list[dict], date_from: str, date_to: str, min_kills: int = 3,
                best_of: int | None = None) -> list[dict]:
    """The squad's recorded plays of the matches played between date_from and date_to (YYYY-MM-DD, both included),
    oldest match first, in match order inside a match. matches: [{"demo_id", "date", "videos", "highlights":
    {id: highlight}}]. best_of keeps the biggest N plays (still in time order). Pure function; unit-tested."""
    out = []
    for m in matches:
        day = str(m.get("date") or "")[:10]
        if not day or not (date_from <= day <= date_to):
            continue
        for v in m.get("videos", []):
            h = m.get("highlights", {}).get(v.get("highlight_id")) or (v.get("ctx") or {}).get("highlight")
            if v.get("type") not in BIG_PLAYS or play_kills(v, h) < min_kills:
                continue
            if not in_squad(v.get("player_steamid") or (h or {}).get("player"), v.get("player") or (h or {}).get("player_name"), members):
                continue
            out.append({"demo_id": m["demo_id"], "date": day, "video": v, "score": play_score(v, h),
                        "start_tick": int(v.get("start_tick") or 0)})
    if best_of and len(out) > best_of:
        keep = sorted(out, key=lambda p: (-p["score"], p["date"], p["start_tick"]))[:best_of]
        out = [p for p in out if any(p is k for k in keep)]
    return sorted(out, key=lambda p: (p["date"], p["demo_id"], p["start_tick"]))


def _recap_matches() -> list[dict]:
    """Every match with recorded clips (also deleted ones), with its date and live highlights when it still exists."""
    out = []
    if not VIDEOS_DIR.is_dir():
        return out
    for d in VIDEOS_DIR.iterdir():
        if d.name == MONTAGE_ID or not (d / "videos.json").is_file():
            continue
        try:
            stamp_match(d.name)
            index = videos_index(d.name)
        except (OSError, ValueError):
            continue
        match = index.get("match") or _match_from_folder(index.get("export_folder", ""))
        live = {h["id"]: h for h in db.get_highlights(d.name)} if db.get_demo(d.name) else {}
        out.append({"demo_id": d.name, "date": match.get("date"), "map": match.get("map"), "match": match,
                    "videos": index.get("videos", []), "highlights": live})
    return out


def recap_preview(date_from: str, date_to: str, min_kills: int = 3, best_of: int | None = None) -> dict:
    """What a recap would hold, plus the squad plays of those matches that are not recorded yet."""
    members = load_squad()
    matches = _recap_matches()
    plays = recap_plays(matches, members, date_from, date_to, min_kills, best_of)
    have = {(m["demo_id"], v["highlight_id"]) for m in matches for v in m["videos"]}
    missing = []
    for d in db.list_demos():
        day = str(d.get("date") or "")[:10]
        if d.get("status") not in (None, "ready", "done") or not day or not (date_from <= day <= date_to):
            continue
        for h in squad_highlights(db.get_highlights(d["id"]), members, min_kills):
            if (d["id"], h["id"]) not in have:
                missing.append({"demo_id": d["id"], "map": d.get("map"), "date": day, "description": h["description"]})
    by_match: dict[str, dict] = {}
    info = {m["demo_id"]: m for m in matches}
    for p in plays:
        e = by_match.setdefault(p["demo_id"], {"demo_id": p["demo_id"], "date": p["date"], "map": info[p["demo_id"]].get("map"),
                                               "plays": 0, "seconds": 0.0})
        e["plays"] += 1
        e["seconds"] += float(p["video"].get("seconds") or 0)
    return {"plays": len(plays), "matches": list(by_match.values()), "missing": missing,
            "seconds": round(sum(float(p["video"].get("seconds") or 0) for p in plays), 1)}


def start_recap(date_from: str, date_to: str, min_kills: int = 3, best_of: int | None = None, title: str = "") -> dict:
    """The recap video as a background job (reported through squad_status() with demo_id "montages"): every
    match's plays made cinematic with ONE song for that match (a different song for the next match when there
    are enough tracks), the matches joined oldest first, saved with the montages."""
    global _squad_job
    if _squad_job and _squad_job.get("status") == "running":
        raise RuntimeError("a montage is already being made")
    if not FFMPEG_EXE.exists():
        raise RuntimeError("FFmpeg is not installed (Install HLAE + FFmpeg)")
    matches = _recap_matches()
    plays = recap_plays(matches, load_squad(), date_from, date_to, min_kills, best_of)
    if not plays:
        raise RuntimeError("no recorded squad plays in that period")
    groups: list[list[dict]] = []
    for p in plays:
        if groups and groups[-1][0]["demo_id"] == p["demo_id"]:
            groups[-1].append(p)
        else:
            groups.append([p])
    _squad_job = {"demo_id": MONTAGE_ID, "status": "running", "stage": "cinematic", "total": len(plays), "error": None,
                  "result": None, "started": time.time(), "cinematic": True, "label": "Recap", "picked": True,
                  "needs_recording": False, "piece_i": 0, "piece_frac": 0.0, "render_started": time.time(), "render_from": 0.0}

    def run() -> None:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        try:
            out = MONTAGE_DIR / f"recap_{len(plays)}plays_{stamp}.mp4"
            dates = {m["demo_id"]: m.get("date") for m in matches}
            made = _render_match_groups([[(p["demo_id"], p["video"]) for p in g] for g in groups], out, stamp,
                                        squad_intro("Recap", [x for g in groups for x in [(p["demo_id"], p["video"]) for p in g]],
                                                    dates, period=recap_period_name(date_from, date_to)))
            songs, secs = made["music"], [made["seconds"]]
            info = {m["demo_id"]: m for m in matches}
            maps = sorted({info[p["demo_id"]].get("map") or "" for p in plays} - {""})
            period = recap_period_name(date_from, date_to)
            entry = {"file": out.name, "seconds": round(sum(secs), 1),
                     "maps": maps, "cinematic": True, "recap": {"from": date_from, "to": date_to, "min_kills": min_kills,
                                                                 "best_of": best_of, "matches": len(groups)},
                     "music": [s_ for s_ in songs if s_],
                     "description": title.strip()[:80] or f"Recap · {period} · {len(plays)} plays from {len(groups)} match{'es' if len(groups) > 1 else ''}",
                     "clips": [{"demo_id": p["demo_id"], "file": p["video"]["file"], "description": p["video"].get("description")} for p in plays],
                     "created": time.time()}
            _save_montage(out, entry)
            _squad_job.update({"status": "done", "stage": "done", "result": {**entry, "demo_id": MONTAGE_ID}})
        except Exception as e:  # noqa: BLE001
            if _squad_job.get("status") == "running":
                traceback.print_exc()
                _squad_job.update({"status": "error", "stage": "error", "error": str(e)})

    threading.Thread(target=run, daemon=True, name="recap").start()
    return dict(_squad_job)


def _render_match_groups(groups: list[list[tuple[str, dict]]], out: Path, stamp: str, intro: dict | None) -> dict:
    """Several matches in one video: each match's plays rendered as a song montage with its own track (the one not
    heard for the longest time, so no repeat while there are tracks left), the intro first, the matches in order.
    Returns {"seconds", "music": [song per match]}."""
    from . import cinematic as cine
    MONTAGE_DIR.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    infos: list[dict] = []
    songs: list[str] = []
    done = 0
    try:
        for gi, g in enumerate(groups):
            part = MONTAGE_DIR / f"part{gi + 1}_{stamp}.mp4"   # no song chosen: each match gets the freshest track
            made = _render_song_montage(g, cine.options(overrides={"cine_music": ""}), MONTAGE_DIR, part,
                                        base=done, last=gi == len(groups) - 1, intro=intro if gi == 0 else None)
            parts.append(part)
            infos.append({"seconds": made["seconds"]})
            if made["music"]:
                songs.append(made["music"])
            done += len(g)
        if len(parts) == 1:
            parts[0].replace(out)
        else:
            _squad_job.update({"stage": "joining", "piece": "joining the matches"})
            _join(parts, infos, out, MONTAGE_DIR / f"list_{stamp}.txt")
        return {"seconds": round(sum(i["seconds"] for i in infos), 1), "music": songs}
    finally:
        for p_ in parts:
            p_.unlink(missing_ok=True)


def intro_dates(days: list[str]) -> str:
    """"17 Oct 2026", "3–20 Oct 2026", "28 Sep – 20 Oct 2026" or "30 Dec 2025 – 2 Jan 2026" for the days a
    montage covers. Pure function; unit-tested."""
    import datetime as dt
    ds = sorted({dt.date.fromisoformat(str(d)[:10]) for d in days if d and len(str(d)) >= 10})
    if not ds:
        return ""
    a, b = ds[0], ds[-1]
    mon = lambda d: d.strftime("%b")
    if a == b:
        return f"{a.day} {mon(a)} {a.year}"
    if (a.year, a.month) == (b.year, b.month):
        return f"{a.day}–{b.day} {mon(a)} {a.year}"
    if a.year == b.year:
        return f"{a.day} {mon(a)} – {b.day} {mon(b)} {a.year}"
    return f"{a.day} {mon(a)} {a.year} – {b.day} {mon(b)} {b.year}"


def squad_intro(kind: str, plays: list[tuple[str, dict]], dates: dict[str, str | None], period: str = "") -> dict | None:
    """The intro card's text for any montage, kept general: the team name, what it is and when ("Squad montage ·
    4 matches · 3–20 Oct 2026", "Recap · October 2026"), and the stats of its plays. None when the intro is off."""
    from . import cinematic as cine
    cfg = load_team()
    if not cfg["intro"] or not plays:
        return None
    demos = list(dict.fromkeys(d for d, _ in plays))
    when = period or intro_dates([dates.get(d) or "" for d in demos])
    n = len(demos)
    sub = " · ".join(x for x in (kind, f"{n} matches" if n > 1 and not period else "", when) if x)
    hl = lambda d, v: (v.get("ctx") or {}).get("highlight") or next((h for h in db.get_highlights(d) if h["id"] == v.get("highlight_id")), None)
    kills, types = [], []
    for d, v in plays:
        kills.append(play_kills(v, hl(d, v) if db.get_demo(d) else (v.get("ctx") or {}).get("highlight")))
        types.append(v.get("type") or "")
    return {"team": cfg["team"] or "Squad", "sub": sub, "stats": cine.intro_stats(kills, types)}


def _save_montage(out: Path, entry: dict) -> None:
    """Add a finished video to the montages index and copy it to <export folder>/montages (entry["exported"])."""
    _write_montages(montages_index() + [entry])
    try:
        cfg = load_config()
        base = Path(cfg["export_folder"]) if cfg.get("export_folder") else Path.home() / "Downloads" / "cs2-watcher"
        (base / "montages").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out, base / "montages" / out.name)
        entry["exported"] = str(base / "montages")
    except Exception as e:  # noqa: BLE001
        entry["exported"] = f"export failed: {e}"


def _join_as_recorded(plays: list[tuple[str, dict]], out: Path, stamp: str, intro: dict | None) -> dict:
    """The recorded clips joined as they are (game sound, no effects, no song), the intro card first."""
    from . import cinematic as cine
    _squad_job.update({"stage": "joining", "piece": "joining the clips"})
    files = [VIDEOS_DIR / d / v["file"] for d, v in plays]
    infos = [{"seconds": float(v.get("seconds") or 0)} for _, v in plays]
    card = MONTAGE_DIR / f"intro_{stamp}.mp4"
    MONTAGE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if intro:
            _squad_job["piece"] = "making the intro"
            first = plays[0][1]
            kills = (first.get("ctx") or {}).get("kills") or [1.0]
            secs = cine.make_intro(FFMPEG_EXE, FFMPEG_DIR / "bin" / "ffprobe.exe", files[0], float(kills[0]), card,
                                   intro.get("team", ""), intro.get("sub", ""), intro.get("stats", ""),
                                   int(load_config().get("record_crf", 20)))
            if secs:
                files.insert(0, card)
                infos.insert(0, {"seconds": secs})
        _squad_job["piece"] = "joining the clips"
        _join(files, infos, out, MONTAGE_DIR / f"list_{stamp}.txt")
        return {"seconds": round(sum(i["seconds"] for i in infos), 1), "music": []}
    finally:
        card.unlink(missing_ok=True)


def multi_squad_plan(demo_ids: list[str], min_kills: int = 3) -> list[dict]:
    """Per selected match, oldest first: its squad plays (min_kills+) and which of them still need recording."""
    members = load_squad()
    plan = []
    for d in demo_ids:
        demo = db.get_demo(d)
        if not demo:
            continue
        hs = squad_highlights(db.get_highlights(d), members, min_kills)
        have = {v["highlight_id"] for v in videos_index(d).get("videos", [])}
        plan.append({"demo_id": d, "map": demo.get("map"), "date": demo.get("date"),
                     "score": f"{demo.get('score_a')}–{demo.get('score_b')}", "ids": [h["id"] for h in hs],
                     "need": [h["id"] for h in hs if h["id"] not in have],
                     "plays": [{"id": h["id"], "description": h["description"], "recorded": h["id"] in have} for h in hs]})
    return sorted(plan, key=lambda m: str(m.get("date") or ""))


def start_multi_squad(demo_ids: list[str], min_kills: int = 3, title: str = "", cinematic: bool = True) -> dict:
    """The squad montage of several matches as one video (background job, squad_status() with demo_id "montages"):
    record what is missing match by match in CS2, then every match's plays cinematic with one song per match
    (cinematic=False: the recorded clips as they are, no song), the intro first, the matches joined oldest first,
    saved with the montages."""
    global _squad_job
    if _squad_job and _squad_job.get("status") == "running":
        raise RuntimeError("a montage is already being made")
    if not FFMPEG_EXE.exists():
        raise RuntimeError("FFmpeg is not installed (Install HLAE + FFmpeg)")
    plan = [m for m in multi_squad_plan(demo_ids, min_kills) if m["ids"]]
    if not plan:
        raise RuntimeError(f"no {min_kills}K+ plays of your squad in these matches")
    to_record = [m for m in plan if m["need"]]
    total = sum(len(m["ids"]) for m in plan)
    _squad_job = {"demo_id": MONTAGE_ID, "status": "running", "stage": "starting", "total": total, "error": None,
                  "result": None, "started": time.time(), "cinematic": cinematic, "label": "Squad montage", "picked": True,
                  "needs_recording": bool(to_record), "rec_i": 0, "rec_n": len(to_record), "matches": len(plan)}

    def run() -> None:
        try:
            for ri, m in enumerate(to_record):
                if _squad_job.get("status") != "running":
                    raise RuntimeError("cancelled")
                _squad_job.update({"stage": "recording", "rec_i": ri, "rec_match": f"{m.get('map') or 'match'} {str(m.get('date') or '')[:10]}"})
                start(m["demo_id"], m["need"])
                while (_job or {}).get("status") in ("preparing", "recording", "encoding"):
                    if _squad_job.get("status") != "running":
                        raise RuntimeError("cancelled")
                    time.sleep(2)
                if (_job or {}).get("status") != "done":
                    raise RuntimeError(f"recording {_squad_job['rec_match']} did not finish: "
                                       + str((_job or {}).get("error") or (_job or {}).get("status")))
            groups = []
            for m in plan:
                vids = sorted((v for v in videos_index(m["demo_id"]).get("videos", []) if v["highlight_id"] in set(m["ids"])),
                              key=lambda v: v["start_tick"])
                if vids:
                    groups.append([(m["demo_id"], v) for v in vids])
            if not groups:
                raise RuntimeError("none of the plays has a recorded clip")
            plays = [x for g in groups for x in g]
            _squad_job.update({"stage": "cinematic", "total": len(plays), "piece_i": 0, "piece_frac": 0.0,
                               "render_started": time.time()})
            _squad_job["render_from"] = squad_progress(_squad_job)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            out = MONTAGE_DIR / f"squad_{len(groups)}matches_{stamp}.mp4"
            dates = {m["demo_id"]: m.get("date") for m in plan}
            intro = squad_intro("Squad montage", plays, dates)
            made = _render_match_groups(groups, out, stamp, intro) if cinematic else _join_as_recorded(plays, out, stamp, intro)
            maps = sorted({m.get("map") or "" for m in plan if any(g[0][0] == m["demo_id"] for g in groups)} - {""})
            when = intro_dates([dates.get(g[0][0]) or "" for g in groups])
            entry = {"file": out.name, "seconds": made["seconds"], "maps": maps, "cinematic": cinematic, "music": made["music"],
                     "description": title.strip()[:80] or f"Squad montage · {len(plays)} plays from {len(groups)} match{'es' if len(groups) > 1 else ''} · {when}",
                     "clips": [{"demo_id": d, "file": v["file"], "description": v.get("description")} for d, v in plays],
                     "created": time.time()}
            _save_montage(out, entry)
            _squad_job.update({"status": "done", "stage": "done", "result": {**entry, "demo_id": MONTAGE_ID}})
        except Exception as e:  # noqa: BLE001
            if _squad_job.get("status") == "running":
                traceback.print_exc()
                _squad_job.update({"status": "error", "stage": "error", "error": str(e)})

    threading.Thread(target=run, daemon=True, name="squad-multi").start()
    return {**_squad_job, "plan": plan}


def recap_period_name(date_from: str, date_to: str) -> str:
    """"October 2026" for a whole month, "2026" for a whole year, else "2026-10-03 – 2026-10-20". Pure function."""
    import calendar
    try:
        y1, m1, d1 = (int(x) for x in date_from.split("-"))
        y2, m2, d2 = (int(x) for x in date_to.split("-"))
    except ValueError:
        return f"{date_from} – {date_to}"
    if (y1, m1, d1) == (y2, 1, 1) and (m2, d2) == (12, 31):
        return str(y1)
    if (y1, m1) == (y2, m2) and d1 == 1 and d2 == calendar.monthrange(y1, m1)[1]:
        return f"{calendar.month_name[m1]} {y1}"
    return f"{date_from} – {date_to}"


def cancel_squad_reel() -> None:
    if _squad_job and _squad_job.get("status") == "running":
        _squad_job.update({"status": "error", "stage": "cancelled", "error": "cancelled"})
