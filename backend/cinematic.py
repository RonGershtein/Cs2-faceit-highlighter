"""Cinematic post-processing for recorded clips: slow-motion around kills + background music.

Works on the MP4s produced by record.py; nothing is re-recorded. Pure FFmpeg filter graph:
  video: trim segments, slow segments get setpts (and optional motion interpolation), concat
  audio: game audio keeps time with atempo in slow segments, then music is mixed underneath
"""
from __future__ import annotations

import json
import random
import subprocess
import time
from pathlib import Path

from . import db
from .config import DATA_DIR, load as load_config

MUSIC_DIR = DATA_DIR / "music"
MUSIC_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac", ".opus"}


def music_files() -> list[Path]:
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(p for p in MUSIC_DIR.iterdir() if p.suffix.lower() in MUSIC_EXT and p.is_file())


def probe_duration(ffprobe: Path, path: Path) -> float:
    r = subprocess.run([str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def slow_segments(kill_times: list[float], duration: float, pre: float, post: float) -> list[tuple[float, float, bool]]:
    """Return (start, end, is_slow) segments covering [0, duration]; slow windows around kills are merged."""
    windows = []
    for t in sorted(kill_times):
        a, b = max(0.0, t - pre), min(duration, t + post)
        if a >= b:
            continue
        if windows and a <= windows[-1][1] + 0.15:
            windows[-1][1] = max(windows[-1][1], b)
        else:
            windows.append([a, b])
    segs: list[tuple[float, float, bool]] = []
    cur = 0.0
    for a, b in windows:
        if a - cur > 0.05:
            segs.append((cur, a, False))
        segs.append((a, b, True))
        cur = b
    if duration - cur > 0.05:
        segs.append((cur, duration, False))
    return [s for s in segs if s[1] - s[0] > 0.02]


def _atempo_chain(factor: float) -> str:
    """atempo accepts 0.5..100 per instance; chain for slower factors."""
    parts = []
    f = factor
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.4f}")
    return ",".join(parts)


def build_filter(segs: list[tuple[float, float, bool]], slow: float, smooth: bool, fps: int, has_music: bool,
                 game_vol: float, music_vol: float, total_out: float) -> str:
    parts = []
    labels = []
    for i, (a, b, is_slow) in enumerate(segs):
        v = f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS"
        au = f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS"
        if is_slow:
            v += f",setpts=PTS/{slow:.4f}"
            if smooth:
                v += f",minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
            au += "," + _atempo_chain(slow)
        parts.append(v + f"[v{i}]")
        parts.append(au + f"[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    parts.append("".join(labels) + f"concat=n={len(segs)}:v=1:a=1[vc][ac]")
    if has_music:
        fade_out = max(0.0, total_out - 2.0)
        parts.append(f"[ac]volume={game_vol:.2f}[ag]")
        parts.append(f"[1:a]atrim=start=0:end={total_out:.3f},asetpts=PTS-STARTPTS,volume={music_vol:.2f},"
                     f"afade=t=in:st=0:d=1.5,afade=t=out:st={fade_out:.3f}:d=2[am]")
        parts.append("[ag][am]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
    else:
        parts.append("[ac]anull[aout]")
    return ";".join(parts)


def make_cinematic(ffmpeg: Path, ffprobe: Path, src: Path, dst: Path, kill_times: list[float], music: Path | None,
                   slow: float = 0.35, pre: float = 0.7, post: float = 0.5, smooth: bool = True, fps: int = 60,
                   game_vol: float = 0.35, music_vol: float = 0.7, crf: int = 20) -> dict:
    duration = probe_duration(ffprobe, src)
    segs = slow_segments(kill_times, duration, pre, post)
    if not segs:
        segs = [(0.0, duration, False)]
    total_out = sum((b - a) / (slow if s else 1.0) for a, b, s in segs)
    has_music = music is not None and music.exists()
    args = [str(ffmpeg), "-y", "-i", str(src)]
    if has_music:
        args += ["-stream_loop", "-1", "-i", str(music)]
    graph = build_filter(segs, slow, smooth, fps, has_music, game_vol, music_vol, total_out)
    args += ["-filter_complex", graph, "-map", "[vc]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", str(crf), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
             "-t", f"{total_out + 0.5:.3f}", str(dst)]
    t0 = time.time()
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError("ffmpeg failed: " + r.stderr[-800:])
    return {"seconds": round(total_out, 1), "slow_segments": sum(1 for s in segs if s[2]), "music": music.name if has_music else None,
            "encode_time": round(time.time() - t0, 1)}


def kill_times_for(demo_id: str, highlight_id: int, clip_start_tick: int) -> list[float]:
    meta = json.loads((DATA_DIR / "parsed" / demo_id / "meta.json").read_text(encoding="utf-8"))
    tr = int(meta.get("tickrate", 64))
    for h in db.get_highlights(demo_id):
        if h["id"] == highlight_id:
            return [(t - clip_start_tick) / tr for t in h.get("kills", []) if t >= clip_start_tick]
    return []


def cinematic_for_video(demo_id: str, video: dict, ffmpeg: Path, ffprobe: Path, videos_dir: Path,
                        smooth: bool | None = None) -> dict:
    """Create <name>_cine.mp4 next to the clip and return the updated video entry."""
    cfg = load_config()
    src = videos_dir / demo_id / video["file"]
    dst = src.with_name(src.stem + "_cine.mp4")
    kills = kill_times_for(demo_id, video["highlight_id"], int(video["start_tick"]))
    if str(cfg.get("cine_kills", "last")).lower() != "all" and kills:
        kills = kills[-1:]
    tracks = music_files()
    music = random.choice(tracks) if tracks else None
    info = make_cinematic(ffmpeg, ffprobe, src, dst, kills, music,
                          slow=float(cfg.get("cine_slow", 0.35)), smooth=bool(cfg.get("cine_smooth", True)) if smooth is None else smooth,
                          fps=int(cfg.get("record_fps", 60)), game_vol=float(cfg.get("cine_game_volume", 0.35)),
                          music_vol=float(cfg.get("cine_music_volume", 0.7)), crf=int(cfg.get("record_crf", 20)))
    video = dict(video)
    video["cine"] = dst.name
    video["cine_info"] = info
    return video
