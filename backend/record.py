"""Record highlights to MP4 by driving CS2 under HLAE (Windows only).

Pipeline (same building blocks as CS Demo Manager, but without its server plugin):
  1. make sure a plain .dem is inside <CS2>/game/csgo/
  2. write <CS2>/game/csgo/cfg/cs2watcher_rec.cfg: per highlight an alias + `mirv_cmd addAtTick` entries that
     jump to the highlight, spectate the player, `mirv_streams record start/end`, then `quit` at the end
  3. launch CS2 through HLAE with the demo and `+exec cs2watcher_rec`   -> launch_game(), see below
  4. poll until cs2.exe exits (or timeout); HLAE wrote <raw>/seqN/video.mp4 + take0000/audio.wav
  5. mux audio with FFmpeg into data/videos/<demo>/<n>_<type>_r<round>_<player>.mp4, concat a reel.mp4
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
    return {"videos": [], "reel": None, "long": []}


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
        "cl_draw_only_deathnotices 1", "mirv_deathmsg lifetime 5", "spec_show_xray 0", "mp_display_kill_assists 1",
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
        setup = max(96, start - int(0.75 * tickrate))
        jump_from = 100 if i == 1 else prev_end + 16
        folder = raw_dir / f"seq{i}"
        rel = hlae_rel_name(demo_id_for_cfg, i)
        player = names.get(str(h["player"]), h.get("player_name", ""))
        token = player.split()[0] if player else ""
        name = f"{i:02d}_{_safe(h['type'])}_r{h['round']}_{_safe(player, 16)}"
        preset = f"p{i}"
        lines += [
            f'mirv_streams settings add ffmpeg {preset} "-c:v libx264 -preset veryfast -crf {crf} -pix_fmt yuv420p '
            f'{{QUOTE}}{_fwd(folder)}\\\\video.mp4{{QUOTE}}"',
            f'alias hl_s{i} "mirv_streams record name {rel}; mirv_streams record screen settings {preset}; '
            f'spec_mode 1; spec_player {token}; echo [cs2watcher] seq {i} ready"',
            f"mirv_cmd addAtTick {jump_from} demo_gototick {setup - 1}",
            f"mirv_cmd addAtTick {setup} hl_s{i}",
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


def build_reel(outputs: list[dict], out_dir: Path) -> dict | None:
    if len(outputs) < 2:
        return None
    lst = out_dir / "reel_list.txt"
    lst.write_text("".join(f"file '{(out_dir / o['file']).as_posix()}'\n" for o in outputs), encoding="utf-8")
    reel_path = out_dir / "reel.mp4"
    r = subprocess.run([str(FFMPEG_EXE), "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                        "-movflags", "+faststart", str(reel_path)], capture_output=True, text=True)
    lst.unlink(missing_ok=True)
    if r.returncode == 0 and reel_path.exists():
        return {"file": reel_path.name, "size": reel_path.stat().st_size}
    return None


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


def pick_long_clips(index: dict, highlight_ids: list[int], cinematic: bool) -> list[dict]:
    """Recorded clips for the given highlights, in match order; cinematic versions when asked and available."""
    want = set(highlight_ids)
    vids = sorted((v for v in index.get("videos", []) if v["highlight_id"] in want), key=lambda v: v["start_tick"])
    clips = []
    for v in vids:
        use_cine = bool(cinematic and v.get("cine"))
        clips.append({"highlight_id": v["highlight_id"], "file": v["cine"] if use_cine else v["file"], "cine": use_cine,
                      "audio": True if use_cine else bool(v.get("audio", True)),
                      "seconds": float((v.get("cine_info") or {}).get("seconds") or v.get("seconds") or 0) if use_cine
                      else float(v.get("seconds") or 0),
                      "description": v.get("description", ""), "player": v.get("player", "")})
    return clips


def compile_long_video(demo_id: str, highlight_ids: list[int], cinematic: bool = False) -> dict:
    """Join the recorded clips of the selected highlights into one MP4 and register it in videos.json."""
    if not FFMPEG_EXE.exists():
        raise RuntimeError("FFmpeg is not installed (Install HLAE + FFmpeg)")
    out_dir = VIDEOS_DIR / demo_id
    index = videos_index(demo_id)
    clips = pick_long_clips(index, highlight_ids, cinematic)
    clips = [c for c in clips if (out_dir / c["file"]).exists()]
    if len(clips) < 2:
        raise RuntimeError("select at least two highlights that already have a recorded clip")
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
        entry["exported"] = str(export_clips(demo_id, [], entry, cfg))
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

    Long videos and reels built earlier are left alone: they are finished files of their own.
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


def export_clips(demo_id: str, outputs: list[dict], reel: dict | None, cfg: dict | None = None) -> Path:
    dst = export_folder_for(demo_id, cfg)
    dst.mkdir(parents=True, exist_ok=True)
    src_dir = VIDEOS_DIR / demo_id
    names = [o["file"] for o in outputs] + ([reel["file"]] if reel else [])
    for name in names:
        src = src_dir / name
        if src.exists():
            target = dst / name
            if not target.exists() or target.stat().st_size != src.stat().st_size:
                shutil.copyfile(src, target)
    return dst


def write_index(demo_id: str, outputs: list[dict], reel: dict | None) -> dict:
    out_dir = VIDEOS_DIR / demo_id
    index = videos_index(demo_id)
    by_id = {v["highlight_id"]: v for v in index.get("videos", [])}
    for o in outputs:
        by_id[o["highlight_id"]] = o
    index = {"videos": sorted(by_id.values(), key=lambda v: v["start_tick"]), "reel": reel or index.get("reel"),
             "reel_cine": index.get("reel_cine"), "long": index.get("long", []),
             "updated": time.time(), "export_folder": str(export_folder_for(demo_id))}
    (out_dir / "videos.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    return index


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
        _set(stage="building reel")
        reel = build_reel(outputs, out_dir)
        write_index(demo_id, outputs, reel)
        _set(stage="copying to Downloads")
        try:
            exported = str(export_clips(demo_id, outputs, reel, cfg))
        except Exception as e:  # noqa: BLE001
            exported = f"export failed: {e}"
        _set(exported=exported)
        if all(o.get("audio") for o in outputs):
            shutil.rmtree(raw_dir, ignore_errors=True)
            shutil.rmtree(take_root, ignore_errors=True)
        else:
            (out_dir / "raw_files.txt").write_text("\n".join(_tree(raw_dir, 500)), encoding="utf-8")
            (out_dir / "console_tail.txt").write_text("\n".join(_console_tail(csgo_dir, 2000)), encoding="utf-8")
        _set(status="done", stage="done", progress=1.0, outputs=outputs, reel=reel,
             warning=None if all(o.get("audio") for o in outputs) else "no game audio was found for some clips; raw files kept in " + str(raw_dir))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        _set(status="error", stage="error", error=str(e))


# ---- cinematic post-processing job (slow-motion kills + music) ------------------------------

_cine_job: dict | None = None


def cinematic_status() -> dict | None:
    return dict(_cine_job) if _cine_job else None


def start_cinematic(demo_id: str, videos: list[dict], overrides: dict | None = None) -> dict:
    """Process the given clips in a background thread; `overrides` are cine_* options on top of config.toml."""
    global _cine_job
    if _cine_job and _cine_job.get("status") == "running":
        raise RuntimeError("cinematic processing already running")
    _cine_job = {"demo_id": demo_id, "status": "running", "total": len(videos), "done": 0, "current": videos[0]["file"], "error": None}

    def run():
        global _cine_job
        from . import cinematic as cine
        ffprobe = FFMPEG_DIR / "bin" / "ffprobe.exe"
        try:
            for i, v in enumerate(videos):
                _cine_job.update({"current": v["file"], "done": i})
                nv = cine.cinematic_for_video(demo_id, v, FFMPEG_EXE, ffprobe, VIDEOS_DIR, overrides=overrides)
                idx = videos_index(demo_id)
                idx["videos"] = [nv if x["highlight_id"] == nv["highlight_id"] else x for x in idx["videos"]]
                (VIDEOS_DIR / demo_id / "videos.json").write_text(json.dumps(idx, indent=1), encoding="utf-8")
                try:
                    export_clips(demo_id, [{"file": nv["cine"]}], None)
                except Exception:  # noqa: BLE001
                    pass
            idx = videos_index(demo_id)
            cines = [{"file": v["cine"]} for v in idx["videos"] if v.get("cine")]
            if len(cines) > 1:
                lst = VIDEOS_DIR / demo_id / "reel_cine_list.txt"
                lst.write_text("".join(f"file '{(VIDEOS_DIR / demo_id / c['file']).as_posix()}'\n" for c in cines), encoding="utf-8")
                reel = VIDEOS_DIR / demo_id / "reel_cinematic.mp4"
                r = subprocess.run([str(FFMPEG_EXE), "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                                    "-movflags", "+faststart", str(reel)], capture_output=True, text=True)
                lst.unlink(missing_ok=True)
                if r.returncode == 0:
                    idx["reel_cine"] = {"file": reel.name, "size": reel.stat().st_size}
                    (VIDEOS_DIR / demo_id / "videos.json").write_text(json.dumps(idx, indent=1), encoding="utf-8")
                    try:
                        export_clips(demo_id, [], {"file": reel.name})
                    except Exception:  # noqa: BLE001
                        pass
            _cine_job.update({"status": "done", "done": len(videos)})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _cine_job.update({"status": "error", "error": str(e)})

    threading.Thread(target=run, daemon=True, name="cinematic").start()
    return dict(_cine_job)
