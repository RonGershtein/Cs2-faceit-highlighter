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
    recover_recordings([demo_id])   # before this recording clears the raw folder
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
    """What the recording shows of CS2's HUD. "killfeed" (the default): a clean picture with only the kill feed.
    "health_ammo": the full HUD with health, armour and ammo; radar, name tags and the target id are hidden, but
    CS2 keeps the players bar at the top. Pure function; unit-tested."""
    if mode != "health_ammo":
        return ["cl_draw_only_deathnotices 1"]
    return ["cl_draw_only_deathnotices 0", "cl_drawhud_force_radar -1", "cl_drawhud_force_teamid_overhead -1",
            "hud_showtargetid 0", "cl_drawhud_force_deathnotices 1"]


# the video encoders HLAE's FFmpeg can record with: the CPU (x264) or a graphics card's own encoder chip, which leaves
# the CPU to the game; the GPU ones at a quantizer a little finer than the x264 crf so the quality holds
ENCODERS = {
    "x264": "-c:v libx264 -preset veryfast -crf {crf} -pix_fmt yuv420p",
    # the GPU encoders at a capped bitrate: at a fixed quantizer AMF wrote 80 Mbit/s, 4.6x the size of x264's files
    "nvenc": "-c:v h264_nvenc -preset p5 -tune hq -rc vbr -cq {qp} -b:v 20M -maxrate 32M -bufsize 40M -pix_fmt yuv420p",
    "amf": "-c:v h264_amf -quality quality -rc vbr_peak -b:v 20M -maxrate 32M -bufsize 40M -pix_fmt yuv420p",
    "qsv": "-c:v h264_qsv -preset medium -global_quality {qp} -pix_fmt nv12",
}
VENDOR_ENCODER = {"nvidia": "nvenc", "amd": "amf", "intel": "qsv"}
ENCODER_NAMES = {"x264": "CPU (x264)", "nvenc": "NVIDIA (NVENC)", "amf": "AMD (AMF)", "qsv": "Intel (Quick Sync)"}
_auto_encoder: str | None = None   # what "auto" resolved to on this PC (resolve_encoder)


def encoder_args(cfg: dict, auto: str | None = None) -> str:
    """The FFmpeg options HLAE records with. record_encoder: "auto" (the default: the graphics card's encoder when
    a test encode with it worked on this PC, see resolve_encoder, else the CPU), "x264", "nvenc", "amf" or "qsv".
    Pure function; unit-tested."""
    crf = int(cfg.get("record_crf", 20))
    enc = str(cfg.get("record_encoder", "auto")).lower()
    if enc == "auto":
        enc = auto or _auto_encoder or "x264"
    return ENCODERS.get(enc, ENCODERS["x264"]).format(crf=crf, qp=max(10, crf - 2))


def gpu_vendors() -> list[str]:
    """The graphics card makers of this PC, the dedicated card first: ["nvidia"], ["amd", "intel"], …"""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-CimInstance Win32_VideoController).Name"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.lower().splitlines():
        for key, vendor in (("nvidia", "nvidia"), ("geforce", "nvidia"), ("radeon", "amd"), ("amd", "amd"), ("intel", "intel")):
            if key in line and vendor not in found:
                found.append(vendor)
    return sorted(found, key=lambda v: v == "intel")   # an integrated Intel GPU last


def encoder_works(enc: str) -> bool:
    """Whether HLAE's FFmpeg really encodes with `enc` on this PC: a second of 1080p60 test video, as a recording."""
    args = [str(FFMPEG_EXE), "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=60", "-frames:v", "60"]
    args += ENCODERS[enc].format(crf=20, qp=18).split() + ["-f", "null", "-"]
    try:
        return subprocess.run(args, capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def pick_encoder(vendors: list[str], works) -> str:
    """The first graphics card encoder (in vendor order) that works, else "x264". Pure function; unit-tested."""
    for v in vendors:
        enc = VENDOR_ENCODER.get(v)
        if enc and works(enc):
            return enc
    return "x264"


def resolve_encoder(force: bool = False) -> str:
    """What record_encoder "auto" means on this PC, found once (the card is asked, its encoder tried) and kept in
    data/encoder.json until FFmpeg changes."""
    global _auto_encoder
    if _auto_encoder and not force:
        return _auto_encoder
    p = DATA_DIR / "encoder.json"
    stamp = FFMPEG_EXE.stat().st_mtime if FFMPEG_EXE.exists() else 0
    try:
        j = json.loads(p.read_text(encoding="utf-8")) if p.is_file() and not force else {}
    except (OSError, ValueError):
        j = {}
    if j.get("ffmpeg") == stamp and j.get("encoder") in ENCODERS:
        _auto_encoder = j["encoder"]
        return _auto_encoder
    vendors = gpu_vendors() if is_windows() else []
    _auto_encoder = pick_encoder(vendors, encoder_works) if FFMPEG_EXE.exists() else "x264"
    try:
        p.write_text(json.dumps({"ffmpeg": stamp, "encoder": _auto_encoder, "vendors": vendors}), encoding="utf-8")
    except OSError:
        pass
    print(f"[record] encoder for 'auto': {ENCODER_NAMES[_auto_encoder]} (graphics: {', '.join(vendors) or 'unknown'})", flush=True)
    return _auto_encoder


SWOOP_FOLLOW = 3.0    # seconds the camera follows behind and above the player before diving in
SWOOP_DIVE = 1.0      # seconds of the dive into his eyes
SWOOP_MIN_FOLLOW = 1.5   # less room than this before the play (round start, the play before): no swoop
SWOOP_LEAD = 2.0      # it lands this long before the play's first kill (where the cinematic play starts in first person)
SWOOP_STEP = 0.25     # seconds between follow-cam keyframes (dense: HLAE's spline must not cut corners into walls)
SWOOP_BACK = 110.0    # how far behind him the camera is, measured on the path he walked
SWOOP_UP = 22.0       # and how much above his eyes (well under any ceiling he walks under)
SWOOP_TRAIL = 3.0     # seconds of his path read before the swoop (the camera's first spot is somewhere on it)
SWOOP_TRAIL_TICKS = 4 # the path is read every this many ticks


def swoop_times(follow: float, dive: float = SWOOP_DIVE) -> list[float]:
    """Keyframe times (seconds from the swoop's start): every SWOOP_STEP while following, then three through the
    dive, the last one at the landing. Pure function; unit-tested."""
    n = max(1, int(round(follow / SWOOP_STEP)))
    ts = [round(follow * i / n, 3) for i in range(n + 1)]
    return ts + [round(follow + dive * f, 3) for f in (0.4, 0.75, 1.0)]


def _unwrap(angles: list[float]) -> list[float]:
    out = []
    for a in angles:
        if out:
            while a - out[-1] > 180:
                a -= 360
            while a - out[-1] < -180:
                a += 360
        out.append(a)
    return out


def _trail_spot(trail: list[dict], i: int, back: float) -> dict:
    """The point of his path before sample i that is `back` units from where he is at i: the latest place he stood
    that far away, so the space between it and him is space he just walked through (no walls in between). The
    farthest point there is when he has not come that far."""
    import math
    p = trail[i]

    def dist(e):
        return math.dist((e["x"], e["y"], e["z"]), (p["x"], p["y"], p["z"]))
    best = p
    for j in range(i - 1, -1, -1):
        d = dist(trail[j])
        if d >= back:
            near, dn = trail[j + 1], dist(trail[j + 1])
            f = (back - dn) / (d - dn) if d > dn else 1.0
            return {k: near[k] + (trail[j][k] - near[k]) * f for k in ("x", "y", "z")}
        if d > dist(best):
            best = trail[j]
    return best


def swoop_points(trail: list[dict], pre: int, step: float, follow: float, dive: float = SWOOP_DIVE) -> list[dict]:
    """Camera keyframes {"t", "x", "y", "z", "pitch", "yaw"} of the swoop. `trail` is his eyes every `step` seconds,
    `pre` samples before the swoop's start and on to its end. The camera rides his own path SWOOP_BACK behind him and
    SWOOP_UP above, looking at him (walls cannot come between: he was just there), then dives along that path into
    his eyes, landing exactly on his view. Pure function; unit-tested."""
    import math
    out, look = [], []
    for t in swoop_times(follow, dive):
        i = min(len(trail) - 1, pre + int(round(t / step)))
        e = trail[i]
        f = 0.0 if t <= follow else min(1.0, (t - follow) / dive)
        ease = f * f * (3 - 2 * f)
        back, up = SWOOP_BACK * (1 - ease), SWOOP_UP * (1 - ease)
        c = _trail_spot(trail, i, back) if back > 0.5 else e
        c = {"x": c["x"], "y": c["y"], "z": c["z"] + up}
        dx, dy, dz = e["x"] - c["x"], e["y"] - c["y"], (e["z"] - 8) - c["z"]
        flat = math.hypot(dx, dy)
        if flat > 20:      # look at him (a little under his eyes: head and body in the shot)
            yaw, pitch = math.degrees(math.atan2(dy, dx)), math.degrees(math.atan2(-dz, flat))
        else:              # too close to aim: his own view
            yaw, pitch = e["yaw"], e["pitch"]
        look.append((yaw, pitch, ease, e))
        out.append({"t": t, "x": round(c["x"], 2), "y": round(c["y"], 2), "z": round(c["z"], 2)})
    yaws = _unwrap([y for y, _, _, _ in look])
    own = _unwrap([e["yaw"] for _, _, _, e in look])
    for o, y, w, (_, pitch, ease, e) in zip(out, yaws, own, look):
        w += round((y - w) / 360) * 360           # his view on the same turn as the camera's
        o["yaw"] = round(y + (w - y) * ease, 2) if ease < 1 else round(w, 2)
        o["pitch"] = round(pitch + (e["pitch"] - pitch) * ease, 2)
    return out


def campath_xml(points: list[dict], fov: float = 90.0) -> str:
    """HLAE's campath file (mirv_campath load): <campath><points><p t x y z fov rx(roll) ry(pitch) rz(yaw)/>.
    Pure function; unit-tested."""
    rows = "".join(f'<p t="{p["t"]}" x="{p["x"]}" y="{p["y"]}" z="{p["z"]}" fov="{fov:g}" rx="0" ry="{p["pitch"]}" '
                   f'rz="{p["yaw"]}"/>' for p in points)
    return f'<?xml version="1.0"?><campath><points>{rows}</points></campath>'


def prepare_swoops(hs: list[dict], dem: Path, raw_dir: Path, tickrate: int, rounds: list[dict] | None = None) -> dict:
    """{highlight id: {"from", "to", "xml"}}: for every play with room for it, the swoop's ticks and its campath file
    (written to raw_dir), landing on the player's eyes as the demo has them. Every play gets one; the montage
    decides which ones are shown. {} when the demo cannot be read (the recording goes on without swoops)."""
    freeze_end = {int(r.get("num") or 0): int(r.get("freeze_end") or r.get("start") or 0) for r in rounds or []}
    plans = {h["id"]: (plan_swoop(h, freeze_end.get(int(h.get("round") or 0), int(h["start_tick"])), tickrate),
                       str(h["player"])) for h in hs}
    plans = {k: v for k, v in plans.items() if v[0]}
    if not plans:
        return {}

    pre = int(round(SWOOP_TRAIL * tickrate / SWOOP_TRAIL_TICKS))

    def trail_ticks(pl: dict) -> list[int]:
        first = pl["from"] - pre * SWOOP_TRAIL_TICKS
        return list(range(first, pl["to"], SWOOP_TRAIL_TICKS)) + [pl["to"]]   # the last one: the landing
    try:
        from .parser import eye_views
        eyes = eye_views(dem, [(t, sid) for pl, sid in plans.values() for t in trail_ticks(pl)])
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return {}
    raw_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for hid, (pl, sid) in plans.items():
        trail = [eyes.get((t, sid)) for t in trail_ticks(pl)]
        land = eyes.get((pl["to"], sid))
        if not land:
            continue
        for j, e in enumerate(trail):     # a tick the demo lacks (dead before the round, a gap): the next one it has
            if not e:
                trail[j] = next((n for n in trail[j + 1:] if n), land)
        trail[-1] = land
        xml = raw_dir / f"swoop_{hid}.xml"
        xml.write_text(campath_xml(swoop_points(trail, pre, SWOOP_TRAIL_TICKS / tickrate, pl["follow"])),
                       encoding="utf-8")
        out[hid] = {**pl, "xml": str(xml)}
    return out


def plan_swoop(h: dict, earliest: int, tickrate: int) -> dict | None:
    """{"from", "to", "follow"} of a play's swoop: it lands SWOOP_LEAD before the first kill after SWOOP_FOLLOW s of
    follow cam and SWOOP_DIVE s of dive, starting no earlier than `earliest` (the end of the round's freeze time):
    the follow gets shorter there, and below SWOOP_MIN_FOLLOW there is no swoop. The recording is started early
    enough for it (build_cfg). Pure function; unit-tested."""
    kills = sorted(h.get("kills") or [])
    if not kills:
        return None
    to = int(kills[0] - SWOOP_LEAD * tickrate)
    follow = min(SWOOP_FOLLOW, (to - SWOOP_DIVE * tickrate - earliest) / tickrate)
    if follow < SWOOP_MIN_FOLLOW:
        return None
    return {"from": int(round(to - (follow + SWOOP_DIVE) * tickrate)), "to": to, "follow": round(follow, 3)}


PREROLL_TIMESCALE = 4   # record_fast_preroll: how fast the unrecorded 2 s before each highlight play


def build_cfg(hs: list[dict], raw_dir: Path, names: dict[str, str], cfg: dict, tickrate: int,
              demo_id_for_cfg: str = "demo", part: int = 1, next_cfg: str | None = None, playdemo: str | None = None,
              demoui: bool = True, swoops: dict | None = None) -> tuple[str, list[dict]]:
    """Return (cfg text, sequences). Pure function; unit-tested.
    One CS2 session can record several demos: part N > 1 is exec'd by the previous demo's cfg once its last
    highlight is recorded; it clears the schedule, schedules its own highlights and loads its demo (`playdemo`,
    HLAE forgets the old demo's ticks on level load). next_cfg: exec that instead of quitting at the end.
    demoui: toggle the demo playback panel off at this demo's first highlight.
    swoops: {highlight id: {"from", "to" (ticks), "xml": campath file}} - the camera leaves the player's eyes for a
    third-person swoop (HLAE's mirv_campath) that lands back in them at "to"."""
    fps = int(cfg.get("record_fps", 60))
    enc = encoder_args(cfg)
    fast_preroll = bool(cfg.get("record_fast_preroll", False))
    tag = "" if part == 1 else str(part)          # part 1 keeps the plain names (hl_s1, p1)
    lines = [
        f'echo "[cs2watcher] {CFG_NAME}{"_" + tag if tag else ""} loaded"',
        "sv_cheats 1", "volume 1", "cl_hud_telemetry_frametime_show 0", "cl_hud_telemetry_net_misdelivery_show 0",
        "cl_hud_telemetry_ping_show 0", "cl_hud_telemetry_serverrecvmargin_graph_show 0",
        "cl_trueview_show_status 0", "r_show_build_info 0", "cl_demo_predict 0",
        *hud_cvars(str(cfg.get("record_hud", "killfeed"))),
        "mirv_deathmsg lifetime 5", "spec_show_xray 0", "mp_display_kill_assists 1",
        "mirv_streams record screen enabled 1", "mirv_streams record startMovieWav 1",
        f"mirv_streams record fps {fps}",
        "mirv_cmd clear", "mirv_cmd enabled 1",
    ]
    seqs = []
    prev_end = 0
    for i, h in enumerate(hs, 1):
        start, end = int(h["start_tick"]), int(h["end_tick"])
        sw = (swoops or {}).get(h["id"])
        if sw and sw["from"] - 8 < start:   # the follow cam starts before the play: record from there
            start = sw["from"] - 8
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
        preset = f"p{tag}_{i}" if tag else f"p{i}"
        sa, pa = f"hl{tag}_s{i}", f"hl{tag}_p{i}"
        lines += [
            f'mirv_streams settings add ffmpeg {preset} "{enc} '
            f'{{QUOTE}}{_fwd(folder)}\\\\video.mp4{{QUOTE}}"',
            f'alias {sa} "mirv_streams record name {rel}; mirv_streams record screen settings {preset}; '
            f'{spec}; echo [cs2watcher] seq {i} ready"',
            f'alias {pa} "{spec}"',
            f"mirv_cmd addAtTick {jump_from} demo_gototick {setup - 1}",
            f"mirv_cmd addAtTick {setup} {sa}",
            # the seek can swallow the first spectator switch (CS2 is still rebuilding entities): repeat it
            f"mirv_cmd addAtTick {setup + tickrate // 2} {pa}",
            f"mirv_cmd addAtTick {max(setup + 1, start - 8)} {pa}",
        ]
        if i == 1 and demoui:
            # the demo playback panel is open while the demo plays; `demoui` toggles it, so close it once, on a
            # demo tick (straight from the cfg it would fire before the demo has loaded). From PR #1 by slipknot94.
            lines.append(f"mirv_cmd addAtTick {setup} demoui")
        if fast_preroll:
            # the 2 s before the highlight are not recorded: play them faster, back to normal well before the
            # last spectator switch and the recording start
            lines += [f"mirv_cmd addAtTick {setup + 1} demo_timescale {PREROLL_TIMESCALE}",
                      f"mirv_cmd addAtTick {max(setup + 2, start - 8 - max(16, tickrate // 4))} demo_timescale 1"]
        swoop = None
        swoop_cfg = None
        if sw and sw["from"] >= start + 4 and sw["to"] < end:
            # its own little cfg: the campath's path has to be quoted (the console splits "C:/..." at the colon), and
            # quotes cannot go inside an alias
            ca, ce = f"{CFG_NAME}_sw{tag}_{i}", f"hl{tag}_swe{i}"
            swoop_cfg = {"name": ca, "text": "\r\n".join([
                # no HUD while the camera is outside: a scoped player's sniper scope would cover the shot
                cfg.get("record_swoop_spec", "spec_mode 5"), "cl_drawhud 0", "mirv_campath clear",
                f'mirv_campath load "{_fwd(Path(sw["xml"]))}"', "mirv_campath edit start", "mirv_campath enabled 1",
                f'echo "[cs2watcher] swoop {i}"']) + "\r\n"}
            lines += [
                f'alias {ce} "mirv_campath enabled 0; cl_drawhud 1; {spec}"',
                f"mirv_cmd addAtTick {sw['from']} exec {ca}",
                f"mirv_cmd addAtTick {sw['to']} {ce}",
            ]
            swoop = {"from": round((sw["from"] - start) / tickrate, 3), "to": round((sw["to"] - start) / tickrate, 3)}
        lines += [
            f"mirv_cmd addAtTick {start} mirv_streams record start",
            f"mirv_cmd addAtTick {end} mirv_streams record end",
        ]
        seqs.append({"n": i, "highlight_id": h["id"], "name": name, "folder": folder, "rel": rel, "start": start, "end": end,
                     "type": h["type"], "round": h["round"], "player": player, "description": h["description"],
                     "player_steamid": str(h["player"]), **({"swoop": swoop, "swoop_cfg": swoop_cfg} if swoop else {})})
        prev_end = end
    if next_cfg:
        lines.append(f"mirv_cmd addAtTick {prev_end + 2 * tickrate} exec {next_cfg}")   # as long as the quit: HLAE finishes the file
    elif cfg.get("record_close_game", True):
        lines.append(f"mirv_cmd addAtTick {prev_end + 2 * tickrate} quit")
    lines.append('echo "[cs2watcher] commands scheduled"')
    if playdemo:
        lines.append(f'playdemo "{playdemo}"')
    return "\r\n".join(lines) + "\r\n", seqs


def cfg_ticks(cfg_text: str) -> list[int]:
    """The demo ticks a cfg schedules commands at. Pure function."""
    return [int(ln.split()[2]) for ln in cfg_text.splitlines() if ln.startswith("mirv_cmd addAtTick ")]


def handover_tick(cfg_text: str) -> int | None:
    """The tick at which a cfg hands over to the next demo's cfg (None when it quits). Pure function."""
    for ln in cfg_text.splitlines():
        if ln.startswith("mirv_cmd addAtTick ") and " exec " in ln:
            return int(ln.split()[2])
    return None


def can_chain(handover: int, next_ticks: list[int], tickrate: int) -> bool:
    """Whether the next demo's commands can be scheduled at `handover` in the current demo: none of them may lie
    in the few ticks the current demo still plays before the next one loads (they would fire in the wrong demo).
    Pure function; unit-tested."""
    return not any(handover - 2 <= t <= handover + 4 * tickrate for t in next_ticks)


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
        o = encode_one(s, out_dir, tickrate)
        if o is None:
            continue
        outputs.append(o)
        if progress:
            progress((i + 1) / len(seqs), outputs)
    return outputs


def encode_one(s: dict, out_dir: Path, tickrate: int) -> dict | None:
    """Mux one recorded sequence into its clip; its videos.json entry, or None when nothing was recorded."""
    out_dir.mkdir(parents=True, exist_ok=True)
    video = Path(s["folder"]) / "video.mp4"
    if not video.exists():
        return None
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
    entry = {**planned_entry(s, tickrate), "size": out.stat().st_size, "audio": bool(wavs)}
    return check_length(entry, out)


SHORT_BY = 1.5   # seconds a recording may come out shorter than its highlight before it counts as cut off


def check_length(entry: dict, path: Path) -> dict:
    """The clip entry with its real length: when the recording stopped early (CS2 or HLAE under strain), "seconds" is
    what the file holds and "short" marks it, so it is recorded again (is_recorded) and its title counts only the
    kills it shows."""
    real = clip_seconds(path)
    if real and real < float(entry.get("seconds") or 0) - SHORT_BY:
        print(f"[record] {path.name} stopped early: {real:.1f} s of {entry['seconds']} s", flush=True)
        return {**entry, "seconds": round(real, 1), "expected": entry["seconds"], "short": True}
    return entry


def clip_seconds(path: Path) -> float:
    from . import cinematic as cine
    try:
        return cine.probe_duration(FFMPEG_DIR / "bin" / "ffprobe.exe", path)
    except OSError:
        return 0.0


def is_recorded(v: dict) -> bool:
    """A clip that holds its whole highlight (one cut off early is recorded again). Pure function."""
    return not v.get("short")


def planned_entry(s: dict, tickrate: int) -> dict:
    """The videos.json entry a sequence will get once recorded (without "size" and "audio"). Pure function."""
    out = {"highlight_id": s["highlight_id"], "file": f"{s['name']}.mp4", "type": s["type"], "round": s["round"],
           "player": s["player"], "player_steamid": s["player_steamid"], "description": s["description"],
           "start_tick": s["start"], "end_tick": s["end"], "seconds": round((s["end"] - s["start"]) / tickrate, 1)}
    if s.get("swoop"):
        out["swoop"] = s["swoop"]   # seconds into the clip: the third-person shot, back in his eyes at "to"
    return out


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


class FileBusy(RuntimeError):
    """A video could not be deleted because Windows still has it open (a player is showing it)."""


def remove_video_file(p: Path, tries: int = 6, wait: float = 0.5) -> None:
    """Delete a video file. On Windows a file that is still being streamed to a player cannot be deleted, so
    retry for a few seconds (the page releases its players first) before giving up with FileBusy."""
    for i in range(tries):
        try:
            p.unlink(missing_ok=True)
            return
        except PermissionError:
            if i == tries - 1:
                raise FileBusy(f"{p.name} is still open in a video player. Close the player (or wait a moment) and delete it again.")
            time.sleep(wait)


def delete_long_video(demo_id: str, name: str) -> None:
    out_dir = VIDEOS_DIR / demo_id
    index = videos_index(demo_id)
    remove_video_file(out_dir / name)
    index["long"] = [e for e in index.get("long", []) if e["file"] != name]
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
    removed, busy = 0, set()
    folders = [out_dir] + ([export_folder_for(demo_id)] if downloads else [])
    for folder in folders:
        for n in names:
            f = folder / n
            if f.exists():
                try:
                    remove_video_file(f, tries=3)
                    removed += 1
                except (FileBusy, OSError):
                    busy.add(n)
    # a clip that is still open in a player stays listed, so deleting it again later works
    gone = [v for v in gone if not ({v.get("file"), v.get("cine")} & busy)]
    want = {v["highlight_id"] for v in gone}
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
        remove_video_file(MONTAGE_DIR / name)
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
        remove_video_file(out_dir / n)   # before the index changes: a busy file stays listed, so it can be retried
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


def plan_sessions(parts: list[dict], cfg: dict) -> list[list[dict]]:
    """Group the demos to record into CS2 sessions: one session records demo after demo (each demo's cfg execs
    the next one's, which loads it with `playdemo`), and a new session starts only where a demo's commands would
    collide with the handover (can_chain). Each part gets "cfg_name" and "cfg_text". Pure function; unit-tested."""
    sessions: list[list[dict]] = []
    for p in parts:
        cur = sessions[-1] if sessions else None
        if cur:
            prev = cur[-1]
            text, _ = build_cfg(p["hs"], p["raw_dir"], p["names"], cfg, p["tickrate"], p["demo_id"], part=len(cur) + 1,
                                swoops=p.get("swoops"),
                                playdemo=p["dem_name"], demoui=cfg.get("record_demoui_each_demo", True))
            handover = prev["last_end"] + 2 * prev["tickrate"]
            if can_chain(handover, cfg_ticks(text), p["tickrate"]):
                cur.append(p)
                continue
        sessions.append([p])
    for sess in sessions:
        for j, p in enumerate(sess):
            p["cfg_name"] = CFG_NAME if j == 0 else f"{CFG_NAME}_{j + 1}"
            nxt = f"{CFG_NAME}_{j + 2}" if j + 1 < len(sess) else None
            p["cfg_text"], p["seqs"] = build_cfg(p["hs"], p["raw_dir"], p["names"], cfg, p["tickrate"], p["demo_id"],
                                                 swoops=p.get("swoops"),
                                                 part=j + 1, next_cfg=nxt, playdemo=p["dem_name"] if j else None,
                                                 demoui=j == 0 or cfg.get("record_demoui_each_demo", True))
    return sessions


def _keep_session_log(csgo_dir: Path, sess: list[dict], si: int) -> None:
    """Copy CS2's console log of a recording session (it is overwritten by the next launch) and note which demo
    recorded how many clips, into data/videos/<demo>/record_log.txt of every demo of the session."""
    try:
        log = (csgo_dir / "console.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        log = "(no console.log)"
    lines = [f"session {si + 1} · {time.strftime('%Y-%m-%d %H:%M:%S')}"]
    for p in sess:
        got = sum(1 for sq in p["seqs"] if (Path(sq["folder"]) / "video.mp4").exists())
        lines.append(f"  {p['cfg_name']}: {p['demo_id']} ({p['dem_name']}) recorded {got} of {len(p['seqs'])}")
    text = "\n".join(lines) + "\n\n---- configs ----\n" + "\n\n".join(f"# {p['cfg_name']}\n{p['cfg_text']}" for p in sess) \
        + "\n\n---- CS2 console.log ----\n" + log
    for p in sess:
        try:
            p["out_dir"].mkdir(parents=True, exist_ok=True)
            (p["out_dir"] / "record_log.txt").write_text(text, encoding="utf-8")
        except OSError:
            pass


def start_session(parts: list[tuple[str, list[int]]], opts: dict | None = None) -> dict:
    """Record the highlights of several demos in as few CS2 launches as possible (usually one)."""
    global _job
    opts = opts or {}
    recover_recordings([d for d, _ in parts])   # before this recording clears the raw folders
    with _lock:
        if _job and _job["status"] in ("preparing", "recording", "encoding"):
            raise RuntimeError("a recording job is already running")
        ts = tools_status()
        if not ts["ready"]:
            missing = [k for k in ("windows", "hlae", "ffmpeg", "cs2") if not ts[k]]
            raise RuntimeError("recording not available: missing " + ", ".join(missing))
        if cs2_running():
            raise RuntimeError("CS2 is running; close it first")
        todo = []
        for demo_id, ids in parts:
            hs = sorted((h for h in db.get_highlights(demo_id) if h["id"] in set(ids)), key=lambda h: h["start_tick"])
            if hs:
                todo.append((demo_id, hs))
        if not todo:
            raise RuntimeError("no highlights selected")
        _cancel.clear()
        total = sum(len(hs) for _, hs in todo)
        _job = {"demo_id": todo[0][0], "demo_ids": [d for d, _ in todo], "status": "preparing", "progress": 0.0,
                "stage": "preparing", "current": 0, "total": total, "started": time.time(), "error": None, "outputs": [],
                "hint": "", "highlight_ids": [h["id"] for _, hs in todo for h in hs], "part_i": 0, "parts": len(todo)}
        threading.Thread(target=_run_session, args=(todo, opts), daemon=True, name="record").start()
        return dict(_job)


def _run(demo_id: str, hs: list[dict], opts: dict) -> None:
    _run_session([(demo_id, hs)], opts)


def _run_session(todo: list[tuple[str, list[dict]]], opts: dict) -> None:
    cfg = load_config()
    parts: list[dict] = []
    try:
        if str(cfg.get("record_encoder", "auto")).lower() == "auto":
            _set(stage="checking the graphics card's encoder")
            resolve_encoder()
        cs2 = find_cs2_path()
        csgo_dir = cs2 / "game" / "csgo"
        (csgo_dir / "cfg").mkdir(exist_ok=True)
        for k, (demo_id, hs) in enumerate(todo):
            _set(stage=f"preparing demo {k + 1} of {len(todo)}" if len(todo) > 1 else "preparing demo")
            meta = json.loads((DATA_DIR / "parsed" / demo_id / "meta.json").read_text(encoding="utf-8"))
            dem = ensure_dem(demo_id, csgo_dir / f"replay_{demo_id[:8]}.dem")
            raw_dir = raw_dir_for(demo_id)
            shutil.rmtree(raw_dir, ignore_errors=True)
            take_root = hlae_take_root(cs2, demo_id)
            shutil.rmtree(take_root, ignore_errors=True)
            p = {"demo_id": demo_id, "hs": hs, "dem": dem, "dem_name": dem.name, "raw_dir": raw_dir, "take_root": take_root,
                 "names": {pl["steamid"]: pl["name"] for pl in meta["players"]}, "tickrate": int(meta.get("tickrate", 64)),
                 "out_dir": VIDEOS_DIR / demo_id}
            if cfg.get("record_swoop", True):
                _set(stage="planning the third-person shots")
                p["swoops"] = prepare_swoops(hs, dem, raw_dir, p["tickrate"], meta.get("rounds"))
            _, seqs = build_cfg(hs, raw_dir, p["names"], cfg, p["tickrate"], demo_id, swoops=p.get("swoops"))
            p["last_end"] = seqs[-1]["end"]
            parts.append(p)
        sessions = plan_sessions(parts, cfg)
        # what will be recorded, before it is: a montage can plan (and start rendering) on it right away
        _set(planned=[(p["demo_id"], planned_entry(sq, p["tickrate"])) for p in parts for sq in p["seqs"]])
        register_ffmpeg()
        done_before = 0
        timing: list[dict] = []
        for si, sess in enumerate(sessions):
            for p in sess:
                p["out_dir"].mkdir(parents=True, exist_ok=True)
                for sq in p["seqs"]:
                    Path(sq["folder"]).mkdir(parents=True, exist_ok=True)
                    sq["take_dir"] = p["take_root"] / f"seq{sq['n']}"
                write_planned(p)
                (csgo_dir / "cfg" / f"{p['cfg_name']}.cfg").write_text(p["cfg_text"], encoding="utf-8")
                for sq in p["seqs"]:
                    if sq.get("swoop_cfg"):
                        (csgo_dir / "cfg" / f"{sq['swoop_cfg']['name']}.cfg").write_text(sq["swoop_cfg"]["text"], encoding="utf-8")
                (p["out_dir"] / "last_record.cfg").write_text(p["cfg_text"], encoding="utf-8")
            launches = f" (CS2 launch {si + 1} of {len(sessions)})" if len(sessions) > 1 else ""
            _set(stage="launching CS2 under HLAE" + launches, status="recording",
                 hint="CS2 is starting. If nothing records within a minute, open the console and type: exec " + CFG_NAME)
            try:
                (csgo_dir / "console.log").unlink(missing_ok=True)
            except OSError:
                pass
            t_launch = time.time()
            launch_game(sess[0]["dem"], cfg)
            time.sleep(5)
            # ---- monitor: wait for the game to record every sequence of this session and quit ---------------
            seqs = [(p, sq) for p in sess for sq in p["seqs"]]
            expected = sum((sq["end"] - sq["start"]) / p["tickrate"] for p, sq in seqs)
            timeout = 240 + expected * 6 + 25 * len(seqs) + 90 * (len(sess) - 1)   # each extra demo also has to load
            seen_running = False
            gone = 0   # "not running" readings in a row
            t0 = time.time()
            while True:
                if _cancel.is_set():
                    raise RuntimeError("cancelled")
                running, seen_running, gone = game_state(cs2_running(), seen_running, gone)
                _mux_finished(seqs, game_gone=seen_running and not running, timing=timing)
                done = [1 for p, sq in seqs if (Path(sq["folder"]) / "video.mp4").exists()]
                cur = done_before + len(done)
                part_i = next((i for i, p in enumerate(parts) if any(not (Path(sq["folder"]) / "video.mp4").exists() for sq in p["seqs"])), len(parts) - 1)
                which = f" (demo {part_i + 1} of {len(parts)})" if len(parts) > 1 else ""
                _set(current=cur, part_i=part_i, progress=0.05 + 0.75 * cur / max(1, _job["total"]),
                     stage=f"recording highlight {min(cur + 1, _job['total'])} of {_job['total']}{which}" if running else "waiting for CS2")
                if seen_running and not running:
                    break
                if time.time() - t0 > timeout:
                    if not done:
                        raise RuntimeError("timed out: CS2 did not record anything. Close CS2 and try again, or type "
                                           f"'exec {CFG_NAME}' in the CS2 console if the game sat idle.")
                    break
                time.sleep(1)
            done_before += sum(1 for p, sq in seqs if (Path(sq["folder"]) / "video.mp4").exists())
            time.sleep(2)
            _mux_finished(seqs, game_gone=True, timing=timing)
            _log_timing(sess, timing, t_launch)
            # keep what CS2 did in this session: a demo that records nothing can be explained afterwards
            _keep_session_log(csgo_dir, sess, si)

        # ---- encode, demo by demo ----------------------------------------------------------------------------
        all_outputs, warnings, exported = [], [], ""
        for k, p in enumerate(parts):
            _set(status="encoding", stage="muxing audio" + (f" (demo {k + 1} of {len(parts)})" if len(parts) > 1 else ""),
                 raw_files=_tree(p["raw_dir"]) + (_tree(p["take_root"]) if p["take_root"].exists() else []),
                 console=_console_tail(csgo_dir))
            # clips are muxed while the game records the next one; only what is left is done here
            outputs = []
            for j, sq in enumerate(p["seqs"]):
                o = sq["output"] if "output" in sq else encode_one(sq, p["out_dir"], p["tickrate"])
                if o:
                    outputs.append(o)
                _set(progress=0.8 + 0.15 * (k + (j + 1) / len(p["seqs"])) / len(parts), outputs=all_outputs + outputs)
            if not outputs:
                warnings.append(f"no video was produced for demo {k + 1}")
                continue
            write_index(p["demo_id"], outputs)
            _set(stage="copying to Downloads")
            try:
                exported = str(export_clips(p["demo_id"], outputs, cfg))
            except Exception as e:  # noqa: BLE001
                exported = f"export failed: {e}"
            if all(o.get("audio") for o in outputs):
                shutil.rmtree(p["raw_dir"], ignore_errors=True)
                shutil.rmtree(p["take_root"], ignore_errors=True)
            else:
                (p["out_dir"] / "raw_files.txt").write_text("\n".join(_tree(p["raw_dir"], 500)), encoding="utf-8")
                (p["out_dir"] / "console_tail.txt").write_text("\n".join(_console_tail(csgo_dir, 2000)), encoding="utf-8")
                warnings.append("no game audio was found for some clips; raw files kept in " + str(p["raw_dir"]))
            all_outputs += outputs
        if not all_outputs:
            raise RuntimeError("no video files were produced")
        _set(exported=exported)
        _set(status="done", stage="done", progress=1.0, outputs=all_outputs, warning="; ".join(warnings) or None)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        _set(status="error", stage="error", error=str(e))



GAME_GONE_CHECKS = 5   # consecutive "CS2 is not running" checks (about a second apart) before a session ends


def game_state(running_now: bool, seen: bool, gone: int) -> tuple[bool, bool, int]:
    """(running, seen running, "not running" checks in a row) after one check. One failed check is not the game
    quitting: tasklist can fail or time out while CS2 loads a demo with the CPU busy, so a game that was seen
    counts as gone only after GAME_GONE_CHECKS "not running" readings in a row. Pure function; unit-tested."""
    gone = 0 if running_now else gone + 1
    running = running_now or (seen and gone < GAME_GONE_CHECKS)
    return running, seen or running, gone


PLANNED_NAME = "planned.json"   # in a demo's raw folder: what CS2 was told to record there (recover_recordings)
_recover_lock = threading.Lock()


def write_planned(p: dict) -> None:
    """Note next to the raw recordings what they are, so recover_recordings can still make clips of them when
    the app stops watching before CS2 is done (the app closed, or the game seemed gone while it still ran)."""
    seqs = [{**sq, "folder": str(sq["folder"]), "take_dir": str(sq.get("take_dir") or "")}
            for sq in p["seqs"]]
    data = {"demo_id": p["demo_id"], "tickrate": p["tickrate"], "out_dir": str(p["out_dir"]),
            "take_root": str(p["take_root"]), "seqs": seqs}
    try:
        (Path(p["raw_dir"]) / PLANNED_NAME).write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass


def recover_recordings(demo_ids: list[str] | None = None) -> int:
    """Make clips of recordings CS2 finished that never became clips: a raw folder still holding planned.json
    and video files that are not in the match's videos.json. Only while nothing records and CS2 is closed (a
    file of a running game may be incomplete). demo_ids: the matches to look at (None = every match with
    clips). Returns how many clips were made."""
    if _job and _job.get("status") in ("preparing", "recording", "encoding"):
        return 0
    if demo_ids is None:
        demo_ids = [d.name for d in VIDEOS_DIR.iterdir() if d.is_dir()] if VIDEOS_DIR.is_dir() else []
    todo = [(d, raw_dir_for(d) / PLANNED_NAME) for d in demo_ids if d != MONTAGE_ID]
    todo = [(d, pf) for d, pf in todo if pf.is_file()]
    # a recovery already running (the app's start) is waited for, so whoever asks sees the clips it makes
    if not todo or not _recover_lock.acquire(timeout=300):
        return 0
    made = 0
    try:
        if cs2_running():
            return 0
        for demo_id, pf in todo:
            try:
                pl = json.loads(pf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            tr = int(pl.get("tickrate") or 64)
            have = {(v.get("highlight_id"), v.get("file")) for v in videos_index(demo_id).get("videos", [])}
            outs, audio = [], True
            for sq in pl.get("seqs", []):
                if (sq["highlight_id"], f"{sq['name']}.mp4") in have:
                    continue
                o = encode_one(sq, Path(pl.get("out_dir") or VIDEOS_DIR / demo_id), tr)
                if o:
                    outs.append(o)
                    audio = audio and o["audio"]
            if outs:
                write_index(demo_id, outs)
                try:
                    export_clips(demo_id, outs)
                except Exception:  # noqa: BLE001
                    pass
                print(f"[record] recovered {len(outs)} clip(s) of {demo_id} that CS2 recorded unwatched", flush=True)
            made += len(outs)
            pf.unlink(missing_ok=True)
            if outs and audio:   # as after a normal recording; without audio the raw files stay for a look
                shutil.rmtree(raw_dir_for(demo_id), ignore_errors=True)
                if pl.get("take_root"):
                    shutil.rmtree(pl["take_root"], ignore_errors=True)
    finally:
        _recover_lock.release()
    return made


def _mux_finished(seqs: list[tuple[dict, dict]], game_gone: bool, timing: list[dict]) -> None:
    """Mux every sequence HLAE has finished: the next one has started (or the game is gone) and its video file stopped
    growing. Each clip goes into videos.json right away, so a montage can render it while CS2 records the rest.
    Notes when each sequence appeared and last grew (the recording speed, in `timing`)."""
    now = time.time()
    for p, sq in seqs:
        v = Path(sq["folder"]) / "video.mp4"
        if not v.exists():
            continue
        sq.setdefault("seen", now)
        try:
            size = v.stat().st_size
        except OSError:
            continue
        if size != sq.get("size"):
            sq["size"], sq["grew"] = size, now
    for j, (p, sq) in enumerate(seqs):
        if "seen" not in sq or "output" in sq:
            continue
        nxt_started = j + 1 < len(seqs) and "seen" in seqs[j + 1][1]
        if not (nxt_started or game_gone) or now - sq["grew"] < 0.9:
            continue
        o = encode_one(sq, p["out_dir"], p["tickrate"])
        sq["output"] = o
        secs = (sq["end"] - sq["start"]) / p["tickrate"]
        wall = max(0.1, sq["grew"] - sq["seen"])
        timing.append({"demo_id": p["demo_id"], "n": sq["n"], "file": f"{sq['name']}.mp4", "seconds": round(secs, 1),
                       "wall": round(wall, 1), "speed": round(secs / wall, 2)})
        if o:
            write_index(p["demo_id"], [o])
            try:
                export_clips(p["demo_id"], [o])
            except Exception:  # noqa: BLE001
                pass


def _log_timing(sess: list[dict], timing: list[dict], t_launch: float) -> None:
    """Write how fast this CS2 session recorded (record_timing.json next to each demo's clips) and keep the totals
    in the job: "speed" is seconds of game recorded per second of waiting, launch and demo loading included."""
    ids = {p["demo_id"] for p in sess}
    rows = [t for t in timing if t["demo_id"] in ids]
    total = time.time() - t_launch
    recorded = sum(t["seconds"] for t in rows)
    summary = {"launched": t_launch, "wall": round(total, 1), "recorded": round(recorded, 1),
               "speed": round(recorded / total, 2) if total > 0 else None,
               "recording_speed": round(recorded / max(0.1, sum(t["wall"] for t in rows)), 2) if rows else None,
               "encoder": (lambda e: _auto_encoder or "x264" if e == "auto" else e)(str(load_config().get("record_encoder", "auto"))),
               "fast_preroll": bool(load_config().get("record_fast_preroll", False))}
    print(f"[record] session: {summary['recorded']} s of game in {summary['wall']} s "
          f"(x{summary['speed']} overall, x{summary['recording_speed']} while recording)", flush=True)
    for p in sess:
        try:
            (p["out_dir"] / "record_timing.json").write_text(
                json.dumps({**summary, "clips": [t for t in rows if t["demo_id"] == p["demo_id"]]}, indent=1), encoding="utf-8")
        except OSError:
            pass
    _set(timing={**summary, "clips": rows})


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
        # piece_frac is the sum over the plays rendering at once
        done = min(float(n), int(st.get("piece_i") or 0) + max(0.0, float(st.get("piece_frac") or 0))) / n
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
            st["detail"] = f"{_job.get('stage', '')} {round(100 * float(_job.get('progress') or 0))}%"
            if st.get("prerendered"):
                st["detail"] += f" · {st['prerendered']} play{'s' if st['prerendered'] != 1 else ''} already rendered"
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
    recover_recordings([demo_id])
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
            have = {v["highlight_id"] for v in videos_index(demo_id).get("videos", []) if is_recorded(v)}
            need = [i for i in ids if i not in have]
            _squad_job["needs_recording"] = bool(need)
            ov, follow = overrides, None
            if need:
                _squad_job["stage"] = "recording"
                start(demo_id, need)
                if cinematic:
                    from . import cinematic as cine
                    ov = _pin_song(overrides)
                    follow = cine.fresh_order(exclude={(ov or {}).get("cine_music", "")})

                    def predict(planned: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
                        by_id = {v["highlight_id"]: v for v in videos_index(demo_id).get("videos", [])}
                        by_id.update({v["highlight_id"]: v for d, v in planned if d == demo_id})
                        return [(demo_id, v) for v in sorted((by_id[i] for i in ids if i in by_id), key=lambda v: v["start_tick"])]
                    pre_render_while_recording(predict, ov, load_team()["intro"], follow=follow)
                wait(lambda: (_job or {}).get("status") in ("preparing", "recording", "encoding"))
                if (_job or {}).get("status") != "done":
                    raise RuntimeError("recording did not finish: " + str((_job or {}).get("error") or (_job or {}).get("status")))
            if cinematic:
                _squad_job.update({"stage": "cinematic", "piece_i": 0, "piece_frac": 0.0, "render_started": time.time()})
                _squad_job["render_from"] = squad_progress(_squad_job)
                _squad_job.update({"status": "done", "stage": "done", "result": _squad_cinematic(demo_id, ids, ov, follow)})
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


def pre_render_while_recording(predict, overrides: dict | None, intro: bool, reorder: bool | None = None,
                               follow: list[str] | None = None) -> threading.Thread | None:
    """While CS2 records a montage's missing clips, render the montage's pieces that can already be made (clips
    recorded earlier, and each new clip once it is muxed) into the piece cache, below normal priority so the game
    keeps the CPU it needs. The montage itself runs afterwards as always and finds those pieces made.
    predict(planned) -> the montage's plays [(demo_id, video entry)] as they will be once recorded, planned being
    the recording job's [(demo_id, planned entry)]; overrides and intro exactly as the montage will be made with
    (the same song pinned in cine_music, the same follow-up songs, the same reorder), so the plan and every piece
    come out the same."""
    from . import cinematic as cine
    cfg = load_config()
    if not cfg.get("render_while_recording", True) or cine.options(cfg, overrides).get("cine_voice"):
        return None

    def recording() -> bool:
        return (_job or {}).get("status") in ("preparing", "recording", "encoding") and (_squad_job or {}).get("status") == "running"

    def run() -> None:
        try:
            while recording() and not (_job or {}).get("planned"):
                time.sleep(1)
            if not recording():
                return
            plays = predict(_job["planned"])
            if not plays:
                return
            per_play = plan_montage(plays, overrides, intro, reorder=reorder, follow=follow)["per_play"]
            ffprobe = FFMPEG_DIR / "bin" / "ffprobe.exe"
            nxt, lock = [0], threading.Lock()

            def worker() -> None:
                while True:
                    with lock:
                        i = nxt[0]
                        nxt[0] += 1
                    if i >= len(plays):
                        return
                    d, v = plays[i]
                    cur = None
                    while recording():
                        cur = next((x for x in videos_index(d).get("videos", []) if x.get("file") == v["file"]
                                    and x.get("highlight_id") == v["highlight_id"]), None)
                        if cur and (VIDEOS_DIR / d / cur["file"]).exists():
                            break
                        cur = None
                        time.sleep(1)
                    if cur is None:
                        return   # the recording is over: the montage makes the rest itself
                    pp = per_play[i]
                    cine.cinematic_for_video(d, cur, FFMPEG_EXE, ffprobe, VIDEOS_DIR, overrides=overrides, montage_piece=True,
                                             slow=pp.get("slow", True), lead=pp.get("lead"), low_priority=True,
                                             trim=pp.get("trim", 0.0), t_in=pp.get("t_in"), t_out=pp.get("t_out"),
                                             swoop=bool(pp.get("swoop")))
                    with lock:
                        _squad_job["prerendered"] = int(_squad_job.get("prerendered") or 0) + 1
            n = max(1, min(2, int(cfg.get("render_parallel", 3) or 1)))
            ts = [threading.Thread(target=worker, daemon=True, name=f"prerender{k}") for k in range(n)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        except Exception:  # noqa: BLE001
            traceback.print_exc()   # only a head start is lost: the montage renders whatever is missing

    t = threading.Thread(target=run, daemon=True, name="prerender")
    t.start()
    return t


def _pin_song(overrides: dict | None) -> dict | None:
    """overrides with the montage's song chosen now (cine_music), so planning it twice gives the same plan."""
    from . import cinematic as cine
    song = cine.pick_music(cine.options(load_config(), overrides))
    return {**(overrides or {}), "cine_music": song.name} if song else overrides


def _lineup_entry(cine, demo_id: str, v: dict) -> tuple[dict | None, int]:
    """(player card, kills) of one montage play for the end card's line-up; (None, 0) when it cannot be told."""
    try:
        h = (v.get("ctx") or {}).get("highlight") or {"player": v.get("player_steamid"), "player_name": v.get("player")}
        return cine.player_card(h), len(cine.video_kills(demo_id, v))
    except (KeyError, OSError, ValueError, TypeError):
        return None, 0


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


def _music_bed(cine, ffprobe: Path, first: Path, offset: float, total: float, work_dir: Path, stamp: str,
               follow: list[str] | None = None):
    """(one track for the whole montage, the names of the songs after `first`): the loud pieces of `first` from
    `offset` (cinematic.song_pieces: no intro, outro or long quiet stretch), then, when the montage is longer, the
    pieces of the next songs, all crossfading. (None, []) when the song plays straight through without a cut.
    follow: the follow-up songs in the order to use them (the montage plan put its drops there); else the songs
    not heard for the longest time."""
    def pieces(f: Path) -> list[tuple[float, float]]:
        c = cine.loudness_curve(FFMPEG_EXE, f)
        return cine.song_pieces(c) if c else [(0.0, cine.probe_video(ffprobe, f)["duration"])]
    songs = [(first, pieces(first))]
    used, n = bed_pieces(songs, offset, total)
    if len(used) == 1 and used[0][2] - used[0][1] >= total:
        return None, []   # one piece covers the montage: the song as it is
    picked: list[Path] = []

    def covered() -> bool:
        got = bed_pieces(songs, offset, total)[0]
        return sum(b - a for _, a, b in got) - 3.0 * (len(got) - 1) >= total + 2.0
    while not covered():
        if follow is not None:
            nxt = next((cine.MUSIC_DIR / x for x in follow[len(picked):] if (cine.MUSIC_DIR / x).is_file()), None)
        else:
            nxt = cine.pick_music({"cine_music": ""}, exclude={first.name, *(p.name for p in picked)})
        if nxt is None or nxt.name in {first.name, *(p.name for p in picked)}:
            break
        if follow is not None:
            cine.note_used(nxt.name)
        picked.append(nxt)
        songs.append((nxt, pieces(nxt)))
    used, n = bed_pieces(songs, offset, total)
    bed = work_dir / f"music_bed_{stamp}.m4a"
    r = subprocess.run(cine.pieces_bed_args(FFMPEG_EXE, used, bed), capture_output=True, text=True)
    if r.returncode != 0 or not bed.exists():
        bed.unlink(missing_ok=True)
        return None, []
    return bed, [p.name for p in picked[:max(0, n - 1)]]


DROP_TOLERANCE = 0.3   # seconds between a slow-motion kill and the drop it lands on


def plan_drop_slowmo(variants: list[list[dict]], start: float, drops: list[float], first_hit_to_drop,
                     tol: float = DROP_TOLERANCE) -> tuple[list[dict], float]:
    """Which plays of a long montage get slow motion so it lands on the song's drops. variants[i]: the ways play i
    can be rendered, [{"slow", "lead", "seconds", "hit"}] with the default (slow, its normal lead) first and a
    normal-speed one among them. The first play is always slow (its kill takes the song's main drop:
    first_hit_to_drop(hit, est_total) gives the offset into the song). Every later play is slow only when one of
    its slow variants puts its kill within `tol` of a drop not used yet; otherwise it runs at normal speed.
    start: where the first play begins (after the intro). Returns (the chosen variant per play, song offset).
    Pure function; unit-tested."""
    if not variants:
        return [], 0.0
    first = variants[0][0]
    hit0 = start + first["hit"]
    normal = [next(v for v in vs if not v["slow"]) for vs in variants]
    est_total = start + first["seconds"] + sum(v["seconds"] for v in normal[1:])
    offset = max(0.0, first_hit_to_drop(hit0, est_total))
    targets = sorted(d - offset for d in drops if d - offset > hit0 + 1.0)   # drops in montage time, after the first
    chosen, used, t = [first], set(), start + first["seconds"]
    for i in range(1, len(variants)):
        best = None
        for v in variants[i]:
            if not v["slow"] or v["hit"] is None:
                continue
            for j, d in enumerate(targets):
                err = abs(t + v["hit"] - d)
                if j not in used and err <= tol and (best is None or err < best[0]):
                    best = (err, j, v)
        if best:
            used.add(best[1])
            chosen.append(best[2])
        else:
            chosen.append(normal[i])
        t += chosen[-1]["seconds"]
    return chosen, offset


def play_variants(cine, demo_id: str, v: dict, opts0: dict, ffprobe: Path) -> list[dict]:
    """The ways a montage play can be rendered, with their timing (clip_timing): slow motion on its kill or not, a
    little more or less footage before its first kill ("lead"), and, when it was recorded with a third-person swoop,
    opening on the swoop ("swoop": True; its footage is skipped in every other version). The default first, a
    normal-speed one among them."""
    kills = cine.video_kills(demo_id, v)
    dur = float(v.get("seconds") or 0) or cine.probe_video(ffprobe, VIDEOS_DIR / demo_id / v["file"])["duration"]
    base_lead = float(opts0.get("cine_trim_lead", 2.0))
    sw = v.get("swoop")
    base = {**opts0, "_skip_before": float(sw["to"])} if sw else opts0
    vs = [{"slow": True, "lead": None, "swoop": False, **cine.clip_timing(kills, dur, base)},
          {"slow": False, "lead": None, "swoop": False, **cine.clip_timing(kills, dur, {**base, "_no_slow": True})}]
    if kills:   # the same play with a little more or less footage before its first kill
        for k in range(3, 17):
            lead = k * 0.25
            if abs(lead - base_lead) > 0.01 and kills[0] - lead >= (float(sw["to"]) if sw else 0.0):
                vs.append({"slow": True, "lead": lead, "swoop": False, **cine.clip_timing(kills, dur, {**base, "_lead": lead})})
    if sw:
        on = {**opts0, "_swoop": (float(sw["from"]), float(sw["to"]))}
        vs += [{"slow": True, "lead": None, "swoop": True, **cine.clip_timing(kills, dur, on)},
               {"slow": False, "lead": None, "swoop": True, **cine.clip_timing(kills, dur, {**on, "_no_slow": True})}]
    if vs[0]["hit"] is None:   # a play without a kill inside the clip cannot carry a drop
        vs = [dict(vs[1])] + vs[1:]
    return vs


def montage_reorders(reorder: bool | None) -> bool:
    """Whether a montage puts its plays in the order that fits the song best (config montage_order = "song")."""
    return str(load_config().get("montage_order", "song")) == "song" if reorder is None else bool(reorder)


def plan_montage(plays: list[tuple[str, dict]], overrides: dict | None, intro: bool, reorder: bool | None = None,
                 follow: list[str] | None = None) -> dict:
    """_plan_montage, plus the transition between every two plays in their order ("t_out" of the one before, "t_in"
    of the one after; config montage_transition: "mixed" rotates whip, zoom and flash, "none" keeps hard cuts)."""
    from . import cinematic as cine
    plan = _plan_montage(plays, overrides, intro, reorder, follow)
    cfg = load_config()
    style = str(cfg.get("montage_transition", "mixed"))
    if style != "none":
        order = plan["order"]
        for j in range(len(order) - 1):
            st = cine.TRANSITIONS[j % len(cine.TRANSITIONS)] if style == "mixed" else style
            plan["per_play"][order[j]]["t_out"] = st
            plan["per_play"][order[j + 1]]["t_in"] = st
        if order and cfg.get("montage_outro", True):
            plan["per_play"][order[-1]]["t_out"] = "flash"   # into the end card on a flash, not a fade to black
    return plan


def _plan_montage(plays: list[tuple[str, dict]], overrides: dict | None, intro: bool, reorder: bool | None = None,
                  follow: list[str] | None = None) -> dict:
    """How every play of a montage is rendered and in which order: {"song": the song a drop plan was made on (None
    without one), "offset": where that song starts, "follow": the songs after it, "per_play": [{"slow", "lead"}] by
    play index, "order": the play indices in montage order, "note": what the plan did}. A long montage gets slow
    motion only on the plays whose kill lands on a drop of its soundtrack; with reorder (montage_reorders) the plays
    are also put in the order that lands the most kills, the biggest plays on the biggest drops (plan_song_order),
    else they keep the given order. Uses the clips' recorded length ("seconds"), not the files, so the same plan can
    be made before the clips are recorded (pre_render_while_recording). Picks a song when none is set and the
    follow-up songs when `follow` is None (pass cine_music and follow for the same plan twice)."""
    from . import cinematic as cine
    ffprobe = FFMPEG_DIR / "bin" / "ffprobe.exe"
    plan = {"song": None, "offset": None, "follow": follow, "per_play": [{} for _ in plays],
            "order": list(range(len(plays))), "note": None}
    cfg0 = load_config()
    opts0 = cine.options(cfg0, overrides)
    if not (cfg0.get("cine_drop_slowmo", True) and len(plays) >= int(cfg0.get("cine_drop_min_plays", 6))
            and opts0.get("cine_music_sync") and float(opts0.get("cine_music_volume", 0.7)) > 0 and not opts0.get("cine_voice")):
        return plan
    song = cine.pick_music(opts0)
    if not song:
        return plan
    variants = [play_variants(cine, demo_id, v, opts0, ffprobe) for demo_id, v in plays]
    curve = cine.loudness_curve(FFMPEG_EXE, song)
    drops = cine.find_drops(curve)
    start = cine.INTRO_SECONDS if intro else 0.0
    est_total = start + sum(next(v for v in vs if not v["slow"])["seconds"] for vs in variants)
    first_hit = lambda hit, total: cine.find_drop(curve, 0.5, hit, total) - hit   # noqa: E731
    if not montage_reorders(reorder):
        picked, offset = plan_drop_slowmo(variants, start, drops, first_hit)
        plan.update({"song": song, "offset": offset, "per_play": [{"slow": c["slow"], "lead": c["lead"]} for c in picked],
                     "note": f"{sum(1 for c in picked if c['slow'])} of {len(plays)} plays in slow motion, on the drops"})
        return plan
    # the whole soundtrack: the song, then the songs that follow it when the montage is longer, with every drop
    if follow is None:
        follow = cine.fresh_order(exclude={song.name})
    # each song only plays its loud pieces (song_pieces: no intro, outro or long quiet stretch); drops in the cut
    # parts do not count
    songs = [(cine.song_pieces(curve), drops, cine.drop_strengths(curve, drops))]
    beat_cuts = bool(load_config().get("montage_beat_cuts", True))
    song_beats = [cine.beat_times(FFMPEG_EXE, song) if beat_cuts else []]
    used_follow, have = [], cine.pieces_length(songs[0][0]) - 30.0   # the song starts in its first half minute or so
    for name in follow:
        if have >= est_total * 1.3:
            break
        f = cine.MUSIC_DIR / name
        if not f.is_file():
            continue
        c = cine.loudness_curve(FFMPEG_EXE, f)
        if not c:
            continue
        d = cine.find_drops(c)
        ps = cine.song_pieces(c)
        songs.append((ps, d, cine.drop_strengths(c, d)))
        song_beats.append(cine.beat_times(FFMPEG_EXE, f) if beat_cuts else [])
        used_follow.append(name)
        have += cine.pieces_length(ps) - 3.0
    hls = [(v.get("ctx") or {}).get("highlight") or next((h for h in db.get_highlights(d) if h["id"] == v.get("highlight_id")), None)
           for d, v in plays]
    sizes = [play_score(v, h) for (_, v), h in zip(plays, hls)]
    top = max(sizes) or 1
    order, picked, offset = plan_song_order(
        variants, [x / top for x in sizes], start, lambda hit: first_hit(hit, est_total),
        lambda off: song_drops_in_montage(songs, off), players=[str(v.get("player") or "") for _, v in plays],
        beats_for=lambda off: [t_ for t_, _ in song_drops_in_montage(
            [(ps, b, [0.0] * len(b)) for (ps, _, _), b in zip(songs, song_beats)], off)])
    t, hits = start, 0
    times = [d for d, _ in song_drops_in_montage(songs, offset)]
    for i in order:
        c = picked[i]
        if c["slow"] and c.get("hit") is not None and any(abs(t + c["hit"] - d) <= DROP_TOLERANCE + 1e-6 for d in times):
            hits += 1
        t += c["seconds"]
    plan.update({"song": song, "offset": offset, "follow": used_follow, "order": order,
                 "per_play": [{"slow": c["slow"], "lead": c["lead"], "trim": c.get("trim", 0.0),
                               "swoop": bool(c.get("swoop"))} for c in picked],
                 "note": f"plays ordered to the song: {hits} of {len(plays)} kills on a drop"})
    return plan


def song_drops_in_montage(songs: list[tuple[list[tuple[float, float]], list[float], list[float]]], offset: float,
                          xfade: float = 3.0) -> list[tuple[float, float]]:
    """(montage time, strength) of every drop a soundtrack plays: songs = [(pieces, drops, strengths)] in song
    seconds (pieces from cinematic.song_pieces), the pieces played one after the other, each crossfading `xfade`
    seconds into the previous, the first song from `offset` (montage time 0). A drop in a part that is cut, before
    the offset, or inside a crossfade (drowned by what plays before) does not count. Pure function; unit-tested."""
    out, t, first = [], 0.0, True
    for k, (pieces, drops, strengths) in enumerate(songs):
        if k == 0:
            pieces = [(max(a, offset), b) for a, b in pieces if b > offset + 1.0]
        for a, b in pieces:
            for d, st in zip(drops, strengths):
                if a <= d <= b and (first or d - a >= xfade):
                    out.append((round(t + d - a, 3), st))
            t += (b - a) - xfade
            first = False
    return sorted(out)


def bed_pieces(songs: list[tuple[object, list[tuple[float, float]]]], offset: float, total: float,
               xfade: float = 3.0) -> tuple[list[tuple[object, float, float]], int]:
    """The pieces (song, start, end) a soundtrack of `total` seconds plays: the first song's pieces from `offset`,
    then the next songs' pieces, as many as it takes (a couple of seconds spare), and how many songs that uses.
    When the songs run out, all of them. Pure function; unit-tested."""
    out, have = [], -xfade
    for k, (song, pieces) in enumerate(songs):
        if k == 0:
            pieces = [(max(a, offset), b) for a, b in pieces if b > offset + 1.0]
        for a, b in pieces:
            out.append((song, a, b))
            have += (b - a) - xfade
            if have >= total + 2.0:
                return out, k + 1
    return out, len(songs)


ORDER_BEAM = 120          # states kept per step by plan_song_order
SWOOP_GAP = 3             # at least this many plays without a third-person swoop between two that have one
SWOOP_GAPS = (3, 4, 5)    # the gap each play asks for, picked at random
SWOOP_BONUS = 0.5         # how much the order search wants a swoop where one is allowed
ORDER_OPENERS = 4         # strongest plays tried as the opener


SNAP_MAX = 0.6     # seconds a play's tail may be cut so it ends on a beat
SNAP_KEEP = 1.0    # seconds always kept after a play's last kill


def snap_to_beat(v: dict, t: float, beats: list[float]) -> dict:
    """The variant v of a play starting at montage time t, its end moved back onto the last beat before it (its tail
    after the last kill cut by at most SNAP_MAX, keeping SNAP_KEEP), so the cut to the next play lands on a beat.
    The kills do not move. Pure function; unit-tested."""
    import bisect
    if not beats:
        return v
    end = t + v["seconds"]
    room = min(SNAP_MAX, float(v.get("tail") if v.get("tail") is not None else 0.0) - SNAP_KEEP)
    if room <= 0:
        return v
    j = bisect.bisect_right(beats, end + 1e-6) - 1
    if j < 0 or beats[j] < end - room:
        return v
    return {**v, "seconds": round(beats[j] - t, 3), "trim": round(end - beats[j], 3)}


def plan_song_order(variants: list[list[dict]], scores: list[float], start: float, opener_offset, drops_for,
                    players: list[str] | None = None, tol: float = DROP_TOLERANCE,
                    beats_for=None) -> tuple[list[int], list[dict], float]:
    """The order of a montage's plays and how each one is rendered so its slow-motion kills land on the song's
    drops, the biggest plays on the biggest drops. variants[i]: the ways play i can be rendered (as for
    plan_drop_slowmo, a normal-speed one among them); scores[i]: how big play i is, 0..1. start: where the first
    play begins (after the intro). opener_offset(hit) -> song offset that puts the main drop on the opener's kill at
    montage time `hit`; drops_for(offset) -> [(montage time, strength 0..1)] of every drop of the soundtrack.
    The opener is one of the strongest plays (slow, on the main drop); the rest is a beam search over orders: a
    kill within `tol` of an unused drop scores (0.5 + strength) * (0.6 + score), a bit less the further off; the
    last play scores its own size (the finale); the same player twice in a row costs a little. beats_for(offset) ->
    the soundtrack's beats in montage time: every play then ends on a beat (snap_to_beat), the cuts on the music.
    Returns (order of the play indices, the chosen variant per play index, song offset). Pure function; unit-tested."""
    import bisect
    n = len(variants)
    if n == 0:
        return [], [], 0.0
    normal = [next(v for v in vs if not v["slow"] and not v.get("swoop")) for vs in variants]
    swoop_normal = [next((v for v in vs if not v["slow"] and v.get("swoop")), None) for vs in variants]
    slow = [[v for v in vs if v["slow"] and v.get("hit") is not None] for vs in variants]
    # a little randomness in which plays get the third-person swoop, the same for the same plays every time
    import random as _random
    rnd = _random.Random(len(variants) * 7919 + round(sum(vs[0]["seconds"] for vs in variants) * 10))
    swoop_bonus = [SWOOP_BONUS * (0.6 + 0.8 * rnd.random()) for _ in variants]
    # each play wants its own gap since the last swoop, 3 to 5 plays: "one has it, then 3-5 don't", never a pattern
    swoop_gap = [rnd.choice(SWOOP_GAPS) for _ in variants]
    players = players or [""] * n
    openers = sorted((i for i in range(n) if variants[i][0]["slow"] and variants[i][0].get("hit") is not None),
                     key=lambda i: -scores[i])[:ORDER_OPENERS] or [0]
    best = None
    for o in openers:
        first = variants[o][0]
        hit0 = start + (first.get("hit") or 0.0)
        offset = max(0.0, opener_offset(hit0))
        drops = drops_for(offset)
        times = [d for d, _ in drops]
        beats = sorted(beats_for(offset)) if beats_for else []
        first = snap_to_beat(first, start, beats)
        # the opener's kill takes the main drop: the drop nearest to it is used
        j0 = min(range(len(times)), key=lambda j: abs(times[j] - hit0)) if times else None
        used0 = (1 << j0) if j0 is not None and abs(times[j0] - hit0) <= 1.0 else 0
        gain0 = (0.5 + (drops[j0][1] if used0 else 0.0)) * (0.6 + scores[o])
        # state: (gain, end time, plays used bitmask, drops used bitmask, sequence of (play, variant))
        beam = [(gain0, start + first["seconds"], 1 << o, used0, ((o, first),))]

        def since_swoop(seq) -> int:
            for back, (_, v_) in enumerate(reversed(seq)):
                if v_.get("swoop"):
                    return back
            return SWOOP_GAP + 1
        for step in range(n - 1):
            last_step = step == n - 2
            nxt: dict = {}
            for gain, t, pm, dm, seq in beam:
                prev_player = players[seq[-1][0]]
                for i in range(n):
                    if pm >> i & 1:
                        continue
                    pen = 0.05 if players[i] and players[i] == prev_player else 0.0
                    fin = 0.6 * scores[i] if last_step else 0.0
                    may_swoop = since_swoop(seq) >= swoop_gap[i]  # 3-5 plays without one in between
                    cands = [(gain - pen + fin, dm, snap_to_beat(normal[i], t, beats))]
                    if may_swoop and swoop_normal[i]:
                        cands.append((gain - pen + fin + swoop_bonus[i], dm, snap_to_beat(swoop_normal[i], t, beats)))
                    for v in slow[i]:
                        if v.get("swoop") and not may_swoop:
                            continue
                        k = t + v["hit"]
                        lo = bisect.bisect_left(times, k - tol)
                        for j in range(lo, len(times)):
                            if times[j] > k + tol:
                                break
                            if dm >> j & 1:
                                continue
                            w = (0.5 + drops[j][1]) * (0.6 + scores[i]) * (1 - 0.5 * abs(times[j] - k) / tol)
                            cands.append((gain - pen + fin + w + (swoop_bonus[i] if v.get("swoop") else 0.0), dm | (1 << j),
                                          snap_to_beat(v, t, beats)))
                    for g, dm2, v in cands:
                        key = (pm | (1 << i), dm2, round((t + v["seconds"]) * 4),
                               0 if v.get("swoop") else min(SWOOP_GAP, since_swoop(seq) + 1))
                        if key not in nxt or nxt[key][0] < g:
                            nxt[key] = (g, t + v["seconds"], pm | (1 << i), dm2, seq + ((i, v),))
            beam = sorted(nxt.values(), key=lambda s: -s[0])[:ORDER_BEAM]
        top = beam[0]
        if best is None or top[0] > best[0][0]:
            best = (top, offset)
    (gain, _, _, _, seq), offset = best
    chosen = [None] * n
    for i, v in seq:
        chosen[i] = v
    return [i for i, _ in seq], chosen, offset


def _render_song_montage(plays: list[tuple[str, dict]], overrides: dict | None, work_dir: Path, out: Path,
                         base: int = 0, last: bool = True, intro: dict | None = None, reorder: bool | None = None,
                         follow: list[str] | None = None) -> dict:
    """Every play (demo_id, its videos.json entry) rendered cinematic without music, joined in the given order,
    then ONE song over the whole video (its drop on the first slow-motion kill, dipping under the voice lines).
    Progress goes to _squad_job; base / last: this is one part (plays base.. of the job's total) of a recap.
    intro: {"team", "sub", "stats"} puts the intro card first (the song already plays under it).
    reorder / follow: see plan_montage (the plays may be joined in another order than given).
    Returns {"seconds", "music", "songs", "order": the play indices in the order they were joined}. The clips' own
    cinematic versions are left alone."""
    from . import cinematic as cine
    ffprobe = FFMPEG_DIR / "bin" / "ffprobe.exe"
    pieces: list[Path] = []
    cached: set[Path] = set()     # pieces that live in the piece cache: never deleted here
    infos: list[dict] = []
    stamp = time.strftime("%Y%m%d_%H%M%S")
    joined = work_dir / f"montage_joined_{stamp}.mp4"
    made_files: list[Path] = []   # every piece rendered so far, whatever the order, so a failure still cleans up
    try:
        _squad_job["piece"] = "planning the montage"
        mp = plan_montage(plays, overrides, intro is not None, reorder=reorder, follow=follow)
        plan_song, plan_offset, per_play, order = mp["song"], mp["offset"], mp["per_play"], mp["order"]
        if mp["note"]:
            _squad_job["drops"] = mp["note"]
        # several plays render at once: one FFmpeg filter chain keeps only a core or two busy
        workers = max(1, min(int(load_config().get("render_parallel", 3) or 1), len(plays)))
        lock = threading.Lock()
        fracs: dict[int, float] = {}
        done = [0]
        n = int(_squad_job.get("total") or len(plays))

        def publish(i: int, stage: str, frac: float | None) -> None:
            if workers == 1:
                _squad_job["piece"] = f"play {base + i + 1}/{n} · {stage}" + (f" {round(100 * frac)}%" if frac is not None else "")
            else:
                busy = len(fracs)
                _squad_job["piece"] = f"{done[0]} of {len(plays)} plays done" + (f" · {busy} rendering at once" if busy else "")
            _squad_job["piece_i"] = base + done[0]
            _squad_job["piece_frac"] = sum(fracs.values())

        def render(i: int) -> dict:
            if _squad_job.get("status") != "running":
                raise RuntimeError("cancelled")
            demo_id, v = plays[i]

            def prog(stage: str, frac: float | None) -> None:
                with lock:
                    fracs[i] = frac if frac is not None else fracs.get(i, 0.0)
                    publish(i, stage, frac)
            pp = per_play[i]
            nv = cine.cinematic_for_video(demo_id, v, FFMPEG_EXE, ffprobe, VIDEOS_DIR, overrides=overrides, progress=prog,
                                          montage_piece=True, slow=pp.get("slow", True), lead=pp.get("lead"),
                                          trim=pp.get("trim", 0.0), t_in=pp.get("t_in"), t_out=pp.get("t_out"),
                                          swoop=bool(pp.get("swoop")))
            with lock:
                if not nv.get("cine_path"):   # a cached piece stays in the cache
                    made_files.append(VIDEOS_DIR / demo_id / nv["cine"])
                fracs.pop(i, None)
                done[0] += 1
                publish(i, "done", None)
            return nv

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="piece") as ex:
            futs = [ex.submit(render, i) for i in range(len(plays))]
            try:
                results = [f.result() for f in futs]   # in play order
            except BaseException:
                for f in futs:
                    f.cancel()
                raise
        for i in order:   # joined in the planned order
            (demo_id, _), nv = plays[i], results[i]
            pieces.append(Path(nv["cine_path"]) if nv.get("cine_path") else VIDEOS_DIR / demo_id / nv["cine"])
            infos.append(nv["cine_info"])
        cached = {Path(nv["cine_path"]) for nv in results if nv.get("cine_path")}
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
        if last and pieces and load_config().get("montage_outro", True):
            # the bookend: GG, the team and the stats of the plays over the last frame (the song fades out under it)
            _squad_job["piece"] = "making the end card"
            team = (intro or {}).get("team") or load_team()["team"] or "Squad"
            card = work_dir / f"montage_outro_{stamp}.mp4"
            # behind the card: the last play's slow-motion kill, else a moment before its end (never a faded frame)
            hits = infos[-1].get("hits") or []
            at = float(hits[-1]) if hits else max(0.0, float(infos[-1].get("seconds") or 1.0) - 1.5)
            squad = cine.lineup_players([_lineup_entry(cine, d_, v) for d_, v in plays]) \
                if load_config().get("montage_lineup", True) else []
            secs = cine.make_outro(FFMPEG_EXE, ffprobe, pieces[-1], at, card, team, (intro or {}).get("stats", ""),
                                   int(load_config().get("record_crf", 20)), players=squad)
            if secs:
                pieces.append(card)
                infos.append({"seconds": secs})
            else:
                card.unlink(missing_ok=True)
        if last:
            _squad_job.update({"stage": "joining", "piece": "joining the plays"})
        total, hit, duck = montage_timeline(infos)
        _join(pieces, infos, joined, work_dir / f"montage_list_{stamp}.txt")
        opts = cine.options(load_config(), overrides)
        music = plan_song or cine.pick_music(opts)   # drop-planned montages keep the song they were planned on
        song, songs = None, []
        if music and float(opts.get("cine_music_volume", 0.7)) > 0:
            if last:
                _squad_job["piece"] = "laying the song"
            if plan_offset is not None:
                offset = plan_offset
            else:
                offset = cine.music_offset(FFMPEG_EXE, music, hit, total) if (opts.get("cine_music_sync") and hit is not None) else 0.0
            # longer than the song: the songs not heard for the longest time follow it, crossfading, instead of a loop
            bed, songs = _music_bed(cine, ffprobe, music, offset, total, work_dir, stamp,
                                    follow=mp["follow"] if plan_song is not None else None)
            src, off = (bed, 0.0) if bed else (music, offset)
            try:
                r = subprocess.run(cine.montage_music_args(FFMPEG_EXE, joined, src, out, total, off,
                                                           float(opts["cine_music_volume"]), duck), capture_output=True, text=True)
            finally:
                if bed:
                    bed.unlink(missing_ok=True)
            if r.returncode != 0 or not out.exists():
                raise RuntimeError("adding the song failed: " + r.stderr[-400:])
            song = music.name
        else:
            joined.replace(out)
        return {"seconds": round(total, 1), "music": song, "songs": [song] + songs if song else [], "order": order}
    finally:
        for p_ in {*pieces, *made_files} - cached:
            p_.unlink(missing_ok=True)
        joined.unlink(missing_ok=True)
        try:
            cine.prune_piece_cache(int(float(load_config().get("render_cache_gb", 20)) * 1024 ** 3))
        except Exception:  # noqa: BLE001
            pass


def _squad_cinematic(demo_id: str, ids: list[int], overrides: dict | None, follow: list[str] | None = None) -> dict:
    """The cinematic montage of plays of one match, saved as one of its long videos (in the order that fits the
    song, see plan_montage; follow: the follow-up songs fixed in advance)."""
    vids = sorted((v for v in videos_index(demo_id).get("videos", []) if v["highlight_id"] in set(ids)),
                  key=lambda v: v["start_tick"])
    if not vids:
        raise RuntimeError("none of the highlights has a recorded clip")
    out_dir = VIDEOS_DIR / demo_id
    out = out_dir / long_video_name(len(vids), True)
    plays = [(demo_id, v) for v in vids]
    made = _render_song_montage(plays, overrides, out_dir, out,
                                intro=squad_intro("Squad montage" if _squad_job.get("label") != "Montage" else "Montage",
                                                  plays, {demo_id: (db.get_demo(demo_id) or {}).get("date")}),
                                follow=follow)
    vids = [vids[i] for i in made["order"]]
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
            made = _render_song_montage(plays, overrides, MONTAGE_DIR, out, intro=squad_intro("Montage", plays, dates),
                                        reorder=False)   # the order picked in the tray
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
    recover_recordings()
    members = load_squad()
    matches = _recap_matches()
    plays = recap_plays(matches, members, date_from, date_to, min_kills, best_of)
    have = {(m["demo_id"], v["highlight_id"]) for m in matches for v in m["videos"] if is_recorded(v)}
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
                     "clips": [{"demo_id": d, "file": v["file"], "description": v.get("description")} for d, v in made["plays"]],
                     "created": time.time()}
            _save_montage(out, entry)
            _squad_job.update({"status": "done", "stage": "done", "result": {**entry, "demo_id": MONTAGE_ID}})
        except Exception as e:  # noqa: BLE001
            if _squad_job.get("status") == "running":
                traceback.print_exc()
                _squad_job.update({"status": "error", "stage": "error", "error": str(e)})

    threading.Thread(target=run, daemon=True, name="recap").start()
    return dict(_squad_job)


def _render_match_groups(groups: list[list[tuple[str, dict]]], out: Path, stamp: str, intro: dict | None,
                         music: str = "", follow: list[str] | None = None, reorder: bool | None = None) -> dict:
    """Several matches in one video with ONE soundtrack over the whole of it: every play rendered cinematic, the
    matches oldest first, the intro before the first play, then the chosen song (`music`, or a song not heard
    lately) from its drop on the first slow-motion kill; when the video is longer than the song, the next
    fresh songs follow it with a crossfade. With reorder (montage_reorders) the plays are not in match order but
    in the order that fits the songs (plan_montage). Returns {"seconds", "music": [the songs, in order],
    "plays": [(demo_id, video) in the order they were joined]}."""
    from . import cinematic as cine
    MONTAGE_DIR.mkdir(parents=True, exist_ok=True)
    plays = [x for g in groups for x in g]
    made = _render_song_montage(plays, cine.options(overrides={"cine_music": music or ""}), MONTAGE_DIR, out, intro=intro,
                                follow=follow, reorder=reorder)
    return {"seconds": made["seconds"], "music": made.get("songs") or ([made["music"]] if made.get("music") else []),
            "plays": [plays[i] for i in made["order"]]}


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
    recover_recordings(demo_ids)
    members = load_squad()
    plan = []
    for d in demo_ids:
        demo = db.get_demo(d)
        if not demo:
            continue
        hs = squad_highlights(db.get_highlights(d), members, min_kills)
        have = {v["highlight_id"] for v in videos_index(d).get("videos", []) if is_recorded(v)}
        plan.append({"demo_id": d, "map": demo.get("map"), "date": demo.get("date"),
                     "score": f"{demo.get('score_a')}–{demo.get('score_b')}", "ids": [h["id"] for h in hs],
                     "need": [h["id"] for h in hs if h["id"] not in have],
                     "plays": [{"id": h["id"], "description": h["description"], "recorded": h["id"] in have} for h in hs]})
    return sorted(plan, key=lambda m: str(m.get("date") or ""))


def start_multi_squad(demo_ids: list[str], min_kills: int = 3, title: str = "", cinematic: bool = True,
                      music: str = "", order: str = "") -> dict:
    """The squad montage of several matches as one video (background job, squad_status() with demo_id "montages"):
    record what is missing match by match in CS2, then every play cinematic with one soundtrack over the whole video
    (cinematic=False: the recorded clips as they are, no song), the intro first, the matches joined oldest first,
    saved with the montages. order: "song" (the plays in the order that fits the soundtrack, plan_montage),
    "date" (match order) or "" (config montage_order)."""
    global _squad_job
    if _squad_job and _squad_job.get("status") == "running":
        raise RuntimeError("a montage is already being made")
    if not FFMPEG_EXE.exists():
        raise RuntimeError("FFmpeg is not installed (Install HLAE + FFmpeg)")
    reorder = montage_reorders({"song": True, "date": False}.get(order))
    plan = [m for m in multi_squad_plan(demo_ids, min_kills) if m["ids"]]
    if not plan:
        raise RuntimeError(f"no {min_kills}K+ plays of your squad in these matches")
    to_record = [m for m in plan if m["need"]]
    total = sum(len(m["ids"]) for m in plan)
    _squad_job = {"demo_id": MONTAGE_ID, "status": "running", "stage": "starting", "total": total, "error": None,
                  "result": None, "started": time.time(), "cinematic": cinematic, "label": "Squad montage", "picked": True,
                  "needs_recording": bool(to_record), "rec_i": 0, "rec_n": 1, "matches": len(plan)}

    def run() -> None:
        try:
            song, follow = music, None
            if to_record:   # every missing play of every match in one CS2 session (it loads demo after demo)
                _squad_job.update({"stage": "recording", "rec_i": 0, "rec_n": 1})
                start_session([(m["demo_id"], m["need"]) for m in to_record])
                if cinematic:
                    from . import cinematic as cine
                    song = song or (_pin_song(None) or {}).get("cine_music", "")
                    follow = cine.fresh_order(exclude={song})

                    def predict(planned: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
                        out = []
                        for m in plan:
                            by_id = {v["highlight_id"]: v for v in videos_index(m["demo_id"]).get("videos", [])}
                            by_id.update({v["highlight_id"]: v for d, v in planned if d == m["demo_id"]})
                            vids = sorted((by_id[i] for i in m["ids"] if i in by_id), key=lambda v: v["start_tick"])
                            out += [(m["demo_id"], v) for v in vids]
                        return out
                    pre_render_while_recording(predict, cine.options(overrides={"cine_music": song or ""}), load_team()["intro"],
                                               reorder=reorder, follow=follow)
                while (_job or {}).get("status") in ("preparing", "recording", "encoding"):
                    if _squad_job.get("status") != "running":
                        raise RuntimeError("cancelled")
                    time.sleep(2)
                if (_job or {}).get("status") != "done":
                    raise RuntimeError("recording did not finish: " + str((_job or {}).get("error") or (_job or {}).get("status")))
            have_now = {(m["demo_id"], v["highlight_id"]) for m in plan for v in videos_index(m["demo_id"]).get("videos", [])}
            missing = [(m, i) for m in plan for i in m["ids"] if (m["demo_id"], i) not in have_now]
            if missing:
                _squad_job["missing"] = f"{len(missing)} play{'s' if len(missing) > 1 else ''} could not be recorded and {'were' if len(missing) > 1 else 'was'} left out"
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
            if cinematic:
                made = _render_match_groups(groups, out, stamp, intro, song, follow=follow, reorder=reorder)
                plays = made["plays"]   # the order they were joined in
            else:
                made = _join_as_recorded(plays, out, stamp, intro)
            maps = sorted({m.get("map") or "" for m in plan if any(g[0][0] == m["demo_id"] for g in groups)} - {""})
            when = intro_dates([dates.get(g[0][0]) or "" for g in groups])
            entry = {"file": out.name, "seconds": made["seconds"], "maps": maps, "cinematic": cinematic, "music": made["music"],
                     "description": title.strip()[:80] or f"Squad montage · {len(plays)} plays from {len(groups)} match{'es' if len(groups) > 1 else ''} · {when}",
                     "clips": [{"demo_id": d, "file": v["file"], "description": v.get("description")} for d, v in plays],
                     "created": time.time()}
            if missing:
                entry["warning"] = _squad_job["missing"] + ": " + ", ".join(
                    f"{m.get('map') or 'match'} {str(m.get('date') or '')[:10]}" for m in {id(m): m for m, _ in missing}.values())
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
