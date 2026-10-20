"""Cinematic post-processing for recorded clips.

Works on the MP4s produced by record.py; nothing is re-recorded. One FFmpeg filter graph per clip:
  1. the clip is cut into segments (start, end, speed): dead time between kills can be dropped or fast-forwarded,
     kills get slow-motion windows, optionally with a staircase speed ramp in and out
  2. per segment: zoom punch + flash on kills (zoompan/eq), setpts for the speed, optional motion interpolation
  3. the segments are concatenated; look (vignette + grade), title card, kill counter and fades go on top
  4. audio: game audio keeps time with atempo per segment; a music track is mixed underneath, optionally
     started at its "drop" (biggest sustained jump in loudness) so the drop lands on the slow-motion kill
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import db, voice
from .config import DATA_DIR, ROOT, load as load_config

MUSIC_DIR = DATA_DIR / "music"
MUSIC_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac", ".opus"}
FONT_DIR = ROOT / "assets" / "fonts"   # drop any .ttf/.otf here to use it for the overlays
PLAYERS_DIR = DATA_DIR / "players"     # <steam64>.png|jpg -> photo in the player card; players.json -> nicknames
PLAYERS_JSON = DATA_DIR / "players.json"
PHOTO_EXT = (".png", ".jpg", ".jpeg", ".webp")
ACCENT = "0xf5a623"                    # ring colour of the player card (the app's orange)
CARD_BG = "0x2a2f3a"                   # blank avatar colour

# cine_* keys of config.toml that the UI can change per job (see options())
OPTION_KEYS = ("cine_slow", "cine_kills", "cine_smooth", "cine_game_volume", "cine_music_volume", "cine_music",
               "cine_music_sync", "cine_trim", "cine_trim_lead", "cine_fast", "cine_skip_waits", "cine_ramp",
               "cine_impact", "cine_look", "cine_grade", "cine_clutch", "cine_fade", "cine_title", "cine_counter", "cine_card", "cine_voice",
               "cine_voice_volume", "cine_voice_pick")
TRIM_MODES = ("off", "cut", "fast")   # dead time between kills: keep / drop / fast-forward

Seg = tuple[float, float, float]   # (start, end, speed) in source seconds; speed 1.0 = normal, 0.35 = slow motion

RAMP_SECONDS = 0.36     # length of a speed ramp (each side)
RAMP_STEPS = 3
SLOW_PRE, SLOW_POST = 0.7, 0.5   # slow-motion window around a kill
TRIM_TAIL = 2.5         # seconds kept after a kill when trimming
TRIM_MIN_CUT = 1.5      # dead time shorter than this is not worth cutting
TITLE_SECONDS = 3.2
# skipping long waits (cine_skip_waits): a fast-forward longer than WAIT_MAX_OUT seconds keeps only its edges, each
# a warp that speeds up from cine_fast to WAIT_BURST (seconds of output per step), and its middle is cut
WAIT_MAX_OUT = 2.6
WAIT_BURST = 16.0
WAIT_STEPS_OUT = (0.5, 0.25, 0.35)   # at cine_fast, between the two, at WAIT_BURST
CUT_FLASH_OUT, CUT_FLASH_IN, CUT_AUDIO_FADE = 0.1, 0.22, 0.06


def music_files() -> list[Path]:
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(p for p in MUSIC_DIR.iterdir() if p.suffix.lower() in MUSIC_EXT and p.is_file())


# options that apply to one run only and are never remembered: the casters stay off unless they are ticked for
# that clip (a montage made without opening the settings has no caster lines)
PER_RUN_KEYS = ("cine_voice",)


def options(cfg: dict | None = None, overrides: dict | None = None) -> dict:
    """Effective cine_* options: config.toml values with (type-coerced) overrides on top; PER_RUN_KEYS always
    start from their default."""
    from .config import DEFAULTS
    cfg = cfg if cfg is not None else load_config()
    out = {k: DEFAULTS[k] if k in PER_RUN_KEYS else cfg.get(k, DEFAULTS[k]) for k in OPTION_KEYS}
    for k, v in (overrides or {}).items():
        if k not in OPTION_KEYS or v is None:
            continue
        d = DEFAULTS[k]
        try:
            if isinstance(d, bool):
                out[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            elif isinstance(d, (int, float)):
                out[k] = float(v)
            else:
                out[k] = str(v)
        except (TypeError, ValueError):
            pass
    out["cine_slow"] = min(0.95, max(0.1, float(out["cine_slow"])))
    out["cine_kills"] = "all" if str(out["cine_kills"]).lower() == "all" else "last"
    mode = str(out["cine_trim"]).lower()
    out["cine_trim"] = mode if mode in TRIM_MODES else ("cut" if mode in ("1", "true", "yes", "on") else "off")
    out["cine_trim_lead"] = min(15.0, max(0.5, float(out["cine_trim_lead"])))
    out["cine_fast"] = min(30.0, max(1.5, float(out["cine_fast"])))
    out["cine_grade"] = out["cine_grade"] if out["cine_grade"] in GRADES else "teal_orange"
    return out


# ---- probing ---------------------------------------------------------------------------------

def probe_video(ffprobe: Path, path: Path) -> dict:
    """{duration, width, height, fps} of a clip (0 / 1920x1080 / 60 when ffprobe fails)."""
    out = {"duration": 0.0, "width": 1920, "height": 1080, "fps": 60.0}
    r = subprocess.run([str(ffprobe), "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True)
    try:
        j = json.loads(r.stdout or "{}")
        st = (j.get("streams") or [{}])[0]
        out["duration"] = float(j.get("format", {}).get("duration") or 0)
        out["width"] = int(st.get("width") or out["width"])
        out["height"] = int(st.get("height") or out["height"])
        num, _, den = str(st.get("r_frame_rate") or "60/1").partition("/")
        out["fps"] = float(num) / float(den or 1)
    except (ValueError, json.JSONDecodeError, ZeroDivisionError):
        pass
    return out


def probe_duration(ffprobe: Path, path: Path) -> float:
    r = subprocess.run([str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


# ---- segment planning (pure functions, unit-tested) ---------------------------------------------

def keep_windows(kill_times: list[float], duration: float, lead: float = 4.0, tail: float = TRIM_TAIL,
                 min_cut: float = TRIM_MIN_CUT) -> list[tuple[float, float]]:
    """Parts of the clip worth keeping when trimming: `lead` s before and `tail` s after every kill.
    Dead time between kills (walking, waiting) is dropped when more than `min_cut` seconds would go."""
    if not kill_times or duration <= 0:
        return [(0.0, duration)]
    return union_windows([(max(0.0, t - lead), min(duration, t + tail)) for t in kill_times], min_cut) or [(0.0, duration)]


def union_windows(wins: list[tuple[float, float]], min_cut: float = TRIM_MIN_CUT) -> list[tuple[float, float]]:
    """Sorted windows with overlaps merged; a gap shorter than `min_cut` is not worth cutting and is merged too."""
    out: list[list[float]] = []
    for a, b in sorted(wins):
        if a >= b:
            continue
        if out and a - out[-1][1] < min_cut:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _merge_windows(kill_times: list[float], lo: float, hi: float, pre: float, post: float) -> list[list[float]]:
    wins: list[list[float]] = []
    for t in sorted(kill_times):
        a, b = max(lo, t - pre), min(hi, t + post)
        if a >= b:
            continue
        if wins and a <= wins[-1][1] + 0.15:
            wins[-1][1] = max(wins[-1][1], b)
        else:
            wins.append([a, b])
    return wins


def speed_segments(kill_times: list[float], keep: list[tuple[float, float]], slow: float, pre: float = SLOW_PRE,
                   post: float = SLOW_POST, ramp: bool = False) -> list[Seg]:
    """Cut every kept window into (start, end, speed) segments: normal speed, slow motion around kills and,
    with ramp=True, a short staircase of intermediate speeds on both sides of each slow-motion window."""
    segs: list[Seg] = []
    for lo, hi in keep:
        wins = _merge_windows([t for t in kill_times if lo <= t <= hi], lo, hi, pre, post)
        n = len(wins)
        rin, rout = [], []
        for j, (a, b) in enumerate(wins):
            before = a - (wins[j - 1][1] if j else lo)
            after = (wins[j + 1][0] if j + 1 < n else hi) - b
            rin.append(min(RAMP_SECONDS, before / 2 if j else before) if ramp else 0.0)
            rout.append(min(RAMP_SECONDS, after / 2 if j + 1 < n else after) if ramp else 0.0)
        cur = lo
        for j, (a, b) in enumerate(wins):
            ri, ro = (rin[j] if rin[j] >= 0.1 else 0.0), (rout[j] if rout[j] >= 0.1 else 0.0)
            if a - ri - cur > 0.05:
                segs.append((cur, a - ri, 1.0))
            if ri:
                step = ri / RAMP_STEPS
                segs += [(a - ri + i * step, a - ri + (i + 1) * step, slow ** ((i + 1) / (RAMP_STEPS + 1)))
                         for i in range(RAMP_STEPS)]
            segs.append((a, b, slow))
            if ro:
                step = ro / RAMP_STEPS
                segs += [(b + i * step, b + (i + 1) * step, slow ** ((RAMP_STEPS - i) / (RAMP_STEPS + 1)))
                         for i in range(RAMP_STEPS)]
            cur = b + ro
        if hi - cur > 0.05:
            segs.append((cur, hi, 1.0))
    return [s for s in segs if s[1] - s[0] > 0.02]


def out_time(segs: list[Seg], t: float) -> float:
    """Map a source time to the time in the output (after cuts and speed changes)."""
    acc = 0.0
    for a, b, s in segs:
        if t < a:
            return acc
        if t <= b:
            return acc + (t - a) / s
        acc += (b - a) / s
    return acc


def fill_gaps(segs: list[Seg], duration: float, speed: float) -> list[Seg]:
    """Fast-forward instead of cutting: every gap between segments (and before/after them) becomes a segment at `speed`."""
    out: list[Seg] = []
    cur = 0.0
    for a, b, s in sorted(segs):
        if a - cur > 0.05:
            out.append((cur, a, speed))
        out.append((a, b, s))
        cur = max(cur, b)
    if duration - cur > 0.05:
        out.append((cur, duration, speed))
    return out


def skip_waits(segs: list[Seg], fast: float, burst: float = WAIT_BURST, max_out: float = WAIT_MAX_OUT) -> list[Seg]:
    """Cut the middle out of every fast-forward (a segment at speed `fast`) that would last more than `max_out`
    seconds: its edges stay as a warp, speeding up from `fast` to `burst` towards the cut and slowing back down
    after it (build_filter flashes over the cut). A wait at the start of the clip keeps only the warp into the
    action, one at the end only the warp out of it. Pure function; unit-tested."""
    speeds = (fast, math.sqrt(fast * burst), burst)
    side = sum(o * sp for o, sp in zip(WAIT_STEPS_OUT, speeds))   # source seconds one warp uses

    def warp_out(a: float) -> list[Seg]:   # from `a` on, speeding up
        out, t = [], a
        for o, sp in zip(WAIT_STEPS_OUT, speeds):
            out.append((t, t + o * sp, sp))
            t += o * sp
        return out

    def warp_in(b: float) -> list[Seg]:    # up to `b`, slowing down
        out, t = [], b
        for o, sp in zip(WAIT_STEPS_OUT, speeds):
            out.insert(0, (t - o * sp, t, sp))
            t -= o * sp
        return out
    out: list[Seg] = []
    for i, (a, b, s) in enumerate(segs):
        first, last = i == 0, i == len(segs) - 1
        need = side * (1 if first or last else 2) + 2.0   # leave at least 2 s worth cutting
        if abs(s - fast) > 1e-6 or (b - a) / s <= max_out or b - a < need or (first and last):
            out.append((a, b, s))
        elif first:
            out += warp_in(b)
        elif last:
            out += warp_out(a)
        else:
            out += warp_out(a) + warp_in(b)
    return out


def split_at_out(segs: list[Seg], t: float) -> list[Seg]:
    """The same segments with a split at output time `t` (so an effect can cover exactly the part after or before
    it). Pure function; unit-tested."""
    out, acc = [], 0.0
    for a, b, s in segs:
        length = (b - a) / s
        if acc + 0.02 < t < acc + length - 0.02:
            m = a + (t - acc) * s
            out += [(a, m, s), (m, b, s)]
        else:
            out.append((a, b, s))
        acc += length
    return out


def trim_out_end(segs: list[Seg], cut: float) -> list[Seg]:
    """The segments with the last `cut` output seconds removed (a montage play ends on a beat). Pure function."""
    out = list(segs)
    while cut > 1e-3 and out:
        a, b, s = out[-1]
        length = (b - a) / s
        if length <= cut + 1e-3:
            out.pop()
            cut -= length
        else:
            out[-1] = (a, b - cut * s, s)
            cut = 0.0
    return out


TRANSITIONS = ("whip", "zoom", "flash")
EDGE_SECONDS = 0.4      # each side of a transition between montage plays


def edge_filter(style: str, side: str, width: int, height: int, d: float = EDGE_SECONDS) -> str:
    """Filters for the first (side "in") or last ("out") EDGE_SECONDS of a montage play, in that piece's own time
    (0..d), meeting on the cut (which lands on a beat):
    whip: the picture swings sideways out of the play and into the next, in one direction, smeared by motion blur;
    zoom: a punch in to 1.6x with blur, a white flash on the cut;
    flash: the picture blows out to white and the next play comes back from it.
    Pure function; unit-tested."""
    rise = f"(t/{d:.3f})" if side == "out" else f"(1-t/{d:.3f})"     # 0 -> 1 towards the cut
    ease = f"({rise}*{rise})"
    if style == "whip":
        # 20 % larger, then the crop window slides: the content moves left, faster towards the cut
        shift = f"(iw-ow)/2*(1+{ease})" if side == "out" else f"(iw-ow)/2*(1-{ease})"
        blur = ",".join(f"avgblur=sizeX={b}:sizeY=1:enable='between({rise},{lo},{hi})'"
                        for b, lo, hi in ((12, 0.25, 0.5), (36, 0.5, 0.75), (90, 0.75, 1.01)))
        return f"scale={int(width * 1.2) // 2 * 2}:{int(height * 1.2) // 2 * 2},crop={width}:{height}:x='{shift}':y=(ih-oh)/2,{blur},setsar=1"
    if style == "zoom":
        z = f"(1+0.6*{rise})" if side == "in" else f"(1+0.6*{ease})"   # in: settles over the whole 0.4 s
        blur = ",".join(f"avgblur=sizeX={b}:sizeY={b}:enable='between({rise},{lo},{hi})'" for b, lo, hi in ((4, 0.5, 0.8), (10, 0.8, 1.01)))
        flash = (f"fade=t=out:st={d - 0.06:.3f}:d=0.06:color=white" if side == "out" else "fade=t=in:st=0:d=0.12:color=white")
        return (f"scale=w='trunc(iw*{z}/2)*2':h='trunc(ih*{z}/2)*2':eval=frame,crop={width}:{height},{blur},{flash},setsar=1")
    if style == "flash":
        return (f"eq=brightness='0.35*{ease}':eval=frame,fade=t=out:st={d * 0.55:.3f}:d={d * 0.45:.3f}:color=white"
                if side == "out" else f"fade=t=in:st=0:d={d:.3f}:color=white")
    return "null"


def total_duration(segs: list[Seg]) -> float:
    return sum((b - a) / s for a, b, s in segs)


# ---- music analysis ----------------------------------------------------------------------------

def loudness_curve(ffmpeg: Path, path: Path, win: float = 0.5) -> list[float]:
    """RMS loudness per `win` seconds of a track (mono 8 kHz decode)."""
    rate = 8000
    r = subprocess.run([str(ffmpeg), "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return []
    n = int(rate * win)
    try:
        import numpy as np
        x = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        full = (len(x) // n) * n
        if full == 0:
            return []
        return [float(v) for v in np.sqrt((x[:full].reshape(-1, n) ** 2).mean(axis=1))]
    except ImportError:
        import array
        x = array.array("h")
        x.frombytes(r.stdout[: len(r.stdout) // 2 * 2])
        return [math.sqrt(sum((v / 32768.0) ** 2 for v in x[i:i + n]) / n) for i in range(0, len(x) - n + 1, n)]


def find_drop(curve: list[float], win: float = 0.5, hit_time: float = 0.0, clip_len: float = 0.0,
              after: float = 6.0, before: float = 3.0) -> float:
    """Track time of the "drop": the moment with the biggest rise from the `before` seconds to the `after` seconds
    that follow. Only moments that leave room for the clip are considered (drop >= hit_time, and the song must
    last until the clip ends); falls back to the loudest sustained part."""
    if not curve:
        return 0.0
    na, nb = max(1, int(round(after / win))), max(1, int(round(before / win)))
    track_len = len(curve) * win

    def score(i: int) -> float:
        seg = curve[i:i + na]
        prev = curve[max(0, i - nb):i]
        return (sum(seg) / len(seg)) - (sum(prev) / len(prev) if prev else 0.0)

    cands = [i for i in range(1, len(curve) - 1)
             if i * win >= hit_time and i * win - hit_time + clip_len <= track_len + win]
    if not cands:
        cands = list(range(len(curve)))
    best = max(cands, key=score)
    return best * win


def find_drops(curve: list[float], win: float = 0.5, min_gap: float = 10.0, share: float = 0.25,
               after: float = 6.0, before: float = 3.0) -> list[float]:
    """Track times of the song's drops: the moments where the loudness rises most (the top `share` of all rises,
    same measure as find_drop), at least `min_gap` seconds apart. Pure function; unit-tested."""
    if len(curve) < 3:
        return []
    na, nb = max(1, int(round(after / win))), max(1, int(round(before / win)))

    def score(i: int) -> float:
        seg, prev = curve[i:i + na], curve[max(0, i - nb):i]
        return (sum(seg) / len(seg)) - (sum(prev) / len(prev) if prev else 0.0)
    scores = {i: score(i) for i in range(1, len(curve) - 1)}
    ranked = sorted(scores, key=scores.get, reverse=True)
    cutoff = scores[ranked[max(0, int(len(ranked) * share) - 1)]]
    picked: list[int] = []
    for i in ranked:
        if scores[i] < cutoff or scores[i] <= 0:
            break
        if all(abs(i - j) * win >= min_gap for j in picked):
            picked.append(i)
    return sorted(i * win for i in picked)


# ---- colour grades: 3D LUTs made here (no files to download), applied with FFmpeg's lut3d ----------------------

GRADES = ("classic", "teal_orange", "dark")   # classic = the old eq colour boost
LUT_DIR = DATA_DIR / "luts"
LUT_VERSION = 2


def grade_rgb(r: float, g: float, b: float, style: str) -> tuple[float, float, float]:
    """One colour through a grade (0..1 in, 0..1 out). Pure function; unit-tested.
    teal_orange: a soft S-curve for contrast, shadows pushed to teal and highlights to warm orange (skin, fire and
    muzzle flashes stay warm, walls and sky go cool). dark: crushed blacks, a gentle highlight roll-off, 20 % less
    saturation and cool shadows."""
    def s_curve(x: float, k: float) -> float:
        return x + k * (x * x * (3 - 2 * x) - x)
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    if style == "teal_orange":
        r, g, b = (s_curve(x, 0.6) for x in (r, g, b))
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        sh, hi = (1 - lum) ** 1.5, lum ** 1.5
        r, g, b = r - 0.09 * sh + 0.10 * hi, g + 0.03 * sh + 0.02 * hi, b + 0.08 * sh - 0.10 * hi
        r, g, b = (lum + (x - lum) * 1.15 for x in (r, g, b))
    elif style == "dark":
        r, g, b = (max(0.0, (x - 0.06) / 0.94) ** 1.12 for x in (r, g, b))
        r, g, b = (1 - (1 - s_curve(x, 0.5)) ** 1.15 for x in (r, g, b))
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        r, g, b = (lum + (x - lum) * 0.72 for x in (r, g, b))
        sh = (1 - lum) ** 2
        r, g, b = r - 0.04 * sh, g + 0.005 * sh, b + 0.06 * sh
    return tuple(min(1.0, max(0.0, x)) for x in (r, g, b))


def lut_cube(style: str, size: int = 33) -> str:
    """The .cube text of a grade (red changes fastest, as the format wants). Pure function."""
    lines = [f'TITLE "cs2-watcher {style}"', f"LUT_3D_SIZE {size}", "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
    n = size - 1
    for bi in range(size):
        for gi in range(size):
            for ri in range(size):
                lines.append("%.5f %.5f %.5f" % grade_rgb(ri / n, gi / n, bi / n, style))
    return "\n".join(lines) + "\n"


def lut_file(style: str) -> Path | None:
    """data/luts/<style>_v<n>.cube, written the first time it is needed; None for the classic grade."""
    if style not in GRADES or style == "classic":
        return None
    p = LUT_DIR / f"{style}_v{LUT_VERSION}.cube"
    if not p.is_file():
        LUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(lut_cube(style), encoding="ascii")
        tmp.replace(p)
    return p


def song_span(curve: list[float], win: float = 0.5, smooth: float = 2.0, level: float = 0.45) -> tuple[float, float]:
    """(start, end) in seconds of the loud part of a track: a quiet intro (talking, a slow build) and the outro
    (fade-out, a quiet ending, silence) are left out, so a montage crossfades from loud part to loud part. The loud
    part is where the loudness over `smooth` seconds reaches `level` x the track's typical loud level (80th
    percentile). The whole track when that leaves less than 30 s. Pure function; unit-tested."""
    total = len(curve) * win
    n = max(1, int(round(smooth / win)))
    if len(curve) < 2 * n:
        return 0.0, total
    avg = [sum(curve[i:i + n]) / n for i in range(len(curve) - n + 1)]
    thr = level * sorted(curve)[int(len(curve) * 0.8)]
    loud = [i for i, a in enumerate(avg) if a >= thr]
    if not loud:
        return 0.0, total
    a, b = max(0.0, loud[0] * win - 0.5), min(total, (loud[-1] + n) * win + 0.5)
    return (a, b) if b - a >= 30.0 else (0.0, total)


# ---- beats: where the cuts between montage plays land --------------------------------------------------------

def onset_envelope(ffmpeg: Path, path: Path, sr: int = 11025, hop: int = 256, n_fft: int = 1024) -> tuple[list[float], float]:
    """(how much new sound starts in each 23 ms frame of a track — spectral flux —, frames per second)."""
    import numpy as np
    r = subprocess.run([str(ffmpeg), "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                       capture_output=True)
    if r.returncode != 0 or len(r.stdout) < n_fft * 4:
        return [], sr / hop
    x = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    n = 1 + (len(x) - n_fft) // hop
    frames = np.lib.stride_tricks.as_strided(x, shape=(n, n_fft), strides=(x.strides[0] * hop, x.strides[0]))
    mag = np.log1p(10.0 * np.abs(np.fft.rfft(frames * np.hanning(n_fft), axis=1)))
    flux = np.maximum(0.0, np.diff(mag, axis=0)).sum(axis=1)
    return [0.0] + flux.tolist(), sr / hop


def track_beats(onset: list[float], fps: float, bpm_lo: float = 70.0, bpm_hi: float = 180.0,
                tightness: float = 100.0) -> list[float]:
    """Beat times (seconds) from an onset envelope: the tempo from its autocorrelation (leaning to 120 BPM), then the
    beats by dynamic programming (each beat on a strong onset about one beat period after the previous one, the
    classic Ellis tracker). Pure function; unit-tested."""
    import numpy as np
    o = np.asarray(onset, dtype=np.float64)
    if len(o) < fps * 4 or o.std() == 0:
        return []
    o = (o - o.mean()) / o.std()
    lags = np.arange(max(1, int(fps * 60 / bpm_hi)), int(fps * 60 / bpm_lo) + 1)
    ac = np.array([np.dot(o[:-lag], o[lag:]) / (len(o) - lag) for lag in lags])
    bpm = 60.0 * fps / lags
    period = int(lags[int(np.argmax(ac * np.exp(-0.5 * np.log2(bpm / 120.0) ** 2)))])
    score, back = o.copy(), np.full(len(o), -1)
    for t in range(period // 2, len(o)):
        ps = np.arange(max(0, t - 2 * period), t - period // 2)
        if not len(ps):
            continue
        cand = score[ps] - tightness * np.log((t - ps) / period) ** 2
        k = int(np.argmax(cand))
        score[t] = o[t] + cand[k]
        back[t] = ps[k]
    t = len(o) - period + int(np.argmax(score[-period:]))
    beats = [t]
    while back[t] >= 0:
        t = int(back[t])
        beats.append(t)
    return [round(b / fps, 3) for b in sorted(beats)]


def beat_times(ffmpeg: Path, path: Path) -> list[float]:
    onset, fps = onset_envelope(ffmpeg, path)
    return track_beats(onset, fps) if onset else []


QUIET_MIN = 8.0     # a quiet stretch inside a song at least this long is cut out of a montage's soundtrack
QUIET_KEEP = 1.5    # seconds of it kept on each side of the cut (the crossfade runs there)


def song_pieces(curve: list[float], win: float = 0.5, level: float = 0.45, quiet_min: float = QUIET_MIN,
                keep: float = QUIET_KEEP) -> list[tuple[float, float]]:
    """The parts of a track a montage plays, (start, end) in song seconds: its loud span (song_span) with every
    quiet stretch of `quiet_min` seconds or more inside it cut out (`keep` seconds of it stay on each side, where
    the pieces crossfade), so a soundtrack never sits in an ambient break. Pure function; unit-tested."""
    a, b = song_span(curve, win, level=level)
    if b - a <= 0 or not curve:
        return [(a, b)]
    h = max(1, int(round(1.0 / win)))          # loudness over about two seconds around each point
    thr = level * sorted(curve)[int(len(curve) * 0.8)]
    quiet = [sum(curve[max(0, k - h):k + h + 1]) / len(curve[max(0, k - h):k + h + 1]) < thr for k in range(len(curve))]
    pieces, cur, k = [], a, int(a / win)
    end = min(len(curve), int(b / win))
    while k < end:
        if not quiet[k]:
            k += 1
            continue
        j = k
        while j < end and quiet[j]:
            j += 1
        qa, qb = k * win, j * win
        if qb - qa >= quiet_min and qa + keep - cur >= 4.0 and b - (qb - keep) >= 4.0:
            pieces.append((cur, qa + keep))
            cur = qb - keep
        k = j
    pieces.append((cur, b))
    return pieces


def pieces_length(pieces: list[tuple[float, float]], xfade: float = 3.0) -> float:
    """Seconds the pieces last played one after the other, each crossfading `xfade` into the previous."""
    return sum(b - a for a, b in pieces) - xfade * max(0, len(pieces) - 1)


def pieces_bed_args(ffmpeg: Path, pieces: list[tuple[Path, float, float]], dst: Path, xfade: float = 3.0) -> list[str]:
    """FFmpeg command playing the pieces (file, start, end) one after the other, each crossfading `xfade` seconds
    into the previous: a montage's whole soundtrack, songs and the parts of each song. Pure function; unit-tested."""
    args = [str(ffmpeg), "-y"]
    for f, a, b in pieces:
        args += (["-ss", f"{a:.3f}"] if a > 0 else []) + ["-t", f"{b - a:.3f}", "-i", str(f)]
    if len(pieces) == 1:
        return args + ["-map", "0:a", "-c:a", "aac", "-b:a", "256k", str(dst)]
    chain, prev = [], "[0:a]"
    for i in range(1, len(pieces)):
        chain.append(f"{prev}[{i}:a]acrossfade=d={xfade:g}:c1=tri:c2=tri[x{i}]")
        prev = f"[x{i}]"
    return args + ["-filter_complex", ";".join(chain), "-map", prev, "-c:a", "aac", "-b:a", "256k", str(dst)]


def drop_strengths(curve: list[float], drops: list[float], win: float = 0.5, after: float = 6.0,
                   before: float = 3.0) -> list[float]:
    """How big each drop is (the rise in loudness find_drops measured), the biggest being 1.0. Pure function."""
    if not curve or not drops:
        return []
    na, nb = max(1, int(round(after / win))), max(1, int(round(before / win)))
    rises = []
    for d in drops:
        i = min(len(curve) - 1, max(0, int(round(d / win))))
        seg, prev = curve[i:i + na], curve[max(0, i - nb):i]
        rises.append(max(0.0, (sum(seg) / len(seg)) - (sum(prev) / len(prev) if prev else 0.0)))
    top = max(rises) or 1.0
    return [r / top for r in rises]


def music_offset(ffmpeg: Path, track: Path, hit_time: float, clip_len: float) -> float:
    """Seconds to skip at the start of `track` so that its drop plays at `hit_time` of the clip."""
    drop = find_drop(loudness_curve(ffmpeg, track), 0.5, hit_time, clip_len)
    return max(0.0, drop - hit_time)


# ---- text ---------------------------------------------------------------------------------------

def find_font() -> Path | None:
    """A bold-ish TTF for drawtext: assets/fonts first, then the system fonts."""
    if FONT_DIR.is_dir():
        for p in sorted(FONT_DIR.iterdir()):
            if p.suffix.lower() in (".ttf", ".otf"):
                return p
    windir = Path(os.environ.get("WINDIR", "C:/Windows"))
    cands = [windir / "Fonts" / n for n in ("bahnschrift.ttf", "arialbd.ttf", "arial.ttf", "segoeuib.ttf")]
    cands += [Path("/mnt/c/Windows/Fonts") / n for n in ("bahnschrift.ttf", "arialbd.ttf", "arial.ttf")]
    cands += [Path(p) for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                                "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
                                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                                "/System/Library/Fonts/Supplemental/Arial Bold.ttf")]
    for p in cands:
        if p.is_file():
            return p
    return None


def find_display_font() -> Path | None:
    """A heavy display face for the intro and end cards (Segoe UI Black, Arial Black, Impact), else find_font()."""
    windir = Path(os.environ.get("WINDIR", "C:/Windows"))
    for d in (windir / "Fonts", Path("/mnt/c/Windows/Fonts")):
        for n in ("seguibl.ttf", "ariblk.ttf", "impact.ttf", "segoeuib.ttf"):
            if (d / n).is_file():
                return d / n
    return find_font()


def pretty_map(name: str) -> str:
    n = str(name or "")
    for pre in ("de_", "cs_", "ar_"):
        if n.startswith(pre):
            n = n[len(pre):]
    return n.replace("_", " ").title() or "?"


def title_for(h: dict, map_name: str = "", with_player: bool = True) -> tuple[str, str]:
    """(big label, sub line) of the title card for a highlight. with_player=False leaves the name to the player card."""
    typ = str(h.get("type") or "")
    n = int(h.get("n") or len(h.get("kills") or []) or 0)
    if typ == "ace" or (typ == "multikill" and n >= 5):
        big = "ACE"
    elif typ == "clutch":
        # named by its kills like any play (the 1vN odds are on screen while the clutch is on)
        k = len(h.get("kills") or [])
        big = ({2: "DOUBLE KILL", 3: "TRIPLE KILL", 4: "QUAD KILL"}.get(k, "ACE" if k >= 5 else "")
               or (f"1v{n}" if n else "CLUTCH"))
    elif typ == "ninja_defuse":
        big = "NINJA DEFUSE"
    elif typ == "knife":
        big = "KNIFE KILL"
    elif n >= 2:
        big = {2: "DOUBLE KILL", 3: "TRIPLE KILL", 4: "QUAD KILL"}.get(n, f"{n} KILLS")
    else:
        big = "KILL"
    parts = [str(h.get("player_name") or "").strip() or "?"] if with_player else []
    if typ in ("multikill", "ace") and n >= 2:
        parts.insert(0, f"{n}K")
    if map_name:
        parts.append(pretty_map(map_name))
    if h.get("round"):
        parts.append(f"round {h['round']}")
    return big, "  ·  ".join(parts)


def title_for_shown(title: tuple[str, str] | None, h: dict | None, shown: int) -> tuple[str, str] | None:
    """The title card for the kills the clip really shows: a recording cut off early (or a kill outside it) must not
    say TRIPLE KILL over two kills. Pure function; unit-tested."""
    ks = list((h or {}).get("kills") or [])
    if not title or not h or shown >= len(ks):
        return title
    h2 = {**h, "kills": ks[:shown]}
    if h.get("type") in ("multikill", "ace"):
        h2.update({"n": shown, "type": "multikill"})
    big = title_for(h2)[0]
    sub = re.sub(r"^\d+K(  ·  )?", f"{shown}K\\1" if shown >= 2 else "", title[1])
    return big, sub


def player_card(h: dict | None) -> dict | None:
    """{"name", "photo": Path|None, "steamid"} for the highlight's player. Nickname from data/players.json
    ({steam64: {"name": ..., "photo": ...}}) when present, else the in-game name; photo data/players/<steam64>.<ext>."""
    if not h:
        return None
    sid = str(h.get("player") or "")
    name = str(h.get("player_name") or "").strip() or "?"
    photo = None
    try:
        reg = json.loads(PLAYERS_JSON.read_text(encoding="utf-8")) if PLAYERS_JSON.exists() else {}
    except (OSError, json.JSONDecodeError):
        reg = {}
    entry = reg.get(sid) if isinstance(reg, dict) else None
    if isinstance(entry, dict):
        name = str(entry.get("name") or name).strip() or name
        if entry.get("photo"):
            cand = PLAYERS_DIR / Path(str(entry["photo"])).name
            photo = cand if cand.is_file() else None
    if photo is None and PLAYERS_DIR.is_dir():
        photo = _photo_named(sid) or _photo_named(name)
    return {"name": name, "photo": photo, "steamid": sid}


def _photo_key(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _photo_named(key: str) -> Path | None:
    """data/players/<key>.<ext>, matched case-insensitively and ignoring punctuation, so a photo named after the
    in-game nickname works too (e.g. 'vALenk.png' for the player 'valenk')."""
    want = _photo_key(key)
    if not want:
        return None
    for cand in sorted(PLAYERS_DIR.iterdir()):
        if cand.is_file() and cand.suffix.lower() in PHOTO_EXT and _photo_key(cand.stem) == want:
            return cand
    return None


def _fesc(s: str) -> str:
    r"""Escape a value for use inside a filtergraph option (paths, expressions).

    The graph is parsed twice: first the filter's argument string is extracted (unescaping \, \; \[ \] \'),
    then it is split into options on ':'. So ',' needs one backslash and ':' (Windows drive letters) needs two,
    which is why 'C\\:/Windows/Fonts/x.ttf' is the form that works everywhere."""
    out = str(s).replace("\\", "/")
    for ch in ("[", "]", ",", ";"):
        out = out.replace(ch, "\\" + ch)
    out = out.replace("'", "\\\\\\'").replace(":", "\\\\:")
    return out


def _drawtext(font: Path, textfile: Path, size: int, x: str, y: str, alpha: str | None = None,
              enable: str | None = None, color: str = "white") -> str:
    o = [f"fontfile={_fesc(font)}", f"textfile={_fesc(textfile)}", "expansion=none",
         f"fontsize={_fesc(size) if isinstance(size, str) else size}",
         f"fontcolor={color}", "borderw=2", "bordercolor=black@0.55", "shadowx=2", "shadowy=2", "shadowcolor=black@0.5",
         f"x={_fesc(x)}", f"y={_fesc(y)}"]
    if alpha:
        o.append(f"alpha={_fesc(alpha)}")
    if enable:
        o.append(f"enable={_fesc(enable)}")
    return "drawtext=" + ":".join(o)


# ---- filter graph --------------------------------------------------------------------------------

def _atempo_chain(factor: float) -> str:
    """atempo sounds best (and keeps time most accurately) between 0.5 and 2; chain instances beyond that."""
    parts = []
    f = factor
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    while f > 2.0:
        parts.append("atempo=2")
        f /= 2.0
    parts.append(f"atempo={f:.4f}")
    return ",".join(parts)


def _impact(kills_rel: list[float], fps: float) -> str:
    """zoompan (8 % punch that decays over ~0.3 s) and eq flash on each kill; times relative to the segment."""
    tau = max(1.0, 0.3 * fps)
    z = ["if(gte(in\\,%d)\\,exp(-(in-%d)/%.1f)\\,0)" % (round(k * fps), round(k * fps), tau) for k in kills_rel]
    fl = ["if(between(t\\,%.3f\\,%.3f)\\,1-(t-%.3f)/0.15\\,0)" % (k, k + 0.15, k) for k in kills_rel]

    def mx(xs: list[str]) -> str:
        e = xs[0]
        for x in xs[1:]:
            e = f"max({e}\\,{x})"
        return e
    return (f"zoompan=z='1+0.08*{mx(z)}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=WxH:fps={fps:g}"
            f",eq=brightness='0.3*{mx(fl)}':eval=frame")


def build_filter(segs: list[Seg], *, fps: float, width: int, height: int, smooth: bool, kills: list[float],
                 impact: bool, look: bool, fade: bool, has_music: bool, game_vol: float, music_vol: float,
                 texts: list[dict], voices: list[float] | None = None, voice_vol: float = 1.0,
                 card: dict | None = None, pad: float = 0.0, duck: list[tuple[float, float]] | None = None,
                 music_later: bool = False, lut: Path | None = None, edge_in: str | None = None,
                 edge_out: str | None = None) -> str:
    """texts: [{"file": Path, "size": int, "x": str, "y": str, "alpha": str|None, "enable": str|None, "font": Path}]
    voices: output-time offsets of the extra audio inputs that follow the clip (and the music, when there is one).
    card: {"idx": input index of the photo or None, "d": avatar px, "x", "y", "initial": Path|None, "font": Path}
    for the circular player card, which stays on screen for the whole clip.
    music_later: no music input, but the game audio is already turned down to game_vol because one song is laid
    over a whole montage afterwards (montage_music)."""
    total = total_duration(segs) + pad
    parts, labels = [], []
    for i, (a, b, s) in enumerate(segs):
        v = f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS"
        au = f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS"
        cut_before = i > 0 and a > segs[i - 1][1] + 0.01          # footage left out between the two segments
        cut_after = i + 1 < len(segs) and segs[i + 1][0] > b + 0.01
        if impact:
            ks = [k - a for k in kills if a <= k < b]
            if ks:
                v += "," + _impact(ks, fps).replace("s=WxH", f"s={width}x{height}")
        if abs(s - 1.0) > 1e-3:
            v += f",setpts=PTS/{s:.4f}"
            if s > 1.0:
                # a fast-forward keeps only the frames the output shows, right here: grading the others only to
                # drop them at the end cost most of the render time
                v += f",fps={fps:g}"
            if smooth and s < 1.0:
                v += f",minterpolate=fps={fps:g}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
            au += "," + _atempo_chain(s)
            if s > 1.5:
                au += ",volume=0.35"   # fast-forwarded game audio is a blur: keep it under the music and the voice lines
        seg_len = (b - a) / s
        if i == 0 and edge_in:     # a transition from the play before (montage_piece): no fade from black here
            v += "," + edge_filter(edge_in, "in", width, height, seg_len)
            au += ",afade=t=in:st=0:d=0.05"
        if i == len(segs) - 1 and edge_out:
            v += "," + edge_filter(edge_out, "out", width, height, seg_len)
            au += f",afade=t=out:st={max(0.0, seg_len - 0.05):.3f}:d=0.05"
        if cut_after:     # a cut is a quick flash: out to white, back in from white, the sound dips with it
            v += f",fade=t=out:st={max(0.0, seg_len - CUT_FLASH_OUT):.3f}:d={CUT_FLASH_OUT}:color=white"
            au += f",afade=t=out:st={max(0.0, seg_len - CUT_AUDIO_FADE):.3f}:d={CUT_AUDIO_FADE}"
        if cut_before:
            v += f",fade=t=in:st=0:d={CUT_FLASH_IN}:color=white"
            au += f",afade=t=in:st=0:d={CUT_AUDIO_FADE}"
        parts.append(v + f"[v{i}]")
        parts.append(au + f"[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    # (an fps filter straight after the concat instead made FFmpeg queue gigabytes of frames on some clips)
    if pad > 0:
        parts.append("".join(labels) + f"concat=n={len(segs)}:v=1:a=1[vcc][acc]")
        parts.append(f"[vcc]tpad=stop_mode=clone:stop_duration={pad:.3f}[vc0]")   # hold the last frame
        parts.append(f"[acc]apad=pad_dur={pad:.3f}[ac0]")
    else:
        parts.append("".join(labels) + f"concat=n={len(segs)}:v=1:a=1[vc0][ac0]")
    cur = "[vc0]"
    if look:
        # no dithering: it only adds ±1 level of noise per frame, which the encoder smooths away, at half the cost
        grade = f"lut3d=file={_fesc(lut)}:interp=trilinear" if lut else "eq=contrast=1.06:saturation=1.18:gamma=0.98"
        parts.append(f"{cur}{grade},vignette=angle=PI/4.6:dither=0[vl]")
        cur = "[vl]"
    if card:
        parts += _card_chain(card, fps)
        parts.append(f"{cur}[card]overlay=x={card['x']}:y={card['y']}:eof_action=pass[vcard]")
        cur = "[vcard]"
    post = [t["raw"] if "raw" in t else
            _drawtext(t["font"], t["file"], t["size"], t["x"], t["y"], t.get("alpha"), t.get("enable"), t.get("color", "white"))
            for t in texts]
    if fade and not edge_in:
        post.append("fade=t=in:st=0:d=0.5")
    if fade and not edge_out:
        post.append(f"fade=t=out:st={max(0.0, total - 0.7):.3f}:d=0.7")
    post.append(f"fps={fps:g}")   # constant frame rate: speed changes leave irregular timestamps behind
    parts.append(cur + ",".join(post) + "[vc]")
    ag = f"volume={game_vol:.2f}" if has_music or music_later else "anull"
    win = "+".join(f"between(t\\,{a - 0.25:.2f}\\,{b + 0.25:.2f})" for a, b in duck) if duck else ""
    if win:   # the game audio drops to half while a voice line plays (0.25 s either side), evaluated per frame
        ag = f"volume='{(game_vol if has_music or music_later else 1.0):.2f}*(1-0.5*min(1\\,{win}))':eval=frame"
    if fade and not edge_in:
        ag += ",afade=t=in:st=0:d=0.4"
    if fade and not edge_out:
        ag += f",afade=t=out:st={max(0.0, total - 0.7):.3f}:d=0.7"
    mix = ["[ag]"]
    parts.append(f"[ac0]{ag}[ag]")
    if has_music:
        fade_out = max(0.0, total - 2.0)
        mvol = f"volume={music_vol:.2f}"
        if win:   # and the music to 40 %
            mvol = f"volume='{music_vol:.2f}*(1-0.6*min(1\\,{win}))':eval=frame"
        parts.append(f"[1:a]atrim=start=0:end={total:.3f},asetpts=PTS-STARTPTS,{mvol},"
                     f"afade=t=in:st=0:d=1.5,afade=t=out:st={fade_out:.3f}:d=2[am]")
        mix.append("[am]")
    for j, at in enumerate(voices or []):
        idx = j + (2 if has_music else 1)
        parts.append(f"[{idx}:a]adelay={int(round(at * 1000))}:all=1,volume={voice_vol:.2f}[vo{j}]")
        mix.append(f"[vo{j}]")
    if len(mix) > 1:
        parts.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:dropout_transition=0:normalize=0[aout]")
    else:
        parts.append("[ag]anull[aout]")
    return ";".join(parts)


def _circle_alpha(d: int) -> str:
    r = d / 2
    return f"a='if(lte((X-{r:.1f})*(X-{r:.1f})+(Y-{r:.1f})*(Y-{r:.1f})\\,{r * r:.1f})\\,255\\,0)'"


def _card_chain(card: dict, fps: float) -> list[str]:
    """Filter chains producing [card]: an orange ring with the photo (or a blank circle with the initial) inside,
    fading in at the start and staying for the whole clip."""
    d, ring = card["d"], card["d"] + 6
    rgb = "r='r(X,Y)':g='g(X,Y)':b='b(X,Y)'"
    if card.get("idx") is not None:
        av = (f"[{card['idx']}:v]scale={d}:{d}:force_original_aspect_ratio=increase,crop={d}:{d},format=rgba,"
              f"geq={rgb}:{_circle_alpha(d)}[av]")
    else:
        av = f"color=c={CARD_BG}:s={d}x{d}:r={fps:g},format=rgba"
        if card.get("initial"):
            av += "," + _drawtext(card["font"], card["initial"], int(d * 0.5), "(w-tw)/2", "(h-th)/2", color="white@0.9")
        av += f",geq={rgb}:{_circle_alpha(d)}[av]"
    return [av,
            f"color=c={ACCENT}:s={ring}x{ring}:r={fps:g},format=rgba,geq={rgb}:{_circle_alpha(ring)}[ring]",
            f"[ring][av]overlay=3:3:format=auto,fade=t=in:st=0:d=0.3:alpha=1[card]"]


def plan_texts(segs: list[Seg], kills: list[float], *, width: int, height: int, title: tuple[str, str] | None,
               counter: bool, font: Path, tmp_base: Path, card: dict | None = None,
               clutch: dict | None = None) -> tuple[list[dict], list[Path], dict | None]:
    """Describe the drawtext calls (title card, player card texts, kill counter) and the player card geometry.
    The texts are written to small files next to the output (drawtext's textfile= needs no escaping); the caller
    deletes them after the encode. card: {"name", "photo"} -> returned spec has d/x/y/initial/idx(None).
    clutch: {"n", "round", "score": (ours, theirs) | None} puts the odds at the top left: 1vN counting down with the
    clutcher's kills (gone once all are down), the round and score under it."""
    texts: list[dict] = []
    files: list[Path] = []
    card_spec: dict | None = None

    def tf(s: str) -> Path:
        p = tmp_base.with_name(f"{tmp_base.name}.t{len(files)}.txt")
        p.write_text(s, encoding="utf-8")
        files.append(p)
        return p
    h = height
    env = (f"if(lt(t,0.3),t/0.3,if(lt(t,{TITLE_SECONDS - 0.5:.2f}),1,"
           f"if(lt(t,{TITLE_SECONDS:.2f}),({TITLE_SECONDS:.2f}-t)/0.5,0)))")
    en = f"between(t,0,{TITLE_SECONDS:.2f})"
    x0, bottom = int(h * 0.06), int(h * 0.11)        # left margin; bottom margin clears the demo playback bar
    if card:
        # lower third: [ring+avatar] NICKNAME / details, with the big label above it. The avatar and the nickname
        # stay for the whole clip; the label and the details go with the title card.
        d = int(h * 0.11)
        ring = d + 6
        cy = h - bottom - ring
        name_px, det_px = int(h * 0.05), int(h * 0.026)
        tx = x0 + ring + int(h * 0.016)
        texts.append({"font": font, "file": tf(card["name"]), "size": name_px, "x": str(tx),
                      "y": str(cy + int(ring * 0.12)), "alpha": "min(1,t/0.3)", "enable": None})
        if title:
            texts.append({"font": font, "file": tf(title[1]), "size": det_px, "x": str(tx),
                          "y": str(cy + int(ring * 0.12) + name_px + 8), "alpha": env, "enable": en})
            big_px = int(h * 0.075)
            texts.append({"font": font, "file": tf(title[0]), "size": big_px, "x": str(x0),
                          "y": str(cy - 12 - big_px), "alpha": env, "enable": en})
        initial = (card["name"].strip()[:1] or "?").upper()
        card_spec = {"d": d, "x": x0, "y": cy, "idx": None, "font": font,
                     "initial": None if card.get("photo") else tf(initial)}
    elif title:
        big, sub = title
        big_px, sub_px = int(h * 0.075), int(h * 0.028)
        texts.append({"font": font, "file": tf(big), "size": big_px, "x": str(x0),
                      "y": str(h - bottom - sub_px - 10 - big_px), "alpha": env, "enable": en})
        texts.append({"font": font, "file": tf(sub), "size": sub_px, "x": str(x0),
                      "y": str(h - bottom - sub_px), "alpha": env, "enable": en})
    if counter and kills:
        outs = [out_time(segs, k) for k in sorted(kills)]
        num_px, lab_px, margin = int(h * 0.09), int(h * 0.024), int(h * 0.05)
        total = total_duration(segs)
        label = tf("KILLS")
        for j, o in enumerate(outs):
            end = outs[j + 1] if j + 1 < len(outs) else total + 1
            if end - o < 0.05:
                continue
            texts.append({"font": font, "file": tf(str(j + 1)), "size": num_px, "x": f"w-tw-{margin}",
                          "y": str(margin), "alpha": f"min(1,(t-{o:.3f})/0.12)", "enable": f"between(t,{o:.3f},{end:.3f})"})
        texts.append({"font": font, "file": label, "size": lab_px, "x": f"w-tw-{margin}",
                      "y": str(margin + num_px + 2), "alpha": "0.85", "enable": f"gte(t,{outs[0]:.3f})"})
    if clutch and clutch.get("n"):
        n = int(clutch["n"])
        ks = sorted(kills)
        # the odds start when the clutch does (the last teammate down); kills before it were made with help
        start = clutch.get("start")
        if start is None:   # not known (an old clip of a deleted match): the last n kills are the clutch
            start = max(0.0, ks[-n] - 1.5) if len(ks) >= n else 0.0
        ks = [k for k in ks if k >= start - 0.01]
        kills_out = [out_time(segs, k) for k in ks]
        a = out_time(segs, start)
        big = find_display_font() or font
        small_px, x0, y0 = int(h * 0.09), int(h * 0.05), int(h * 0.05)
        sc = clutch.get("score")
        line = "  ·  ".join(x for x in (f"ROUND {clutch['round']}" if clutch.get("round") else "",
                                         f"{sc[0]}–{sc[1]}" if sc else "") if x)
        # the announcement: 1vN slams in at the middle, holds, then flies to the top left where the counter goes on
        an = clutch_announcement(a, kills_out, total_duration(segs))
        if an:
            hold, fly = an["hold"], an["fly"]
            word = tf(f"1v{n}")
            for t_ in announcement_texts(a, hold, fly, big, word, width, h, small_px, x0, y0):
                texts.append(t_)
            if line:
                texts.append({"font": font, "file": tf(line), "size": int(h * 0.03), "x": "(w-text_w)/2",
                              "y": f"(h/2)+{int(h * 0.13)}", "alpha": f"min(1,(t-{a + 0.25:.3f})/0.2)*min(1,({hold:.3f}-t)/0.12)",
                              "enable": f"between(t,{a + 0.25:.3f},{hold:.3f})"})
            counter_from = fly
        else:
            counter_from = a
        for state in clutch_states(n, kills_out, total_duration(segs), a):
            frm = max(state["from"], counter_from)
            if state["to"] - frm < 0.05:
                continue
            texts.append({"font": big, "file": tf(state["text"]), "size": small_px, "x": str(x0), "y": str(y0),
                          "alpha": "1" if frm == counter_from and an else f"min(1,(t-{frm:.3f})/0.12)",
                          "enable": f"between(t,{frm:.3f},{state['to']:.3f})"})
        if line:
            texts.append({"font": font, "file": tf(line), "size": int(h * 0.024), "x": str(x0),
                          "y": str(y0 + small_px + 2), "alpha": f"0.85*min(1,(t-{counter_from:.3f})/0.25)",
                          "enable": f"gte(t,{counter_from:.3f})"})
    return texts, files, card_spec


ANNOUNCE_HOLD, ANNOUNCE_FLY = 1.1, 0.3


def clutch_announcement(a: float, kills_out: list[float], total: float) -> dict | None:
    """When the centred 1vN announcement holds and when it has flown to the corner: {"hold": end of the hold,
    "fly": end of the flight}. It gets out of the way of the clutch's first kill, and is left out when there is
    no room for it (less than half a second). Pure function; unit-tested."""
    nxt = min([k for k in kills_out if k > a + 0.05] + [total])
    hold = min(a + ANNOUNCE_HOLD, nxt - 0.1)
    if hold - a < 0.5:
        return None
    return {"hold": round(hold, 3), "fly": round(min(hold + ANNOUNCE_FLY, total), 3)}


def announcement_texts(a: float, hold: float, fly: float, font: Path, word: Path, width: int, h: int,
                       small_px: int, x0: int, y0: int) -> list[dict]:
    """The drawtext/drawbox steps of the 1vN announcement: the picture dips, the number slams in from 1.6x with an
    orange/cyan split for a moment and an accent shadow, holds in the middle, then shrinks and flies to the corner
    (x0, y0, small_px) where the counter takes over. Pure function; unit-tested."""
    big = int(h * 0.26)
    off = max(4, int(big * 0.035))
    d = max(0.05, fly - hold)
    slam = f"(1-pow(1-min(1,max(0,(t-{a:.3f})/0.25)),3))"          # 0 -> 1 as it lands
    go = f"(1-pow(1-min(1,max(0,(t-{hold:.3f})/{d:.3f})),3))"       # 0 -> 1 as it flies
    size = f"if(lt(t,{hold:.3f}),{big}*(1+0.6*(1-{slam})),{big}+({small_px}-{big})*{go})"
    x = f"if(lt(t,{hold:.3f}),(w-text_w)/2,(w-text_w)/2*(1-{go})+{x0}*{go})"
    y = f"if(lt(t,{hold:.3f}),(h-text_h)/2,(h-text_h)/2*(1-{go})+{y0}*{go})"
    on = f"between(t,{a:.3f},{fly:.3f})"
    split = f"between(t,{a:.3f},{a + 0.18:.3f})"
    fade = f"max(0,1-(t-{a:.3f})/0.18)*0.8"
    return [
        {"raw": f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.28:t=fill:enable='between(t,{a:.3f},{hold:.3f})'"},
        {"font": font, "file": word, "size": size, "x": f"{x}-{int(h * 0.016)}", "y": y, "alpha": fade, "enable": split,
         "color": "0x22D3EE"},
        {"font": font, "file": word, "size": size, "x": f"{x}+{int(h * 0.016)}", "y": y, "alpha": fade, "enable": split,
         "color": INTRO_ACCENT},
        {"font": font, "file": word, "size": size, "x": f"{x}+{off}*(1-{go})", "y": f"{y}+{off}*(1-{go})",
         "alpha": "0.95", "enable": on, "color": INTRO_ACCENT},
        {"font": font, "file": word, "size": size, "x": x, "y": y, "alpha": f"min(1,(t-{a:.3f})/0.08)", "enable": on},
    ]


def clutch_states(n: int, kills_out: list[float], total: float, start: float = 0.0) -> list[dict]:
    """[{"text", "from", "to"}] of the clutch odds: 1vN from `start` (when the clutch began), one fewer at each of
    the clutcher's kills after it, gone once nobody is left. Pure function; unit-tested."""
    out, t, left = [], start, n
    kills_out = [k for k in kills_out if k >= start - 0.01]
    for k in kills_out:
        if left <= 0:
            break
        if k > t + 0.05:
            out.append({"text": f"1v{left}", "from": t, "to": k})
        t, left = k, left - 1
    if left > 0 and total > t + 0.05:
        out.append({"text": f"1v{left}", "from": t, "to": total + 1})
    return out


def round_score(meta: dict, round_num: int, steamid: str) -> tuple[int, int] | None:
    """(rounds won by the player's side, rounds lost) before `round_num`, following the player across the halftime
    side swap. None without round data. Pure function; unit-tested."""
    rounds = meta.get("rounds") or []
    if not rounds or not steamid:
        return None
    ours = theirs = 0
    for r in rounds:
        if int(r.get("num") or 0) >= round_num:
            break
        side = (r.get("teams") or {}).get(str(steamid))
        if not side or not r.get("winner"):
            continue
        if r["winner"] == side:
            ours += 1
        else:
            theirs += 1
    return ours, theirs


def voice_prefs(highlight: dict | None) -> list[str]:
    """Voice sub-folders that fit a highlight, best first: its type (ace / clutch / multikill / knife / ninja_defuse)."""
    if not highlight:
        return []
    typ = str(highlight.get("type") or "")
    n = int(highlight.get("n") or len(highlight.get("kills") or []) or 0)
    if typ == "multikill" and n >= 5:
        return ["ace", "multikill"]
    return [typ] if typ in voice.TYPE_SUBS else []


def voice_overrun(voices: list[tuple[Path, float]], durs: dict[Path, float], total: float, tail: float = 1.0) -> float:
    """Seconds by which the latest voice line (plus a tail that clears the fade-out) runs past the end of the clip."""
    return max((at + durs.get(pth, 0.0) + tail for pth, at in voices), default=0.0) - total


def plan_voices(segs: list[Seg], kills: list[float], total: float, highlight: dict | None = None,
                choice: list[tuple[Path, float]] | None = None, pins: dict | None = None) -> list[tuple[Path, float]]:
    """(file, output time) of the voice lines: a 'start' line at the beginning, a 'kill' line on every kill but
    the last, a 'final' line right after the final kill. Lines from the sub-folder that matches the highlight
    (its type, or the kill number) are preferred; slots without files are silent."""
    out: list[tuple[Path, float]] = []
    prefs = voice_prefs(highlight)
    picked = iter([pth for pth, _ in choice]) if choice is not None else None
    pins = pins or {}

    def pick(slot: str, prefer: list[str]) -> Path | None:
        return next(picked, None) if picked is not None else voice.pick(slot, prefer, pins.get(slot))
    p = pick("start", prefs)
    if p:
        out.append((p, 0.3))
    ks = sorted(kills)
    for j, k in enumerate(ks[:-1], start=1):
        p = pick("kill", [str(j)])
        if p:
            out.append((p, min(total, out_time(segs, k) + 0.15)))
    if ks:
        p = pick("final", prefs)
        if p:
            out.append((p, min(total, out_time(segs, ks[-1]) + 0.4)))
    return out


BELOW_NORMAL_PRIORITY_CLASS = 0x00004000   # Windows process priority


def run_ffmpeg_with_progress(args: list[str], total_seconds: float, on_fraction, low_priority: bool = False) -> tuple[int, str]:
    """Run an ffmpeg command, reporting the encoded position as a fraction of `total_seconds` through
    `-progress pipe:1`. Returns (return code, stderr tail). low_priority: below normal priority on Windows, so it
    only takes the CPU time the game leaves."""
    cmd = list(args)
    cmd[1:1] = ["-v", "error", "-nostats", "-progress", "pipe:1"]     # right after the executable, before -y
    flags = BELOW_NORMAL_PRIORITY_CLASS if low_priority and os.name == "nt" else 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            creationflags=flags)
    _running_procs.add(proc)
    err_chunks: list[str] = []

    def drain() -> None:   # stderr must be read concurrently or ffmpeg blocks once the pipe buffer is full
        for chunk in proc.stderr:
            err_chunks.append(chunk)
    t = threading.Thread(target=drain, daemon=True)
    t.start()
    last = -1.0
    try:
        for line in proc.stdout:   # key=value lines; out_time_us advances as frames are written
            if line.startswith("out_time_us=") and total_seconds > 0:
                try:
                    frac = min(0.99, int(line.split("=", 1)[1]) / 1_000_000 / total_seconds)
                except ValueError:
                    continue
                if frac - last >= 0.01:
                    last = frac
                    on_fraction(frac)
    finally:
        proc.wait()
        t.join(timeout=5)
        _running_procs.discard(proc)
    return proc.returncode, "".join(err_chunks)[-4000:]


_running_procs: set = set()


def cancel_running_ffmpeg() -> int:
    """Terminate every ffmpeg started through run_ffmpeg_with_progress (used to abort a cinematic job)."""
    n = 0
    for proc in list(_running_procs):
        try:
            proc.kill()
            n += 1
        except OSError:
            pass
    return n


def plan_clip(kill_times: list[float], duration: float, opts: dict):
    """(kills, slow-motion kills, kept windows, trim mode, plan) of a clip: the timing make_cinematic renders, also
    used by the montage planner to know in advance where each play's kills land. opts may carry "_no_slow" (this
    play runs without slow motion) and "_lead" (seconds kept before the first kill: shifts its kills in time).
    plan(keep) gives the (start, end, speed) segments. Pure function; unit-tested."""
    slow = float(opts["cine_slow"])
    kills = sorted(k for k in kill_times if 0 <= k <= duration)
    slow_kills = [] if opts.get("_no_slow") else (kills[-1:] if opts["cine_kills"] == "last" else kills)
    mode = opts.get("cine_trim", "off")
    lead = opts.get("_lead")
    if mode in ("cut", "fast"):
        keep = keep_windows(kills, duration, lead=float(lead if lead is not None else opts.get("cine_trim_lead", 2.0)))
    elif lead is not None and kills:
        keep = [(max(0.0, kills[0] - float(lead)), duration)]   # no trimming: only the start moves
    else:
        keep = [(0.0, duration)]

    def plan(keep_windows_: list[tuple[float, float]]) -> list[Seg]:
        sg = speed_segments(slow_kills, keep_windows_, slow, ramp=bool(opts.get("cine_ramp")))
        if mode == "fast":
            sg = fill_gaps(sg, duration, float(opts.get("cine_fast", 6.0)))
            if opts.get("cine_skip_waits", True):
                sg = skip_waits(sg, float(opts.get("cine_fast", 6.0)))
        return sg or [(0.0, duration, 1.0)]
    return kills, slow_kills, keep, mode, plan


def clip_timing(kill_times: list[float], duration: float, opts: dict) -> dict:
    """{"seconds": output length, "hit": output time of the first slow-motion kill or None} of a clip rendered with
    `opts` (no voice lines). Pure function; unit-tested."""
    kills, slow_kills, keep, mode, plan = plan_clip(kill_times, duration, opts)
    segs = plan(keep)
    total = total_duration(segs)
    return {"seconds": total, "hit": out_time(segs, slow_kills[0]) if slow_kills else None,
            "tail": total - out_time(segs, kills[-1]) if kills else total}


def make_cinematic(ffmpeg: Path, ffprobe: Path, src: Path, dst: Path, kill_times: list[float], music: Path | None,
                   opts: dict, title: tuple[str, str] | None = None, fps_hint: float | None = None,
                   crf: int = 20, highlight: dict | None = None, card: dict | None = None,
                   progress=None, music_later: bool = False, low_priority: bool = False,
                   score: tuple[int, int] | list[int] | None = None, clutch_at: float | None = None) -> dict:
    """low_priority: FFmpeg runs below normal priority (it renders while CS2 records and must not slow it down).
    music_later: a montage piece; no music now (one song goes over the joined montage), game audio already
    at the music mix level. The result has "hits" (output seconds of the slow-motion kills) and "duck" (output
    windows of the voice lines) for laying that song.
    progress(stage: str, fraction: float | None) is called as the work advances (fraction only while encoding)."""
    def report(stage: str, frac: float | None = None) -> None:
        if progress:
            try:
                progress(stage, frac)
            except Exception:  # noqa: BLE001
                pass
    report("planning the clip")
    info = probe_video(ffprobe, src)
    duration, width, height = info["duration"], info["width"], info["height"]
    fps = info["fps"] or fps_hint or 60.0
    kills, slow_kills, keep, mode, plan = plan_clip(kill_times, duration, opts)
    title = title_for_shown(title, highlight, len(kills))
    segs = plan(keep)
    if float(opts.get("_trim") or 0) > 0:          # a montage play ends on a beat: its tail is cut that much
        segs = trim_out_end(segs, float(opts["_trim"]))
    pins = voice.parse_pins(opts.get("cine_voice_pick"))
    voices = plan_voices(segs, kills, total_duration(segs), highlight, pins=pins) if opts.get("cine_voice") else []
    pad = 0.0
    durs: dict[Path, float] = {}
    if voices:
        # let the last voice line finish: keep more real footage after the final kill when the recording has it,
        # otherwise hold the last frame (pad)
        durs = {pth: probe_duration(ffprobe, pth) for pth, _ in voices}
        for _ in range(4):
            need = voice_overrun(voices, durs, total_duration(segs))
            if need <= 0.05:
                break
            if mode in ("cut", "fast") and keep[-1][1] < duration - 0.05:
                keep[-1] = (keep[-1][0], min(duration, keep[-1][1] + need * 1.2))
                segs = plan(keep)
                voices = plan_voices(segs, kills, total_duration(segs), highlight, choice=voices)
            else:
                pad = need
                break
    edge_in, edge_out = opts.get("_in"), opts.get("_out")   # transitions to the plays before and after
    if edge_in:
        segs = split_at_out(segs, EDGE_SECONDS)
    if edge_out and not pad:
        segs = split_at_out(segs, total_duration(segs) - EDGE_SECONDS)
    else:
        edge_out = None
    total_out = total_duration(segs) + pad
    has_music = music is not None and music.exists()
    clutch = ({"n": int(highlight.get("n") or 0), "round": highlight.get("round"), "score": score, "start": clutch_at}
              if opts.get("cine_clutch", True) and highlight and highlight.get("type") == "clutch" else None)
    want_text = opts.get("cine_title") or opts.get("cine_counter") or (opts.get("cine_card") and card) or clutch
    font = find_font() if want_text else None
    texts, text_files, card_spec = plan_texts(
        segs, kills, width=width, height=height, title=title if opts.get("cine_title") else None,
        counter=bool(opts.get("cine_counter")), font=font, tmp_base=dst,
        card=card if opts.get("cine_card") else None, clutch=clutch) if font else ([], [], None)
    offset = 0.0
    if has_music and opts.get("cine_music_sync") and slow_kills:
        report("finding the drop in the music")
        offset = music_offset(ffmpeg, music, out_time(segs, slow_kills[0]), total_out)
    args = [str(ffmpeg), "-y", "-i", str(src)]
    if has_music:
        args += ["-stream_loop", "-1"] + (["-ss", f"{offset:.3f}"] if offset > 0 else []) + ["-i", str(music)]
    for p, _ in voices:
        args += ["-i", str(p)]
    if card_spec and card and card.get("photo"):
        card_spec["idx"] = 1 + (1 if has_music else 0) + len(voices)
        args += ["-loop", "1", "-framerate", f"{fps:g}", "-i", str(card["photo"])]
    graph = build_filter(segs, fps=fps, width=width, height=height, smooth=bool(opts.get("cine_smooth")), kills=kills,
                         impact=bool(opts.get("cine_impact")), look=bool(opts.get("cine_look")),
                         fade=bool(opts.get("cine_fade")), has_music=has_music,
                         game_vol=float(opts["cine_game_volume"]), music_vol=float(opts["cine_music_volume"]), texts=texts,
                         voices=[at for _, at in voices], voice_vol=float(opts.get("cine_voice_volume", 1.0)),
                         card=card_spec, pad=pad,
                         duck=[(at, at + durs.get(pth, 1.5)) for pth, at in voices] if voices else None,
                         music_later=music_later,
                         lut=lut_file(str(opts.get("cine_grade", "teal_orange"))) if opts.get("cine_look") else None,
                         edge_in=edge_in, edge_out=edge_out)
    args += ["-filter_complex", graph, "-map", "[vc]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", str(crf), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
             "-t", f"{total_out:.3f}", str(dst)]
    t0 = time.time()
    report("encoding", 0.0)
    try:
        code, err = run_ffmpeg_with_progress(args, total_out, lambda f: report("encoding", f), low_priority=low_priority)
    finally:
        for p in text_files:
            p.unlink(missing_ok=True)
    if code != 0 or not dst.exists():
        raise RuntimeError("ffmpeg failed: " + err[-1500:])
    report("done", 1.0)
    effects = [k[5:] for k in ("cine_ramp", "cine_impact", "cine_look", "cine_fade", "cine_title", "cine_counter")
               if opts.get(k)]
    if card_spec:
        effects.append("card" + (" (photo)" if card.get("photo") else " (no photo yet)"))
    if mode != "off":
        effects.insert(0, mode)
    if mode == "fast" and opts.get("cine_skip_waits", True) and any(segs[i][0] > segs[i - 1][1] + 0.01 for i in range(1, len(segs))):
        effects.insert(1, "long waits skipped")
    if want_text and not font:
        effects.append("no font found: text overlays skipped")
    if voices:
        effects.append(f"{len(voices)} voice line(s)" + (f", held last frame {pad:.1f}s" if pad > 0 else ""))
    return {"seconds": round(total_out, 1), "slow_segments": sum(1 for s in segs if s[2] < 0.999),
            "dead_seconds": round(duration - sum(b - a for a, b in keep), 1), "music": music.name if has_music else None,
            "music_offset": round(offset, 1), "effects": effects, "encode_time": round(time.time() - t0, 1),
            "hits": [round(out_time(segs, k), 3) for k in slow_kills],
            "duck": [(round(at, 3), round(at + durs.get(pth, 1.5), 3)) for pth, at in voices]}


# ---- one song over a whole montage ---------------------------------------------------------------------------

def bed_tracks_needed(first_len: float, offset: float, total: float, next_lens: list[float], xfade: float = 3.0) -> int:
    """How many of next_lens must follow the first song (played from `offset`) so the music lasts `total` seconds,
    each one crossfading `xfade` seconds into the previous. 0 when the first song is long enough.
    Pure function; unit-tested."""
    have = max(0.0, first_len - offset)
    n = 0
    while have < total and n < len(next_lens):
        have += max(0.0, next_lens[n] - xfade)
        n += 1
    return n


def music_bed_args(ffmpeg: Path, first: Path, offset: float, others: list[Path], dst: Path, xfade: float = 3.0,
                   first_end: float | None = None, spans: list[tuple[float, float]] | None = None) -> list[str]:
    """FFmpeg command joining the first song (from `offset`, up to `first_end`) and the songs after it (each only
    its span (start, end), see song_span) into one track, each crossfading into the next, so a long montage keeps
    one continuous soundtrack. Pure function; unit-tested."""
    args = [str(ffmpeg), "-y"] + (["-ss", f"{offset:.3f}"] if offset > 0 else [])
    if first_end is not None and first_end > offset:
        args += ["-t", f"{first_end - offset:.3f}"]
    args += ["-i", str(first)]
    for k, o in enumerate(others):
        if spans and k < len(spans):
            a, b = spans[k]
            args += (["-ss", f"{a:.3f}"] if a > 0 else []) + ["-t", f"{b - a:.3f}"]
        args += ["-i", str(o)]
    chain, prev = [], "[0:a]"
    for i in range(1, len(others) + 1):
        chain.append(f"{prev}[{i}:a]acrossfade=d={xfade:g}:c1=tri:c2=tri[x{i}]")
        prev = f"[x{i}]"
    return args + ["-filter_complex", ";".join(chain), "-map", prev, "-c:a", "aac", "-b:a", "256k", str(dst)]


def montage_music_args(ffmpeg: Path, video: Path, music: Path, dst: Path, total: float, offset: float,
                       music_vol: float, duck: list[tuple[float, float]] | None = None) -> list[str]:
    """FFmpeg command laying one song (looped when the montage is longer, started `offset` seconds in) under the
    joined montage's own sound, dipping to 40 % while a voice line plays. The video stream is copied.
    Pure function; unit-tested."""
    win = "+".join(f"between(t\\,{a - 0.25:.2f}\\,{b + 0.25:.2f})" for a, b in duck) if duck else ""
    vol = f"volume='{music_vol:.2f}*(1-0.6*min(1\\,{win}))':eval=frame" if win else f"volume={music_vol:.2f}"
    graph = (f"[1:a]atrim=start=0:end={total:.3f},asetpts=PTS-STARTPTS,{vol},afade=t=in:st=0:d=1.5,"
             f"afade=t=out:st={max(0.0, total - 2.5):.3f}:d=2.5[m];"
             "[0:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
    return [str(ffmpeg), "-y", "-i", str(video), "-stream_loop", "-1"] + (["-ss", f"{offset:.3f}"] if offset > 0 else []) + \
        ["-i", str(music), "-filter_complex", graph, "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
         "-b:a", "192k", "-movflags", "+faststart", "-t", f"{total:.3f}", str(dst)]


# ---- montage intro card ----------------------------------------------------------------------------------

INTRO_SECONDS = 3.5
INTRO_ACCENT = "0xF5A623"


def intro_stats(kills: list[int], types: list[str]) -> str:
    """The "something nice" line of the intro from the plays' kill counts and types: "4 plays · 2 aces · 1 clutch
    · 17 kills" (zero counts left out). Pure function; unit-tested."""
    n = len(kills)
    aces = sum(1 for k, t in zip(kills, types) if t == "ace" or k >= 5)
    fours = sum(1 for k, t in zip(kills, types) if k == 4 and t != "clutch")
    clutches = sum(1 for t in types if t == "clutch")
    parts = [f"{n} play{'s' if n != 1 else ''}"]
    for c, one, many in ((aces, "ace", "aces"), (fours, "4K", "4Ks"), (clutches, "clutch", "clutches")):
        if c:
            parts.append(f"{c} {one if c == 1 else many}")
    if sum(kills):
        parts.append(f"{sum(kills)} kills")
    return " · ".join(parts)


OUTRO_SECONDS = 4.5


def _ease(t0: float, d: float) -> str:
    """0 -> 1 between t0 and t0 + d, easing out (a strong start that settles), as an FFmpeg expression of t."""
    return f"(1-pow(1-min(1,max(0,(t-{t0})/{d})),3))"


def fit_size(text: str, width: int, height: int, top: float, share: float = 0.86, aspect: float = 0.62) -> int:
    """The font size that makes `text` fill about `share` of the width, `top` x height at most (a display face's
    letters are about `aspect` of the size wide). Pure function; unit-tested."""
    n = max(1, len(text.strip()))
    return max(round(height * 0.05), min(round(height * top), round(width * share / (n * aspect))))


def card_args(ffmpeg: Path, src: Path, at: float, dst: Path, texts: dict, words: dict, font: Path, display: Path,
              width: int, height: int, fps: float, sample_rate: int, channels: int, crf: int, seconds: float,
              kind: str, lineup: list[dict] | None = None) -> list[str]:
    """FFmpeg command for an animated title card on one frame of `src` (at `at` s): the frame punches in and melts
    into a dark blurred backdrop that keeps drifting, an accent bar sweeps in, the big title (texts["big"], the
    display face, sized to fill the width) rises into place over its accent shadow, the accent line
    (texts["line"]) slides in, the small line (texts["small"]) fades in, and a light sweep crosses the screen.
    kind "intro" ends with a push in and a fade to black (the montage starts), "outro" with a slow fade to black.
    words: the same texts as strings (for sizing). Encoded like the montage pieces so it joins them without
    re-encoding. lineup (the end card): [{"photo": Path|None, "initial": textfile|None, "name": textfile, "kills":
    textfile}] - the squad pops in under the title one by one, round avatars with their nickname and kills.
    Pure function; unit-tested."""
    frames = max(1, round(seconds * fps))
    lineup = lineup or []
    big = fit_size(words["big"], width, height, 0.36 if kind == "outro" and not lineup else 0.24)
    line_px, small_px = round(height * 0.058), round(height * 0.036)
    bar_w, bar_h = round(width * 0.24), max(4, round(height * 0.01))
    cy = round(height * (0.26 if lineup else 0.42))
    off = max(4, round(big * 0.035))                # the accent shadow's offset
    end_fade = 0.35 if kind == "intro" else 0.6
    out_a = f"*if(gt(t,{seconds - end_fade:.2f}),max(0,({seconds:.2f}-t)/{end_fade}),1)"
    punch = f"min(1,on/{max(1, round(0.7 * fps))})"
    push = f"+0.25*pow(max(0,(on-{frames - round(0.45 * fps)})/{max(1, round(0.45 * fps))}),2)" if kind == "intro" else ""
    z = f"1+0.15*(1-pow(1-{punch},3))+0.04*on/{frames}{push}"
    big_y = f"{cy}-{big}/2+{round(height * 0.07)}*(1-{_ease(0.4, 0.55)})"
    line_y = cy + big // 2 + round(height * 0.075)
    sweep_w = round(width * 0.06)
    chain = [
        f"[0:v]loop=loop={frames}:size=1:start=0,fps={fps:g},trim=end_frame={frames},setpts=N/FRAME_RATE/TB,"
        f"scale={round(width * 1.25) // 2 * 2}:-2,"
        f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={fps:g},split[sharp][soft]",
        "[soft]gblur=sigma=22,eq=brightness=-0.26:saturation=0.45,vignette=PI/3.6[dark]",
        "[sharp][dark]xfade=transition=fade:duration=0.7:offset=0.05[bg]",
        f"color=c={INTRO_ACCENT}:s={bar_w}x{bar_h}:r={fps:g}:d={seconds:.3f}[bar]",
        f"[bg][bar]overlay=x='-w+((W-w)/2+w)*{_ease(0.3, 0.45)}':y={line_y - round(height * 0.03)}:eof_action=pass[bb]",
        f"color=c=white:s={sweep_w}x{height * 2}:r={fps:g}:d={seconds:.3f},format=rgba,colorchannelmixer=aa=0.16,"
        f"rotate=0.4:c=none:ow=rotw(0.4):oh=roth(0.4)[shine]",
        "[bb]" + ",".join([
            _drawtext(display, texts["big"], big, f"(w-text_w)/2+{off}", f"{big_y}+{off}",
                      alpha=f"0.95*{_ease(0.45, 0.4)}{out_a}", color=INTRO_ACCENT),
            _drawtext(display, texts["big"], big, "(w-text_w)/2", big_y, alpha=f"{_ease(0.4, 0.4)}{out_a}"),
            _drawtext(display, texts["line"], line_px, f"(w-text_w)/2-{round(width * 0.04)}*(1-{_ease(0.85, 0.5)})",
                      str(line_y), alpha=f"{_ease(0.85, 0.35)}{out_a}", color=INTRO_ACCENT),
            _drawtext(font, texts["small"], small_px, "(w-text_w)/2", str(line_y + line_px + round(height * 0.03)),
                      alpha=f"{_ease(1.25, 0.5)}{out_a}")]) + "[tx]",
        f"[tx][shine]overlay=x='-w+(W+2*w)*min(1,max(0,(t-1.0)/0.8))':y=-h/4:eof_action=pass[sh]",
    ]
    cur, extra = "[sh]", []
    if lineup:
        d = round(height * 0.14)
        ring = d + 8
        step = max(round(ring * 1.9), round(width * 0.12))
        x_first = (width - step * (len(lineup) - 1)) // 2 - ring // 2
        y_av = round(height * 0.665)
        rgb = "r='r(X,Y)':g='g(X,Y)':b='b(X,Y)'"
        for i, p in enumerate(lineup):
            t0 = 1.5 + 0.25 * i
            if p.get("photo"):
                extra += ["-loop", "1", "-framerate", f"{fps:g}", "-t", f"{seconds:.3f}", "-i", str(p["photo"])]
                av = (f"[{1 + len(extra) // 8}:v]scale={d}:{d}:force_original_aspect_ratio=increase,crop={d}:{d},"
                      f"format=rgba,geq={rgb}:{_circle_alpha(d)}[av{i}]")
            else:
                av = f"color=c={CARD_BG}:s={d}x{d}:r={fps:g}:d={seconds:.3f},format=rgba"
                if p.get("initial"):
                    av += "," + _drawtext(display, p["initial"], int(d * 0.5), "(w-tw)/2", "(h-th)/2", color="white@0.9")
                av += f",geq={rgb}:{_circle_alpha(d)}[av{i}]"
            chain += [av,
                      f"color=c={ACCENT}:s={ring}x{ring}:r={fps:g}:d={seconds:.3f},format=rgba,geq={rgb}:{_circle_alpha(ring)}[ring{i}]",
                      f"[ring{i}][av{i}]overlay=4:4:format=auto,fade=t=in:st={t0:.2f}:d=0.3:alpha=1[p{i}]"]
            cx = x_first + i * step
            chain.append(f"{cur}[p{i}]overlay=x={cx}:y='{y_av}+{round(height * 0.04)}*(1-{_ease(t0, 0.4)})':eof_action=pass[l{i}]")
            cur = f"[l{i}]"
            name_y, kills_y = y_av + ring + round(height * 0.02), y_av + ring + round(height * 0.02) + round(height * 0.04)
            col = f"{cx + ring // 2}-text_w/2"
            chain.append(f"{cur}" + ",".join([
                _drawtext(display, p["name"], round(height * 0.034), col, str(name_y), alpha=f"{_ease(t0 + 0.1, 0.3)}{out_a}"),
                _drawtext(font, p["kills"], round(height * 0.026), col, str(kills_y), alpha=f"{_ease(t0 + 0.2, 0.3)}{out_a}",
                          color=INTRO_ACCENT)]) + f"[n{i}]")
            cur = f"[n{i}]"
    chain.append(f"{cur}fade=t=out:st={seconds - end_fade:.2f}:d={end_fade},format=yuv420p[v]")
    layout = "stereo" if channels == 2 else "mono" if channels == 1 else f"{channels}c"
    return [str(ffmpeg), "-y", "-ss", f"{max(0.0, at):.3f}", "-i", str(src), "-f", "lavfi", "-t", f"{seconds:.3f}",
            "-i", f"anullsrc=r={sample_rate}:cl={layout}"] + extra + ["-filter_complex", ";".join(chain),
            "-map", "[v]", "-map", "1:a", "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast",
            "-crf", str(crf), "-pix_fmt", "yuv420p", "-r", f"{fps:g}", "-c:a", "aac", "-b:a", "192k",
            "-ar", str(sample_rate), "-ac", str(channels), "-movflags", "+faststart", "-t", f"{seconds:.3f}", str(dst)]


def _make_card(ffmpeg: Path, ffprobe: Path, piece: Path, at: float, dst: Path, words: dict, crf: int, seconds: float,
               kind: str, players: list[dict] | None = None) -> float:
    font, display = find_font(), find_display_font()
    if font is None or display is None:
        return 0.0
    info = probe_video(ffprobe, piece)
    sr, ch = probe_audio(ffprobe, piece)
    files = {}
    for k, v in words.items():
        f = dst.with_name(f"{dst.stem}_{k}.txt")
        f.write_text(v or " ", encoding="utf-8")
        files[k] = f
    lineup = []
    for i, p in enumerate(players or []):
        item = {"photo": p.get("photo")}
        for k, v in (("name", p["name"]), ("kills", f"{p['kills']} kill{'s' if p['kills'] != 1 else ''}"),
                     ("initial", (p["name"].strip()[:1] or "?").upper())):
            f = dst.with_name(f"{dst.stem}_p{i}{k}.txt")
            f.write_text(v, encoding="utf-8")
            files[f"p{i}{k}"] = f
            item[k] = f
        if item["photo"]:
            item["initial"] = None
        lineup.append(item)
    try:
        r = subprocess.run(card_args(ffmpeg, piece, at, dst, files, words, font, display, info["width"], info["height"],
                                     float(info["fps"] or 60), sr, ch, crf, seconds, kind, lineup), capture_output=True, text=True)
        return seconds if r.returncode == 0 and dst.exists() and dst.stat().st_size > 0 else 0.0
    finally:
        for f in files.values():
            f.unlink(missing_ok=True)


LINEUP_MAX = 6
OUTRO_LINEUP_SECONDS = 6.0


def make_outro(ffmpeg: Path, ffprobe: Path, last_piece: Path, at: float, dst: Path, team: str, stats: str,
               crf: int = 20, players: list[dict] | None = None) -> float:
    """Render the animated end card (GG, the team, the stats, and the squad line-up when `players` is given:
    [{"name", "photo": Path|None, "kills"}]) over a frame of the last play; its length, 0 when it failed."""
    players = (players or [])[:LINEUP_MAX]
    return _make_card(ffmpeg, ffprobe, last_piece, at, dst, {"big": "GG", "line": team.upper(), "small": stats},
                      crf, OUTRO_LINEUP_SECONDS if players else OUTRO_SECONDS, "outro", players)


def lineup_players(plays: list[tuple[dict, int]]) -> list[dict]:
    """The squad line-up of a montage from its plays [(player card {"name", "photo", "steamid"}, kills)]: each player
    once with their kills added up, most kills first, at most LINEUP_MAX. Pure function; unit-tested."""
    by: dict[str, dict] = {}
    for card, kills in plays:
        if not card:
            continue
        key = str(card.get("steamid") or card.get("name"))
        p = by.setdefault(key, {"name": card.get("name") or "?", "photo": card.get("photo"), "kills": 0})
        p["kills"] += int(kills or 0)
    return sorted(by.values(), key=lambda p: -p["kills"])[:LINEUP_MAX]


def probe_audio(ffprobe: Path, path: Path) -> tuple[int, int]:
    """(sample rate, channels) of a clip's first audio stream; (48000, 2) when ffprobe fails."""
    r = subprocess.run([str(ffprobe), "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate,channels",
                        "-of", "json", str(path)], capture_output=True, text=True)
    try:
        st = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
        return int(st.get("sample_rate") or 48000), int(st.get("channels") or 2)
    except (ValueError, TypeError):
        return 48000, 2


def make_intro(ffmpeg: Path, ffprobe: Path, first_piece: Path, at: float, dst: Path, team: str, sub: str, stats: str,
               crf: int = 20) -> float:
    """Render the animated intro card (the team, what and when, the stats) next to the montage pieces; returns its
    length in seconds (0 when it failed or no font was found: the montage simply starts without one)."""
    return _make_card(ffmpeg, ffprobe, first_piece, at, dst, {"big": team.upper(), "line": sub, "small": stats},
                      crf, INTRO_SECONDS, "intro")


# ---- glue to the app's data --------------------------------------------------------------------------

def clip_context(demo_id: str, highlight_id: int, clip_start_tick: int, clip_end_tick: int | None = None) -> dict:
    """{kills: [seconds from clip start], highlight: dict|None, map: str} for a recorded clip."""
    meta_p = DATA_DIR / "parsed" / demo_id / "meta.json"
    if not meta_p.is_file():   # the match was deleted: cinematic_for_video falls back to the clip's saved "ctx"
        return {"kills": [], "highlight": None, "map": ""}
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    tr = int(meta.get("tickrate", 64))
    for h in db.get_highlights(demo_id):
        if h["id"] == highlight_id:
            ks = [(t - clip_start_tick) / tr for t in h.get("kills", [])
                  if t >= clip_start_tick and (clip_end_tick is None or t <= clip_end_tick)]
            out = {"kills": ks, "highlight": h, "map": meta.get("map", ""),
                   "score": round_score(meta, int(h.get("round") or 0), str(h.get("player") or ""))}
            if h.get("type") == "clutch":
                tick = h.get("clutch_tick") or _clutch_tick(demo_id, meta, h)
                if tick is not None:
                    out["clutch_at"] = round(max(0.0, (tick - clip_start_tick) / tr), 3)
            return out
    return {"kills": [], "highlight": None, "map": meta.get("map", "")}


def _clutch_tick(demo_id: str, meta: dict, h: dict) -> int | None:
    """When the clutch began (the clutcher became the last one alive), for highlights found before the detector
    kept it: replayed from the round's kills."""
    from .highlights import clutch_start
    rnd = next((r for r in meta.get("rounds") or [] if r.get("num") == h.get("round")), None)
    try:
        ev = json.loads((DATA_DIR / "parsed" / demo_id / "events.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not rnd:
        return None
    got = clutch_start([k for k in ev.get("kills", []) if k.get("round") == h.get("round")], rnd.get("teams") or {},
                       str(h.get("player") or ""))
    return got[0] if got else None


def kill_times_for(demo_id: str, highlight_id: int, clip_start_tick: int) -> list[float]:
    return clip_context(demo_id, highlight_id, clip_start_tick)["kills"]


MUSIC_RECENT = DATA_DIR / "music_recent.json"   # track names, the most recently used last


def fresh_tracks(names: list[str], recent: list[str]) -> list[str]:
    """The tracks to pick a random one from: those not heard for the longest time. Never-used tracks first; of
    the used ones, the half used longest ago, so the same song does not come back while others wait.
    Pure function; unit-tested."""
    if not names:
        return []
    last = {n: i for i, n in enumerate(recent)}            # higher = used more recently
    never = [n for n in names if n not in last]
    if never:
        return never
    by_age = sorted(names, key=lambda n: last[n])
    return by_age[:max(1, len(by_age) // 2)]


def _note_used(name: str) -> None:
    try:
        recent = json.loads(MUSIC_RECENT.read_text(encoding="utf-8")) if MUSIC_RECENT.is_file() else []
        recent = [n for n in (recent if isinstance(recent, list) else []) if n != name] + [name]
        MUSIC_RECENT.write_text(json.dumps(recent[-200:]), encoding="utf-8")
    except (OSError, ValueError):
        pass


def pick_music(opts: dict, exclude: set[str] | None = None) -> Path | None:
    """The chosen track, or (none chosen) a random one among those not heard for the longest time; `exclude`
    leaves tracks out (the ones already in this montage)."""
    tracks = [t for t in music_files() if t.name not in (exclude or set())]
    if not tracks:
        return None
    want = str(opts.get("cine_music") or "")
    pick = next((t for t in tracks if t.name == want), None)
    if pick is None:
        try:
            recent = json.loads(MUSIC_RECENT.read_text(encoding="utf-8")) if MUSIC_RECENT.is_file() else []
        except (OSError, ValueError):
            recent = []
        pool = set(fresh_tracks([t.name for t in tracks], recent if isinstance(recent, list) else []))
        pick = random.choice([t for t in tracks if t.name in pool])
    _note_used(pick.name)
    return pick


def fresh_order(exclude: set[str] | None = None) -> list[str]:
    """Every track name (but `exclude`) in the order pick_music would reach for them: never-used ones first (shuffled),
    then the one heard longest ago. Nothing is marked as used. A montage fixes its follow-up songs with this."""
    names = [t.name for t in music_files() if t.name not in (exclude or set())]
    try:
        recent = json.loads(MUSIC_RECENT.read_text(encoding="utf-8")) if MUSIC_RECENT.is_file() else []
    except (OSError, ValueError):
        recent = []
    last = {n: i for i, n in enumerate(recent if isinstance(recent, list) else [])}
    never = [n for n in names if n not in last]
    random.shuffle(never)
    return never + sorted((n for n in names if n in last), key=lambda n: last[n])


def note_used(name: str) -> None:
    _note_used(name)


def video_kills(demo_id: str, video: dict) -> list[float]:
    """Kill times (seconds from the clip start) of a recorded clip, from the match or from what the clip saved."""
    ctx = clip_context(demo_id, video["highlight_id"], int(video["start_tick"]), video.get("end_tick"))
    if ctx["highlight"] is None and video.get("ctx"):
        ctx = video["ctx"]
    return list(ctx["kills"])


def cinematic_for_video(demo_id: str, video: dict, ffmpeg: Path, ffprobe: Path, videos_dir: Path,
                        smooth: bool | None = None, overrides: dict | None = None, progress=None,
                        montage_piece: bool = False, slow: bool = True, lead: float | None = None,
                        low_priority: bool = False, trim: float = 0.0, t_in: str | None = None,
                        t_out: str | None = None) -> dict:
    """Create <name>_cine.mp4 next to the clip and return the updated video entry. montage_piece=True makes
    <name>_mtg.mp4 without music instead (the montage gets one song over everything); the entry is not meant to
    be saved. A montage piece without voice lines comes from (or goes into) the piece cache: the entry then has
    "cine_path" (the cached file, which the caller must not delete) and "cache_hit"."""
    cfg = load_config()
    opts = options(cfg, overrides)
    if smooth is not None:
        opts["cine_smooth"] = smooth
    if not slow:
        opts["_no_slow"] = True   # a montage play that does not land on a drop of the song
    if lead is not None:
        opts["_lead"] = lead      # shifts the play's kills so a kill lands on a drop
    if trim:
        opts["_trim"] = round(float(trim), 3)   # ends the play on a beat
    if t_in:
        opts["_in"] = t_in        # transitions from / to the neighbouring plays of a montage
    if t_out:
        opts["_out"] = t_out
    src = videos_dir / demo_id / video["file"]
    dst = src.with_name(src.stem + ("_mtg.mp4" if montage_piece else "_cine.mp4"))
    ctx = clip_context(demo_id, video["highlight_id"], int(video["start_tick"]), video.get("end_tick"))
    if ctx["highlight"] is None and video.get("ctx"):   # the match was deleted: what was saved with the clip
        ctx = video["ctx"]
    card = player_card(ctx["highlight"]) if opts.get("cine_card") else None
    title = title_for(ctx["highlight"], ctx["map"], with_player=card is None) if ctx["highlight"] else None
    def make(out: Path) -> dict:
        return make_cinematic(ffmpeg, ffprobe, src, out, ctx["kills"], None if montage_piece else pick_music(opts), opts,
                              title=title, fps_hint=float(cfg.get("record_fps", 60)), crf=int(cfg.get("record_crf", 20)),
                              highlight=ctx["highlight"], card=card, progress=progress, music_later=montage_piece,
                              low_priority=low_priority, score=ctx.get("score"), clutch_at=ctx.get("clutch_at"))
    video = dict(video)
    if montage_piece and not opts.get("cine_voice"):   # voice lines are picked at random: nothing to reuse
        shape = {k: x for k, x in opts.items() if k not in PIECE_IGNORES}
        key = piece_key(src, {"kills": ctx["kills"], "opts": shape, "title": title, "card": card, "score": ctx.get("score"),
                              "clutch": [(ctx["highlight"] or {}).get(k) for k in ("type", "n", "round")] + [ctx.get("clutch_at")],
                              "photo": _mtime((card or {}).get("photo")), "font": str(find_font()),
                              "fps": cfg.get("record_fps", 60), "crf": cfg.get("record_crf", 20)})
        path, info, hit = cached_piece(key, make)
        if hit and progress:
            progress("done (already made)", 1.0)
        video.update({"cine": path.name, "cine_path": str(path), "cine_info": info, "cache_hit": hit})
        return video
    video["cine"] = dst.name
    video["cine_info"] = make(dst)
    return video


# ---- piece cache: a montage piece made once is reused by every later montage with the same play and look ----

PIECE_CACHE = DATA_DIR / "cache" / "pieces"
# options a montage piece does not depend on (it has no music; the song goes over the whole montage afterwards)
PIECE_IGNORES = ("cine_music", "cine_music_volume", "cine_music_sync", "cine_voice_pick", "cine_voice_volume")
_inflight: dict[str, threading.Event] = {}   # pieces being made right now, so two jobs never make the same one
_inflight_lock = threading.Lock()


def _mtime(p) -> int | None:
    try:
        return Path(p).stat().st_mtime_ns if p else None
    except OSError:
        return None


_code_sig: str | None = None


def piece_key(src: Path, parts: dict) -> str:
    """Cache key of a montage piece: the recording (name, size, time), everything that shapes the render, and this
    module's code (a change to the effects makes every piece again)."""
    global _code_sig
    if _code_sig is None:
        _code_sig = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()
    st = src.stat()
    blob = json.dumps({"code": _code_sig, "src": src.name, "size": st.st_size, "mtime": st.st_mtime_ns, **parts},
                      sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:24]


def cached_piece(key: str, make) -> tuple[Path, dict, bool]:
    """(file, make_cinematic info, True when it was already there) of the piece `key`; make(path) renders it when
    it is not cached. While another thread is making the same piece, wait for it instead of making it twice."""
    PIECE_CACHE.mkdir(parents=True, exist_ok=True)
    out, meta = PIECE_CACHE / f"{key}.mp4", PIECE_CACHE / f"{key}.json"
    while True:
        with _inflight_lock:
            if out.exists() and meta.exists():
                try:
                    info = json.loads(meta.read_text(encoding="utf-8"))
                    os.utime(out)   # recently used: kept longest by prune_piece_cache
                    return out, info, True
                except (OSError, ValueError):
                    pass
            ev = _inflight.get(key)
            if ev is None:
                ev = _inflight[key] = threading.Event()
                break
        ev.wait()   # the other render finished (or failed: then this thread makes it)
    tmp = PIECE_CACHE / f"{key}.part.mp4"
    try:
        info = make(tmp)
        tmp.replace(out)
        meta.write_text(json.dumps(info), encoding="utf-8")
        return out, info, False
    finally:
        tmp.unlink(missing_ok=True)
        with _inflight_lock:
            _inflight.pop(key, None)
        ev.set()


def prune_piece_cache(max_bytes: int) -> int:
    """Delete the least recently used pieces until the cache fits in max_bytes; returns how many went."""
    if not PIECE_CACHE.is_dir():
        return 0
    files = []
    for p in PIECE_CACHE.glob("*.mp4"):
        if p.name.endswith(".part.mp4") or p.stem in _inflight:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, p))
    total, gone = sum(f[1] for f in files), 0
    for _, size, p in sorted(files):
        if total <= max_bytes:
            break
        try:
            p.unlink()
            p.with_suffix(".json").unlink(missing_ok=True)
            total -= size
            gone += 1
        except OSError:
            pass
    return gone
