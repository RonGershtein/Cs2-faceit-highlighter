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
               "cine_impact", "cine_look", "cine_fade", "cine_title", "cine_counter", "cine_card", "cine_voice",
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
        big = f"1v{n} CLUTCH" if n else "CLUTCH"
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
    o = [f"fontfile={_fesc(font)}", f"textfile={_fesc(textfile)}", "expansion=none", f"fontsize={size}",
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
                 music_later: bool = False) -> str:
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
        parts.append(f"{cur}eq=contrast=1.06:saturation=1.18:gamma=0.98,vignette=angle=PI/4.6:dither=0[vl]")
        cur = "[vl]"
    if card:
        parts += _card_chain(card, fps)
        parts.append(f"{cur}[card]overlay=x={card['x']}:y={card['y']}:eof_action=pass[vcard]")
        cur = "[vcard]"
    post = [_drawtext(t["font"], t["file"], t["size"], t["x"], t["y"], t.get("alpha"), t.get("enable")) for t in texts]
    if fade:
        post.append(f"fade=t=in:st=0:d=0.5,fade=t=out:st={max(0.0, total - 0.7):.3f}:d=0.7")
    post.append(f"fps={fps:g}")   # constant frame rate: speed changes leave irregular timestamps behind
    parts.append(cur + ",".join(post) + "[vc]")
    ag = f"volume={game_vol:.2f}" if has_music or music_later else "anull"
    win = "+".join(f"between(t\\,{a - 0.25:.2f}\\,{b + 0.25:.2f})" for a, b in duck) if duck else ""
    if win:   # the game audio drops to half while a voice line plays (0.25 s either side), evaluated per frame
        ag = f"volume='{(game_vol if has_music or music_later else 1.0):.2f}*(1-0.5*min(1\\,{win}))':eval=frame"
    if fade:
        ag += f",afade=t=in:st=0:d=0.4,afade=t=out:st={max(0.0, total - 0.7):.3f}:d=0.7"
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
               counter: bool, font: Path, tmp_base: Path, card: dict | None = None) -> tuple[list[dict], list[Path], dict | None]:
    """Describe the drawtext calls (title card, player card texts, kill counter) and the player card geometry.
    The texts are written to small files next to the output (drawtext's textfile= needs no escaping); the caller
    deletes them after the encode. card: {"name", "photo"} -> returned spec has d/x/y/initial/idx(None)."""
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
    return texts, files, card_spec


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
    return {"seconds": total_duration(segs), "hit": out_time(segs, slow_kills[0]) if slow_kills else None}


def make_cinematic(ffmpeg: Path, ffprobe: Path, src: Path, dst: Path, kill_times: list[float], music: Path | None,
                   opts: dict, title: tuple[str, str] | None = None, fps_hint: float | None = None,
                   crf: int = 20, highlight: dict | None = None, card: dict | None = None,
                   progress=None, music_later: bool = False, low_priority: bool = False) -> dict:
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
    segs = plan(keep)
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
    total_out = total_duration(segs) + pad
    has_music = music is not None and music.exists()
    want_text = opts.get("cine_title") or opts.get("cine_counter") or (opts.get("cine_card") and card)
    font = find_font() if want_text else None
    texts, text_files, card_spec = plan_texts(
        segs, kills, width=width, height=height, title=title if opts.get("cine_title") else None,
        counter=bool(opts.get("cine_counter")), font=font, tmp_base=dst,
        card=card if opts.get("cine_card") else None) if font else ([], [], None)
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
                         music_later=music_later)
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
        raise RuntimeError("ffmpeg failed: " + err[-800:])
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


def music_bed_args(ffmpeg: Path, first: Path, offset: float, others: list[Path], dst: Path, xfade: float = 3.0) -> list[str]:
    """FFmpeg command joining the first song (from `offset`) and the songs after it into one track, each
    crossfading into the next, so a long montage keeps one continuous soundtrack. Pure function; unit-tested."""
    args = [str(ffmpeg), "-y"] + (["-ss", f"{offset:.3f}"] if offset > 0 else []) + ["-i", str(first)]
    for o in others:
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


def intro_args(ffmpeg: Path, src: Path, at: float, dst: Path, texts: dict, font: Path, width: int, height: int,
               fps: float, sample_rate: int, channels: int, crf: int = 20, seconds: float = INTRO_SECONDS) -> list[str]:
    """FFmpeg command for the intro card: one frame of `src` at `at` s (the first kill), blurred, darkened and slowly
    zooming, with the team name (texts["team"] textfile), a line in the accent colour (texts["sub"]) and the stats
    (texts["stats"]) fading in one after the other; fades in from black and out to black. Encoded like the
    cinematic pieces (same size, frame rate, codecs, sample rate) so it joins them without re-encoding.
    Pure function; unit-tested."""
    frames = max(1, round(seconds * fps))
    big, mid, small = round(height * 0.115), round(height * 0.042), round(height * 0.032)
    bar_w = round(width * 0.16)

    def fade(t0: float, d: float = 0.45) -> str:
        return f"if(lt(t,{t0}),0,if(lt(t,{t0 + d}),(t-{t0})/{d},if(gt(t,{seconds - 0.35}),max(0,({seconds}-t)/0.35),1)))"
    team_y = f"(h/2)-{round(height * 0.11)}-6*(1-min(1,max(0,(t-0.25)/0.5)))"
    chain = [
        f"loop=loop={frames}:size=1:start=0", f"fps={fps:g}", f"trim=end_frame={frames}", "setpts=N/FRAME_RATE/TB",
        f"scale={round(width * 1.12)}:-2", "gblur=sigma=14", "eq=brightness=-0.18:saturation=0.55",
        f"zoompan=z='1+0.035*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={fps:g}",
        "vignette=PI/4.2",
        _drawtext(font, texts["team"], big, "(w-text_w)/2", team_y, alpha=fade(0.25)),
        f"drawbox=x=(iw-{bar_w})/2:y=ih/2+{round(height * 0.035)}:w={bar_w}:h={max(2, round(height * 0.004))}:"
        f"color={INTRO_ACCENT}@0.95:t=fill:enable='between(t,0.7,{seconds - 0.3})'",
        _drawtext(font, texts["sub"], mid, "(w-text_w)/2", f"(h/2)+{round(height * 0.07)}", alpha=fade(0.75), color=INTRO_ACCENT),
        _drawtext(font, texts["stats"], small, "(w-text_w)/2", f"(h/2)+{round(height * 0.14)}", alpha=fade(1.2)),
        "fade=t=in:st=0:d=0.4", f"fade=t=out:st={seconds - 0.3:.2f}:d=0.3", "format=yuv420p",
    ]
    layout = "stereo" if channels == 2 else "mono" if channels == 1 else f"{channels}c"
    return [str(ffmpeg), "-y", "-ss", f"{max(0.0, at):.3f}", "-i", str(src), "-f", "lavfi", "-t", f"{seconds:.3f}",
            "-i", f"anullsrc=r={sample_rate}:cl={layout}", "-filter_complex", f"[0:v]{','.join(chain)}[v]",
            "-map", "[v]", "-map", "1:a", "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast",
            "-crf", str(crf), "-pix_fmt", "yuv420p", "-r", f"{fps:g}", "-c:a", "aac", "-b:a", "192k",
            "-ar", str(sample_rate), "-ac", str(channels), "-movflags", "+faststart", "-t", f"{seconds:.3f}", str(dst)]


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
    """Render the intro card next to the montage pieces; returns its length in seconds (0 when it failed or no
    font was found: the montage simply starts without one)."""
    font = find_font()
    if font is None:
        return 0.0
    info = probe_video(ffprobe, first_piece)
    sr, ch = probe_audio(ffprobe, first_piece)
    files = {}
    for k, v in (("team", team.upper()), ("sub", sub), ("stats", stats)):
        f = dst.with_name(f"{dst.stem}_{k}.txt")
        f.write_text(v or " ", encoding="utf-8")
        files[k] = f
    try:
        r = subprocess.run(intro_args(ffmpeg, first_piece, at, dst, files, font, info["width"], info["height"],
                                      float(info["fps"] or 60), sr, ch, crf), capture_output=True, text=True)
        return INTRO_SECONDS if r.returncode == 0 and dst.exists() and dst.stat().st_size > 0 else 0.0
    finally:
        for f in files.values():
            f.unlink(missing_ok=True)


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
            return {"kills": ks, "highlight": h, "map": meta.get("map", "")}
    return {"kills": [], "highlight": None, "map": meta.get("map", "")}


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


def video_kills(demo_id: str, video: dict) -> list[float]:
    """Kill times (seconds from the clip start) of a recorded clip, from the match or from what the clip saved."""
    ctx = clip_context(demo_id, video["highlight_id"], int(video["start_tick"]), video.get("end_tick"))
    if ctx["highlight"] is None and video.get("ctx"):
        ctx = video["ctx"]
    return list(ctx["kills"])


def cinematic_for_video(demo_id: str, video: dict, ffmpeg: Path, ffprobe: Path, videos_dir: Path,
                        smooth: bool | None = None, overrides: dict | None = None, progress=None,
                        montage_piece: bool = False, slow: bool = True, lead: float | None = None,
                        low_priority: bool = False) -> dict:
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
                              low_priority=low_priority)
    video = dict(video)
    if montage_piece and not opts.get("cine_voice"):   # voice lines are picked at random: nothing to reuse
        shape = {k: x for k, x in opts.items() if k not in PIECE_IGNORES}
        key = piece_key(src, {"kills": ctx["kills"], "opts": shape, "title": title, "card": card,
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
