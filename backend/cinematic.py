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

import json
import math
import os
import random
import subprocess
import sys
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
               "cine_music_sync", "cine_trim", "cine_trim_lead", "cine_fast", "cine_ramp", "cine_impact", "cine_look",
               "cine_fade", "cine_title", "cine_counter", "cine_card", "cine_voice", "cine_voice_volume")
TRIM_MODES = ("off", "cut", "fast")   # dead time between kills: keep / drop / fast-forward

Seg = tuple[float, float, float]   # (start, end, speed) in source seconds; speed 1.0 = normal, 0.35 = slow motion

RAMP_SECONDS = 0.36     # length of a speed ramp (each side)
RAMP_STEPS = 3
SLOW_PRE, SLOW_POST = 0.7, 0.5   # slow-motion window around a kill
TRIM_TAIL = 2.5         # seconds kept after a kill when trimming
TRIM_MIN_CUT = 1.5      # dead time shorter than this is not worth cutting
TITLE_SECONDS = 3.2


def music_files() -> list[Path]:
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(p for p in MUSIC_DIR.iterdir() if p.suffix.lower() in MUSIC_EXT and p.is_file())


def options(cfg: dict | None = None, overrides: dict | None = None) -> dict:
    """Effective cine_* options: config.toml values with (type-coerced) overrides on top."""
    from .config import DEFAULTS
    cfg = cfg if cfg is not None else load_config()
    out = {k: cfg.get(k, DEFAULTS[k]) for k in OPTION_KEYS}
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
    wins: list[list[float]] = []
    for t in sorted(kill_times):
        a, b = max(0.0, t - lead), min(duration, t + tail)
        if a >= b:
            continue
        if wins and a - wins[-1][1] < min_cut:
            wins[-1][1] = max(wins[-1][1], b)
        else:
            wins.append([a, b])
    return [(a, b) for a, b in wins] or [(0.0, duration)]


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
    if photo is None and sid and PLAYERS_DIR.is_dir():
        for ext in PHOTO_EXT:
            cand = PLAYERS_DIR / f"{sid}{ext}"
            if cand.is_file():
                photo = cand
                break
    return {"name": name, "photo": photo, "steamid": sid}


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
                 card: dict | None = None, pad: float = 0.0) -> str:
    """texts: [{"file": Path, "size": int, "x": str, "y": str, "alpha": str|None, "enable": str|None, "font": Path}]
    voices: output-time offsets of the extra audio inputs that follow the clip (and the music, when there is one).
    card: {"idx": input index of the photo or None, "d": avatar px, "x", "y", "initial": Path|None, "font": Path}
    for the circular player card, shown with the title card."""
    total = total_duration(segs) + pad
    parts, labels = [], []
    for i, (a, b, s) in enumerate(segs):
        v = f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS"
        au = f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS"
        if impact:
            ks = [k - a for k in kills if a <= k < b]
            if ks:
                v += "," + _impact(ks, fps).replace("s=WxH", f"s={width}x{height}")
        if abs(s - 1.0) > 1e-3:
            v += f",setpts=PTS/{s:.4f}"
            if smooth and s < 1.0:
                v += f",minterpolate=fps={fps:g}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
            au += "," + _atempo_chain(s)
        parts.append(v + f"[v{i}]")
        parts.append(au + f"[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    if pad > 0:
        parts.append("".join(labels) + f"concat=n={len(segs)}:v=1:a=1[vcc][acc]")
        parts.append(f"[vcc]tpad=stop_mode=clone:stop_duration={pad:.3f}[vc0]")   # hold the last frame
        parts.append(f"[acc]apad=pad_dur={pad:.3f}[ac0]")
    else:
        parts.append("".join(labels) + f"concat=n={len(segs)}:v=1:a=1[vc0][ac0]")
    cur = "[vc0]"
    if look:
        parts.append(f"{cur}eq=contrast=1.06:saturation=1.18:gamma=0.98,vignette=angle=PI/4.6[vl]")
        cur = "[vl]"
    if card:
        parts += _card_chain(card, fps)
        parts.append(f"{cur}[card]overlay=x={card['x']}:y={card['y']}:eof_action=pass:"
                     f"enable='between(t\\,0\\,{TITLE_SECONDS:.2f})'[vcard]")
        cur = "[vcard]"
    post = [_drawtext(t["font"], t["file"], t["size"], t["x"], t["y"], t.get("alpha"), t.get("enable")) for t in texts]
    if fade:
        post.append(f"fade=t=in:st=0:d=0.5,fade=t=out:st={max(0.0, total - 0.7):.3f}:d=0.7")
    post.append(f"fps={fps:g}")   # constant frame rate: speed changes leave irregular timestamps behind
    parts.append(cur + ",".join(post) + "[vc]")
    ag = f"volume={game_vol:.2f}" if has_music else "anull"
    if fade:
        ag += f",afade=t=in:st=0:d=0.4,afade=t=out:st={max(0.0, total - 0.7):.3f}:d=0.7"
    mix = ["[ag]"]
    parts.append(f"[ac0]{ag}[ag]")
    if has_music:
        fade_out = max(0.0, total - 2.0)
        parts.append(f"[1:a]atrim=start=0:end={total:.3f},asetpts=PTS-STARTPTS,volume={music_vol:.2f},"
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
    fading in and out with the title card."""
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
            f"[ring][av]overlay=3:3:format=auto,fade=t=in:st=0:d=0.3:alpha=1,"
            f"fade=t=out:st={TITLE_SECONDS - 0.5:.2f}:d=0.5:alpha=1[card]"]


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
        # lower third: [ring+avatar] NICKNAME / details, with the big label above it
        d = int(h * 0.11)
        ring = d + 6
        cy = h - bottom - ring
        name_px, det_px = int(h * 0.05), int(h * 0.026)
        tx = x0 + ring + int(h * 0.016)
        texts.append({"font": font, "file": tf(card["name"]), "size": name_px, "x": str(tx),
                      "y": str(cy + int(ring * 0.12)), "alpha": env, "enable": en})
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
                choice: list[tuple[Path, float]] | None = None) -> list[tuple[Path, float]]:
    """(file, output time) of the voice lines: a 'start' line at the beginning, a 'kill' line on every kill but
    the last, a 'final' line right after the final kill. Lines from the sub-folder that matches the highlight
    (its type, or the kill number) are preferred; slots without files are silent."""
    out: list[tuple[Path, float]] = []
    prefs = voice_prefs(highlight)
    picked = iter([pth for pth, _ in choice]) if choice is not None else None

    def pick(slot: str, prefer: list[str]) -> Path | None:
        return next(picked, None) if picked is not None else voice.pick(slot, prefer)
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


def make_cinematic(ffmpeg: Path, ffprobe: Path, src: Path, dst: Path, kill_times: list[float], music: Path | None,
                   opts: dict, title: tuple[str, str] | None = None, fps_hint: float | None = None,
                   crf: int = 20, highlight: dict | None = None, card: dict | None = None) -> dict:
    info = probe_video(ffprobe, src)
    duration, width, height = info["duration"], info["width"], info["height"]
    fps = info["fps"] or fps_hint or 60.0
    slow = float(opts["cine_slow"])
    kills = sorted(k for k in kill_times if 0 <= k <= duration)
    slow_kills = kills[-1:] if opts["cine_kills"] == "last" else kills
    mode = opts.get("cine_trim", "off")
    keep = keep_windows(kills, duration, lead=float(opts.get("cine_trim_lead", 2.0))) if mode in ("cut", "fast") \
        else [(0.0, duration)]

    def plan(keep_windows_: list[tuple[float, float]]) -> list[Seg]:
        sg = speed_segments(slow_kills, keep_windows_, slow, ramp=bool(opts.get("cine_ramp")))
        if mode == "fast":
            sg = fill_gaps(sg, duration, float(opts.get("cine_fast", 6.0)))
        return sg or [(0.0, duration, 1.0)]
    segs = plan(keep)
    voices = plan_voices(segs, kills, total_duration(segs), highlight) if opts.get("cine_voice") else []
    pad = 0.0
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
                         card=card_spec, pad=pad)
    args += ["-filter_complex", graph, "-map", "[vc]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", str(crf), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
             "-t", f"{total_out:.3f}", str(dst)]
    t0 = time.time()
    try:
        r = subprocess.run(args, capture_output=True, text=True)
    finally:
        for p in text_files:
            p.unlink(missing_ok=True)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError("ffmpeg failed: " + r.stderr[-800:])
    effects = [k[5:] for k in ("cine_ramp", "cine_impact", "cine_look", "cine_fade", "cine_title", "cine_counter")
               if opts.get(k)]
    if card_spec:
        effects.append("card" + (" (photo)" if card.get("photo") else " (no photo yet)"))
    if mode != "off":
        effects.insert(0, mode)
    if want_text and not font:
        effects.append("no font found: text overlays skipped")
    if voices:
        effects.append(f"{len(voices)} voice line(s)" + (f", held last frame {pad:.1f}s" if pad > 0 else ""))
    return {"seconds": round(total_out, 1), "slow_segments": sum(1 for s in segs if s[2] < 0.999),
            "dead_seconds": round(duration - sum(b - a for a, b in keep), 1), "music": music.name if has_music else None,
            "music_offset": round(offset, 1), "effects": effects, "encode_time": round(time.time() - t0, 1)}


# ---- glue to the app's data --------------------------------------------------------------------------

def clip_context(demo_id: str, highlight_id: int, clip_start_tick: int, clip_end_tick: int | None = None) -> dict:
    """{kills: [seconds from clip start], highlight: dict|None, map: str} for a recorded clip."""
    meta = json.loads((DATA_DIR / "parsed" / demo_id / "meta.json").read_text(encoding="utf-8"))
    tr = int(meta.get("tickrate", 64))
    for h in db.get_highlights(demo_id):
        if h["id"] == highlight_id:
            ks = [(t - clip_start_tick) / tr for t in h.get("kills", [])
                  if t >= clip_start_tick and (clip_end_tick is None or t <= clip_end_tick)]
            return {"kills": ks, "highlight": h, "map": meta.get("map", "")}
    return {"kills": [], "highlight": None, "map": meta.get("map", "")}


def kill_times_for(demo_id: str, highlight_id: int, clip_start_tick: int) -> list[float]:
    return clip_context(demo_id, highlight_id, clip_start_tick)["kills"]


def pick_music(opts: dict) -> Path | None:
    tracks = music_files()
    if not tracks:
        return None
    want = str(opts.get("cine_music") or "")
    for t in tracks:
        if t.name == want:
            return t
    return random.choice(tracks)


def cinematic_for_video(demo_id: str, video: dict, ffmpeg: Path, ffprobe: Path, videos_dir: Path,
                        smooth: bool | None = None, overrides: dict | None = None) -> dict:
    """Create <name>_cine.mp4 next to the clip and return the updated video entry."""
    cfg = load_config()
    opts = options(cfg, overrides)
    if smooth is not None:
        opts["cine_smooth"] = smooth
    src = videos_dir / demo_id / video["file"]
    dst = src.with_name(src.stem + "_cine.mp4")
    ctx = clip_context(demo_id, video["highlight_id"], int(video["start_tick"]), video.get("end_tick"))
    card = player_card(ctx["highlight"]) if opts.get("cine_card") else None
    title = title_for(ctx["highlight"], ctx["map"], with_player=card is None) if ctx["highlight"] else None
    info = make_cinematic(ffmpeg, ffprobe, src, dst, ctx["kills"], pick_music(opts), opts, title=title,
                          fps_hint=float(cfg.get("record_fps", 60)), crf=int(cfg.get("record_crf", 20)),
                          highlight=ctx["highlight"], card=card)
    video = dict(video)
    video["cine"] = dst.name
    video["cine_info"] = info
    return video
