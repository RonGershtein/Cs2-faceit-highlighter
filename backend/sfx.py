"""Sound effects for the cinematic edits: a hit on the slow-motion kills, a whoosh into slow motion and a riser into
the drop. Built-in ones are made here (synthesised, so there is nothing to download and no licence to worry
about); a file the user drops into a slot (Settings) replaces the built-in one: data/sfx/<slot>.<ext>.
"""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

from .config import DATA_DIR

SFX_DIR = DATA_DIR / "sfx"
BUILTIN_VERSION = 1
SLOTS = {   # slot: (label, what it does)
    "impact": ("Kill hit", "a deep hit on every slow-motion kill"),
    "whoosh": ("Whoosh", "rushes into slow motion"),
    "riser": ("Riser", "builds up into a drop the kill lands on"),
}
EXT = (".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".opus")
RATE = 48000
# where the effect's moment is in its file: the hit's attack at 0, the whoosh and the riser peak at their very end
WHOOSH_SECONDS, RISER_SECONDS = 0.55, 2.4


def _write(path: Path, left: list[float], right: list[float] | None = None) -> None:
    right = right or left
    peak = max(1e-6, max(max(abs(v) for v in left), max(abs(v) for v in right)))
    g = 0.89 / peak
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<hh", int(l * g * 32767), int(r * g * 32767)) for l, r in zip(left, right)))
    tmp.replace(path)


def _noise(n: int, seed: int) -> list[float]:
    import random
    rnd = random.Random(seed)
    return [rnd.uniform(-1.0, 1.0) for _ in range(n)]


def make_impact(seconds: float = 1.3) -> list[float]:
    """A sub drop with a punchy click: a sine falling from 110 to 38 Hz that dies away, plus a short filtered
    noise burst for the attack. Pure function."""
    n = int(RATE * seconds)
    out, phase = [], 0.0
    noise = _noise(int(RATE * 0.06), 7)
    lp = 0.0
    for i in range(n):
        t = i / RATE
        f = 38 + 72 * math.exp(-t / 0.09)
        phase += 2 * math.pi * f / RATE
        body = math.sin(phase) * math.exp(-t / 0.38) * min(1.0, t / 0.002)
        click = 0.0
        if i < len(noise):
            lp += 0.25 * (noise[i] - lp)            # a dull thump, not a hiss
            click = lp * math.exp(-t / 0.012) * 1.6
        out.append(0.95 * math.tanh(1.6 * (body + click)))
    return out


def _sweep_noise(n: int, f0: float, f1: float, env, seed: int) -> list[float]:
    """Noise through a band-pass whose centre glides from f0 to f1 (a state-variable filter), shaped by env(t)."""
    noise = _noise(n, seed)
    low = band = 0.0
    out = []
    for i in range(n):
        x = i / max(1, n - 1)
        fc = f0 * (f1 / f0) ** x
        f = 2 * math.sin(math.pi * min(0.45, fc / RATE))
        q = 0.9
        high = noise[i] - low - q * band
        band += f * high
        low += f * band
        out.append(band * env(i / RATE, x))
    return out


def make_whoosh(seconds: float = WHOOSH_SECONDS) -> tuple[list[float], list[float]]:
    """Air rushing past: band-passed noise sweeping up, swelling to the end where slow motion starts, a little
    wider in stereo. Pure function."""
    n = int(RATE * seconds)
    env = lambda t, x: (x ** 2.2) * (1 - max(0.0, x - 0.93) / 0.07)   # noqa: E731
    left = _sweep_noise(n, 300, 4200, env, 11)
    right = _sweep_noise(n, 320, 4000, env, 12)
    return left, right


def make_riser(seconds: float = RISER_SECONDS) -> tuple[list[float], list[float]]:
    """A build-up: noise sweeping up with a rising tone under it, getting louder to the very end (the drop).
    Pure function."""
    n = int(RATE * seconds)
    env = lambda t, x: (x ** 1.8)   # noqa: E731
    left = _sweep_noise(n, 200, 7000, env, 21)
    right = _sweep_noise(n, 220, 6600, env, 22)
    phase = 0.0
    for i in range(n):
        x = i / max(1, n - 1)
        phase += 2 * math.pi * (180 * (6.0 ** x)) / RATE
        tone = 0.35 * math.sin(phase) * x ** 2
        left[i] += tone
        right[i] += tone
    return left, right


def _builtin(slot: str) -> Path:
    p = SFX_DIR / "builtin" / f"{slot}_v{BUILTIN_VERSION}.wav"
    if not p.is_file():
        if slot == "impact":
            _write(p, make_impact())
        elif slot == "whoosh":
            _write(p, *make_whoosh())
        else:
            _write(p, *make_riser())
    return p


def user_file(slot: str) -> Path | None:
    if not SFX_DIR.is_dir():
        return None
    for p in sorted(SFX_DIR.iterdir()):
        if p.is_file() and p.stem == slot and p.suffix.lower() in EXT:
            return p
    return None


def file_for(slot: str) -> Path:
    """The effect to use for a slot: the user's file, else the built-in one (made on first use)."""
    if slot not in SLOTS:
        raise ValueError(f"unknown sound effect slot {slot}")
    return user_file(slot) or _builtin(slot)


def signature() -> list:
    """What the effects are right now (for the piece cache: replacing one makes the pieces that use it again)."""
    out = []
    for slot in SLOTS:
        p = user_file(slot)
        try:
            out.append([slot, p.name, p.stat().st_mtime_ns] if p else [slot, "builtin", BUILTIN_VERSION])
        except OSError:
            out.append([slot, "?"])
    return out


def listing() -> dict:
    return {"slots": [{"slot": s, "label": lab, "what": what, "file": (user_file(s).name if user_file(s) else None)}
                      for s, (lab, what) in SLOTS.items()]}


def save(slot: str, name: str, data: bytes) -> str:
    """Store an uploaded file as the slot's effect (the one it replaces is removed)."""
    if slot not in SLOTS:
        raise ValueError("unknown slot")
    ext = Path(name).suffix.lower()
    if ext not in EXT:
        raise ValueError("unsupported audio format")
    SFX_DIR.mkdir(parents=True, exist_ok=True)
    old = user_file(slot)
    dst = SFX_DIR / f"{slot}{ext}"
    tmp = SFX_DIR / f"{slot}.upload"
    tmp.write_bytes(data)
    if old and old != dst:
        old.unlink(missing_ok=True)
    tmp.replace(dst)
    return dst.name


def reset(slot: str) -> None:
    p = user_file(slot)
    if p:
        p.unlink(missing_ok=True)
