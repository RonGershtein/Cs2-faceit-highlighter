"""What the app knows about a song: its beats, the first beat of every bar, its drops, the parts a montage plays and
how loud it is. One decode, cached per file (data/cache/songs), so planning a montage reads it instead of decoding
every song again. Drops the user placed by hand (the song editor in Settings) replace the detected ones.

A drop is where the music comes in hard after a build-up: the loudness AND the bass jump. It is measured on the beat
grid, so it lands exactly on a beat (and, when it can, on the first beat of a bar), not to the nearest half second.
"""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
from pathlib import Path

from .config import DATA_DIR

ANALYSIS_VERSION = 9
SONG_CACHE = DATA_DIR / "cache" / "songs"
MARKS_JSON = DATA_DIR / "music_marks.json"   # {song file name: {"drops": [seconds, ...]}} placed by hand

SR, HOP, NFFT = 11025, 256, 2048
FPS = SR / HOP              # analysis frames per second (about 43)
XFADE = 3.0                 # seconds two pieces of the soundtrack crossfade (record.py uses the same)
WAVE_STEP = 0.1             # seconds per point of the waveform the song editor draws

DROP_AFTER, DROP_BEFORE = 6.0, 3.0   # a drop: the next DROP_AFTER s against the DROP_BEFORE s before it
DROP_MIN_GAP = 8.0
DROP_SHARE = 0.3            # a drop rises at least this share of the song's biggest one
BIG_DROP = 0.6              # strength from which a drop gets the build-up effects (muffle, riser, gap)
TARGET_DB = -11.0           # loudness (RMS of the loud parts, dBFS) every song is brought to
GAIN_MIN, GAIN_MAX = -9.0, 6.0


# ---- pure analysis (unit-tested) ------------------------------------------------------------------------------

def envelopes(x, sr: int = SR, hop: int = HOP, n_fft: int = NFFT) -> dict:
    """Per analysis frame of the mono signal x (numpy float array): "onset" (spectral flux: how much new sound
    starts), "rms" (loudness) and "bass" (energy from 30 to 150 Hz: the kick and the bass line)."""
    import numpy as np
    if len(x) < n_fft * 2:
        return {"onset": [], "rms": [], "bass": []}
    n = 1 + (len(x) - n_fft) // hop
    frames = np.lib.stride_tricks.as_strided(x, shape=(n, n_fft), strides=(x.strides[0] * hop, x.strides[0]))
    f = np.fft.rfftfreq(n_fft, 1.0 / sr)
    low = (f >= 30) & (f <= 150)
    win = np.hanning(n_fft).astype(np.float32)
    flux, bass = np.zeros(n), np.zeros(n)
    prev = None
    # a few thousand frames at a time: the whole spectrum of a five-minute song at once would be hundreds of MB
    for a in range(0, n, 512):
        spec = np.abs(np.fft.rfft(frames[a:a + 512] * win, axis=1)).astype(np.float32)
        mag = np.log1p(10.0 * spec)
        both = mag if prev is None else np.vstack([prev, mag])
        d = np.maximum(0.0, np.diff(both, axis=0)).sum(axis=1)
        flux[a + (1 if prev is None else 0):a + len(mag)] = d
        bass[a:a + len(mag)] = spec[:, low].sum(axis=1)
        prev = mag[-1:]
    return {"onset": flux, "rms": window_rms(x, n_fft, hop, n), "bass": bass}


def window_rms(x, w: int, h: int, n: int | None = None):
    """RMS of the windows of w samples every h samples, from a running sum of the squares (no copy per window)."""
    import numpy as np
    cs = np.concatenate([[0.0], np.cumsum(np.square(x, dtype=np.float64))])
    n = n if n is not None else 1 + (len(x) - w) // h
    starts = np.arange(n) * h
    return np.sqrt(np.maximum(0.0, cs[starts + w] - cs[starts]) / w)


def refine_beats(x, beats: list[float], sr: int = SR, reach: float = 0.1) -> list[float]:
    """Each beat moved onto the exact start of its sound: the steepest rise of the loudness (20 ms windows) within
    `reach` seconds of it. The beat tracker's frames are 186 ms long, so it only knows roughly where a beat is; a
    kill has to land on the hit itself. A beat with no clear hit near it stays where it was. Pure function."""
    import numpy as np
    if not beats or len(x) < sr:
        return list(beats)
    # 20 ms windows: long enough that a low bass note does not flicker in them, short enough to find the hit
    w, h = max(8, int(sr * 0.02)), max(4, int(sr * 0.002))
    n = 1 + (len(x) - w) // h
    e = np.log1p(1000.0 * window_rms(x, w, h, n) ** 2)
    d = np.maximum(0.0, np.diff(e, prepend=e[0]))
    d = np.convolve(d, np.ones(3) / 3, mode="same")
    out = []
    for b in beats:
        i0, i1 = max(0, int((b - reach) * sr / h)), min(n, int((b + reach) * sr / h) + 1)
        seg = d[i0:i1]
        if len(seg) < 3:
            out.append(b)
            continue
        k = int(np.argmax(seg))
        clear = seg[k] > 3.0 * (float(np.median(seg)) + 1e-9)
        out.append(((i0 + k) * h + w / 2) / sr if clear else b)
    # a beat that jumped to some other sound (a quiet intro without drums) goes back in line with its neighbours:
    # the shift its neighbours got, applied to it
    moved = np.array(out) - np.array(beats)
    fixed = []
    for i, b in enumerate(beats):
        near = moved[max(0, i - 4):i + 5]
        local = float(np.median(near))
        fixed.append(round(out[i] if len(near) < 5 or abs(moved[i] - local) <= 0.03 else b + local, 4))
    return fixed


def rise_scores(times: list[float], env, fps: float = FPS, after: float = DROP_AFTER, before: float = DROP_BEFORE) -> list[float]:
    """For each time: how much louder the next `after` seconds are than the `before` seconds before it, in units of
    the envelope's typical loud level (80th percentile). Pure function."""
    import numpy as np
    e = np.asarray(env, dtype=np.float64)
    if not len(e):
        return [0.0] * len(times)
    ref = float(np.percentile(e, 80)) or 1.0
    cs = np.concatenate([[0.0], np.cumsum(e)])
    na, nb = max(1, int(round(after * fps))), max(1, int(round(before * fps)))
    out = []
    for t in times:
        # frame i covers the NFFT samples from i * HOP on: the frame centred on t is the first one after it
        i = min(len(e) - 1, max(0, int(round(t * fps - NFFT / 2 / HOP))))
        a = (cs[min(len(e), i + na)] - cs[i]) / max(1, min(len(e), i + na) - i)
        j = max(0, i - nb)
        b = (cs[i] - cs[j]) / (i - j) if i > j else 0.0
        out.append(float((a - b) / ref))
    return out


def detect_drops(cands: list[float], rms_rise: list[float], bass_rise: list[float], duration: float,
                 min_gap: float = DROP_MIN_GAP, share: float = DROP_SHARE) -> list[tuple[float, float]]:
    """(time, score) of the song's drops among candidate times (its beats): the loudness and the bass rising
    together score highest; the best ones at least `min_gap` apart, down to `share` of the biggest, at most one per
    20 s of song (3 at least). Pure function; unit-tested."""
    if not cands:
        return []
    score = [0.45 * r + 0.55 * b for r, b in zip(rms_rise, bass_rise)]
    ranked = sorted(range(len(cands)), key=lambda i: -score[i])
    top = score[ranked[0]]
    if top <= 0.05:
        return []
    cap = max(3, int(duration / 20.0))
    picked: list[int] = []
    for i in ranked:
        if score[i] < share * top or len(picked) >= cap:
            break
        if cands[i] < 1.0 or cands[i] > duration - 4.0:
            continue
        if all(abs(cands[i] - cands[j]) >= min_gap for j in picked):
            picked.append(i)
    return sorted((round(cands[i], 3), round(score[i], 4)) for i in picked)


def localize_drops(drops: list[tuple[float, float]], beats: list[float], rms, bass, fps: float = FPS,
                   reach: float = 2.1) -> list[tuple[float, float]]:
    """Each drop moved onto the beat where the music really comes in: of the beats within `reach` beats of it, the
    one with the sharpest jump (the next second against the second before). The long windows that find a drop rise
    almost as much one beat early or late. Pure function; unit-tested."""
    if len(beats) < 3 or not drops:
        return drops
    period = (beats[-1] - beats[0]) / (len(beats) - 1)
    out = []
    for t, s in drops:
        near = [b for b in beats if abs(b - t) <= reach * period]
        if not near:
            out.append((t, s))
            continue
        r = rise_scores(near, rms, fps, after=1.0, before=1.0)
        q = rise_scores(near, bass, fps, after=1.0, before=1.0)
        k = max(range(len(near)), key=lambda i: 0.45 * r[i] + 0.55 * q[i])
        out.append((round(near[k], 4), s))
    return out


def downbeat_phase(beats: list[float], drops: list[tuple[float, float]], bass_at_beats: list[float]) -> int:
    """Which beat of every four starts a bar (0-3): the drops vote (a drop comes in on the one), weighted by how
    big they are; the bass on the beats decides when there are none. Pure function; unit-tested."""
    if len(beats) < 4:
        return 0
    votes = [0.0] * 4
    for t, s in drops:
        i = min(range(len(beats)), key=lambda k: abs(beats[k] - t))
        votes[i % 4] += max(0.0, s)
    if max(votes) > 0:
        return max(range(4), key=lambda p: votes[p])
    sums = [sum(bass_at_beats[p::4]) / max(1, len(bass_at_beats[p::4])) for p in range(4)]
    return max(range(4), key=lambda p: sums[p])


def snap_drops(drops: list[tuple[float, float]], beats: list[float], downbeats: list[float]) -> list[tuple[float, float]]:
    """Every drop on the grid: onto the first beat of a bar when one is within a beat of it, else onto its nearest
    beat. Two that meet keep the stronger. Pure function; unit-tested."""
    import bisect
    if not beats:
        return drops
    period = (beats[-1] - beats[0]) / max(1, len(beats) - 1)
    out: dict[float, float] = {}
    for t, s in drops:
        to = None
        if downbeats:
            j = bisect.bisect_left(downbeats, t)
            near = [downbeats[k] for k in (j - 1, j) if 0 <= k < len(downbeats)]
            d = min(near, key=lambda b: abs(b - t))
            if abs(d - t) <= period * 1.05:
                to = d
        if to is None:
            j = bisect.bisect_left(beats, t)
            to = min((beats[k] for k in (j - 1, j) if 0 <= k < len(beats)), key=lambda b: abs(b - t))
        if out.get(to, -1e9) < s:
            out[to] = s
    return sorted(out.items())


def snap_pieces(pieces: list[tuple[float, float]], downbeats: list[float], duration: float,
                xfade: float = XFADE, drops: list[float] | None = None) -> list[tuple[float, float]]:
    """The montage pieces of a song moved onto its bars: each piece ends on the first beat of a bar, and starts
    `xfade` seconds before one, so when the crossfade into it is over it is exactly on its bar while the piece
    before has just finished its own. A piece that starts just before a drop starts `xfade` before the drop
    instead: the soundtrack comes in on the drop (not with the drop lost in the middle of the crossfade).
    Pure function; unit-tested."""
    if not downbeats or not pieces:
        return pieces

    def nearest(t: float) -> float:
        return min(downbeats, key=lambda d: abs(d - t))
    out = []
    for k, (a, b) in enumerate(pieces):
        on_drop = [d for d in (drops or []) if a - 1.0 <= d <= a + xfade + 4.0 and d - xfade >= 0]
        if a <= 0.01:
            a2 = a
        elif on_drop:
            a2 = round(on_drop[0] - xfade, 3)
        else:
            a2 = max(0.0, nearest(a + xfade) - xfade)
        b2 = nearest(b)
        if b2 > duration or b2 - a2 < max(4.0, xfade + 1.0):
            b2 = b
        if abs(a2 - a) > 2.5:
            a2 = a
        if abs(b2 - b) > 2.5:
            b2 = b
        out.append((round(a2, 3), round(b2, 3)))
    return out


def beat_grid(beats: list[float]) -> list[float]:
    """The times a kill may be put on: the beats, and the half beats too when the beat is slow (a half-time
    drum-and-bass beat, or a ballad), so a kill never has to move far. Pure function; unit-tested."""
    if len(beats) < 2:
        return list(beats)
    period = (beats[-1] - beats[0]) / (len(beats) - 1)
    if period <= 0.6:
        return list(beats)
    out = []
    for a, b in zip(beats, beats[1:]):
        out += [a, round((a + b) / 2, 3)]
    return out + [beats[-1]]


def loud_db(curve: list[float]) -> float:
    """Loudness of the loud parts of a song: the RMS (dBFS) of its louder half. Pure function."""
    vals = sorted(v for v in curve if v > 0)
    if not vals:
        return TARGET_DB
    top = vals[len(vals) // 2:]
    rms = math.sqrt(sum(v * v for v in top) / len(top))
    return 20 * math.log10(max(rms, 1e-6))


def song_gain(curve: list[float]) -> float:
    """dB that bring a song to TARGET_DB (a quiet master up, a loud one down), within GAIN_MIN..GAIN_MAX."""
    return round(min(GAIN_MAX, max(GAIN_MIN, TARGET_DB - loud_db(curve))), 2)


def punch(bass_ratio: float, drop_power: float) -> float:
    """How hard a song hits, 0..1, for picking montage songs when none is chosen: a heavy low end (kick and bass
    against everything else) and a big jump into its drop. Tuned on montage songs (phonk and EDM score 0.6-0.9,
    drum and bass that is loud all the way through 0.4-0.55, a cheerful fan chant 0.22). Pure function; unit-tested."""
    z = (bass_ratio - 2.05) / 0.2 + (drop_power - 0.72) / 0.15
    return round(1.0 / (1.0 + math.exp(-z / 1.5)), 3)


def strengths(scores: list[float]) -> list[float]:
    top = max(scores, default=0.0) or 1.0
    return [round(max(0.0, s) / top, 3) for s in scores]


# ---- decoding, caching, marks --------------------------------------------------------------------------------

def decode(ffmpeg: Path, path: Path, sr: int = SR):
    import numpy as np
    r = subprocess.run([str(ffmpeg), "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                       capture_output=True)
    if r.returncode != 0 or len(r.stdout) < 4 * NFFT:
        return None
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def analyze_signal(x, sr: int = SR) -> dict:
    """Everything about a decoded song (mono float array at `sr`). Pure function; unit-tested with a made-up song."""
    import numpy as np
    from . import cinematic as cine
    duration = len(x) / sr
    n = int(sr * 0.5)
    curve = window_rms(x, n, n, len(x) // n).tolist() if len(x) >= n else []
    m = int(sr * WAVE_STEP)
    wave = window_rms(x, m, m, len(x) // m) if len(x) >= m else np.zeros(0)
    env = envelopes(x, sr)
    fps = sr / HOP
    beats = cine.track_beats(list(env["onset"]), fps) if len(env["onset"]) else []
    # a frame's time is its middle, not its start (the tracker counts frames from their first sample), then each beat
    # onto the hit itself
    beats = refine_beats(x, [round(b + NFFT / 2 / sr, 4) for b in beats], sr)
    cands = beats if len(beats) >= 8 else [i * 0.5 for i in range(int(duration / 0.5))]
    rr, br = rise_scores(cands, env["rms"], fps), rise_scores(cands, env["bass"], fps)
    found = localize_drops(detect_drops(cands, rr, br, duration), beats, env["rms"], env["bass"], fps)
    rms_a, bass_a = np.asarray(env["rms"]), np.asarray(env["bass"])
    loud = rms_a >= np.percentile(rms_a, 50) if len(rms_a) else rms_a
    bass_ratio = float(bass_a[loud].mean() / (rms_a[loud].mean() * 1000 + 1e-9)) if len(rms_a) else 0.0
    drop_power = max((sc for _, sc in found), default=0.0)
    bass = np.asarray(env["bass"])
    bass_at = [float(bass[min(len(bass) - 1, int(b * fps))]) for b in beats] if len(bass) else []
    phase = downbeat_phase(beats, found, bass_at)
    downbeats = beats[phase::4] if len(beats) >= 8 else []
    drops = snap_drops(found, beats, downbeats)
    pieces = snap_pieces(cine.song_pieces(curve), downbeats, duration, drops=[t for t, _ in drops])
    period = (beats[-1] - beats[0]) / (len(beats) - 1) if len(beats) >= 2 else 0.5
    peak = float(wave.max()) if len(wave) else 1.0
    return {"version": ANALYSIS_VERSION, "duration": round(duration, 3),
            "bpm": round(60.0 / period, 1) if period else 0.0, "beat": round(period, 4),
            "beats": beats, "downbeats": downbeats,
            "auto_drops": [t for t, _ in drops], "auto_strengths": strengths([s for _, s in drops]),
            "pieces": pieces, "curve": [round(v, 5) for v in curve], "gain_db": song_gain(curve),
            "bass_ratio": round(bass_ratio, 3), "drop_power": round(drop_power, 3),
            "punch": punch(bass_ratio, drop_power),
            "wave": [round(float(v) / (peak or 1.0), 3) for v in wave]}


def _key(path: Path) -> str:
    st = path.stat()
    return hashlib.sha1(f"{path.name}|{st.st_size}|{st.st_mtime_ns}|{ANALYSIS_VERSION}".encode("utf-8")).hexdigest()[:20]


def _read_marks() -> dict:
    try:
        m = json.loads(MARKS_JSON.read_text(encoding="utf-8")) if MARKS_JSON.is_file() else {}
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def marks_for(name: str) -> list[float] | None:
    got = (_read_marks().get(name) or {}).get("drops")
    return sorted(float(t) for t in got) if isinstance(got, list) else None


def set_marks(name: str, drops: list[float] | None) -> None:
    """Drops placed by hand for a song (None: back to the detected ones)."""
    m = _read_marks()
    entry = dict(m.get(name) or {})
    if drops is None:
        entry.pop("drops", None)
    else:
        entry["drops"] = sorted(round(float(t), 3) for t in drops if t >= 0)
    _write_marks(m, name, entry)


def in_random(name: str) -> bool:
    """Whether a song may be picked when a video has no song chosen (the song editor's switch)."""
    return (_read_marks().get(name) or {}).get("random", True) is not False


def set_in_random(name: str, on: bool) -> None:
    m = _read_marks()
    entry = dict(m.get(name) or {})
    if on:
        entry.pop("random", None)
    else:
        entry["random"] = False
    _write_marks(m, name, entry)


def _write_marks(m: dict, name: str, entry: dict) -> None:
    if entry:
        m[name] = entry
    else:
        m.pop(name, None)
    MARKS_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = MARKS_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=1), encoding="utf-8")
    tmp.replace(MARKS_JSON)


def user_strengths(drops: list[float], info: dict) -> list[float]:
    """Strengths of hand-placed drops: the detected strength of a detected drop at the same place, else how much
    the song rises there, relative to the biggest detected drop (a drop the user insists on is at least 0.6)."""
    auto = dict(zip(info.get("auto_drops") or [], info.get("auto_strengths") or []))
    out = []
    for t in drops:
        near = [s for a, s in auto.items() if abs(a - t) <= 0.3]
        out.append(max(near) if near else BIG_DROP)
    return out


def analyze(ffmpeg: Path, path: Path) -> dict | None:
    """The analysis of a song file (cached), with the hand-placed drops (if any) as "drops"/"strengths" and whether
    they are ("edited"). None when the file cannot be decoded."""
    path = Path(path)
    if not path.is_file():
        return None
    SONG_CACHE.mkdir(parents=True, exist_ok=True)
    cp = SONG_CACHE / f"{_key(path)}.json"
    info = None
    if cp.is_file():
        try:
            info = json.loads(cp.read_text(encoding="utf-8"))
            if info.get("version") != ANALYSIS_VERSION:
                info = None
        except (OSError, ValueError):
            info = None
    if info is None:
        x = decode(ffmpeg, path)
        if x is None:
            return None
        info = analyze_signal(x)
        tmp = cp.with_suffix(".tmp")
        tmp.write_text(json.dumps(info), encoding="utf-8")
        tmp.replace(cp)
    info["random"] = in_random(path.name)
    info.setdefault("punch", 0.5)
    mine = marks_for(path.name)
    if mine is not None:
        info.update({"drops": mine, "strengths": user_strengths(mine, info), "edited": True})
    else:
        info.update({"drops": list(info["auto_drops"]), "strengths": list(info["auto_strengths"]), "edited": False})
    return info


SILENCE = 0.03      # of the song's loudest moment (waveform): below this is silence, not quiet music


def silence_edges(wave: list[float], duration: float, step: float = WAVE_STEP) -> tuple[float, float] | None:
    """(start, end) of a song without the silence before and after it (a downloaded video's black start or minutes of
    nothing at the end), keeping 0.15 s before the first sound and 0.5 s after the last so a fade-out stays whole.
    None when there is nothing worth cutting (under 0.8 s at the start and 1.5 s at the end). Quiet music, a soft
    intro, is never cut. Pure function; unit-tested."""
    on = [i for i, v in enumerate(wave) if v > SILENCE]
    if not on:
        return None
    a = max(0.0, on[0] * step - 0.15)
    b = min(duration, (on[-1] + 1) * step + 0.5)
    if a < 0.8 and duration - b < 1.5:
        return None
    return round(a, 2), round(b, 2)


def trim_silence(ffmpeg: Path, path: Path) -> tuple[float, float] | None:
    """Cut the silence off both ends of a song file in place (stream copy, no quality loss); returns the seconds cut
    (start, end), or None when there was nothing to cut or it could not be done (the file is then left as it was)."""
    info = analyze(ffmpeg, path)
    edges = silence_edges(info["wave"], info["duration"]) if info else None
    if not edges:
        return None
    a, b = edges
    tmp = path.with_name(path.stem + ".trimming" + path.suffix)
    r = subprocess.run([str(ffmpeg), "-v", "error", "-y", "-ss", f"{a:.2f}", "-i", str(path), "-t", f"{b - a:.2f}",
                        "-c", "copy", "-map_metadata", "0", str(tmp)], capture_output=True)
    if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 0.5 * path.stat().st_size * (b - a) / info["duration"]:
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(path)
    analyze(ffmpeg, path)                  # the trimmed file is a new file: analysed (and cached) again
    return round(a, 2), round(info["duration"] - b, 2)


def main_drop(info: dict, hit: float, clip_len: float) -> float | None:
    """The drop a single clip starts its song on: the strongest one that leaves `hit` seconds of song before it and
    lasts to the end of the clip. Pure function; unit-tested."""
    dur = float(info.get("duration") or 0)
    fits = [(s, d) for d, s in zip(info.get("drops") or [], info.get("strengths") or [])
            if d >= hit and d - hit + clip_len <= dur + 0.5]
    if not fits:
        fits = [(s, d) for d, s in zip(info.get("drops") or [], info.get("strengths") or []) if d >= hit]
    return max(fits)[1] if fits else None


def forget(name: str) -> None:
    """A song was removed: its hand-placed drops and its switch go too (the cache entry ages out with the file's key)."""
    m = _read_marks()
    if name in m:
        _write_marks(m, name, {})


def rank_for_random(names: list[str], punches: dict[str, float], recent: list[str]) -> list[str]:
    """The songs (all allowed in random picks) in the order to use them when none is chosen: the hardest-hitting
    first, a song heard recently pushed back (0.7 of punch for the one heard last, half that for the one before,
    and so on), so the strong songs take turns instead of one always opening. Pure function; unit-tested."""
    back = {n: k for k, n in enumerate(reversed(recent))}   # 0 = heard last

    def key(n: str) -> float:
        return punches.get(n, 0.5) - (0.7 * 0.5 ** back[n] if n in back else 0.0)
    return sorted(names, key=lambda n: -key(n))
