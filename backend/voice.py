"""Voice lines for cinematic clips: short sound bites played at the clip start, on kills and after the final kill.

data/voice/<slot>/*.mp3|wav          generic lines of a slot (slot in SLOTS)
data/voice/<slot>/<sub>/*.mp3|wav    lines for one situation: the highlight type (ace, clutch, multikill, knife,
                                     ninja_defuse) for the start/final slots, the kill number (2, 3, 4, 5) for the
                                     kill slot. A matching sub-folder wins when it has files, else a generic line plays.
Files are real caster recordings, added through the UI (upload) or cut from a longer recording with cut().
"""
from __future__ import annotations

import random
import re
import subprocess
from pathlib import Path

from .config import DATA_DIR

VOICE_DIR = DATA_DIR / "voice"
VOICE_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac", ".opus"}
SLOTS = ("start", "kill", "final")   # when a line plays: clip start / every kill but the last / right after the final kill
TYPE_SUBS = ("ace", "clutch", "multikill", "knife", "ninja_defuse")   # sub-folders of start/final (highlight type)
KILL_SUBS = ("2", "3", "4", "5")                                      # sub-folders of kill (the n-th kill of the clip)
SUBS = {"start": TYPE_SUBS, "kill": KILL_SUBS, "final": TYPE_SUBS}
_SUB_RE = re.compile(r"^[a-z0-9_]{1,16}$")


def slot_dir(slot: str, sub: str = "") -> Path:
    if slot not in SLOTS:
        raise ValueError(f"unknown voice slot {slot!r}")
    if sub and not _SUB_RE.match(sub):
        raise ValueError(f"bad voice sub-folder {sub!r}")
    d = VOICE_DIR / slot / sub if sub else VOICE_DIR / slot
    d.mkdir(parents=True, exist_ok=True)
    return d


def files(slot: str, sub: str = "") -> list[Path]:
    """Audio files directly in data/voice/<slot>[/<sub>] (sub-folders are not included)."""
    return sorted(p for p in slot_dir(slot, sub).iterdir() if p.suffix.lower() in VOICE_EXT and p.is_file())


def subs_present(slot: str) -> list[str]:
    return sorted(p.name for p in slot_dir(slot).iterdir() if p.is_dir() and _SUB_RE.match(p.name))


def listing() -> dict:
    """Names are relative to the slot: 'x.mp3' is generic, 'ace/x.mp3' plays only for aces."""
    return {"dir": str(VOICE_DIR), "slots": {s: listing_names(s) for s in SLOTS}, "subs": SUBS}


def split_name(name: str) -> tuple[str, str]:
    """'ace/x.mp3' -> ('ace', 'x.mp3'); 'x.mp3' -> ('', 'x.mp3'). Rejects anything deeper or with '..'."""
    parts = [p for p in str(name).replace("\\", "/").split("/") if p]
    if not parts or len(parts) > 2 or any(p in (".", "..") for p in parts):
        raise ValueError("bad name")
    return (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])


def pick(slot: str, prefer: list[str] | None = None, pinned: list[str] | None = None) -> Path | None:
    """A random line for a slot. `pinned` (slot-relative names such as 'x.mp3' or 'ace/x.mp3') restricts the choice
    to those lines; among them a line from the first preferred sub-folder wins, else any pinned line. Without pins:
    the first preferred sub-folder that has files, else a generic line from the slot root."""
    pins = set(pinned or [])
    if pins:
        avail = {n: slot_dir(slot) / n for n in listing_names(slot) if n in pins}
        for sub in prefer or []:
            hit = [p for n, p in avail.items() if n.startswith(sub + "/")]
            if hit:
                return random.choice(hit)
        return random.choice(list(avail.values())) if avail else None
    for sub in prefer or []:
        try:
            fs = files(slot, sub)
        except ValueError:
            continue
        if fs:
            return random.choice(fs)
    fs = files(slot)
    return random.choice(fs) if fs else None


def listing_names(slot: str) -> list[str]:
    names = [p.name for p in files(slot)]
    for sub in subs_present(slot):
        names += [f"{sub}/{p.name}" for p in files(slot, sub)]
    return names


def parse_pins(value) -> dict[str, list[str]]:
    """cine_voice_pick: JSON {"start": [...], "kill": [...], "final": [...]} (or already a dict); unknown slots dropped."""
    import json
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else {}
        except json.JSONDecodeError:
            value = {}
    if not isinstance(value, dict):
        return {}
    return {s: [str(n) for n in value.get(s) or [] if isinstance(n, str)] for s in SLOTS}


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:48].rstrip("-") or "line")


def cut(ffmpeg: Path, src: Path, start: float, end: float, slot: str, name: str, pad: float = 0.25,
        sub: str = "", fade_out: float = 0.35) -> Path:
    """Cut [start, end] of a longer recording into data/voice/<slot>[/<sub>]/<name>.mp3.
    `end` should be where the voice actually stops (not the last word's end); `pad` is added after it and the
    tail fades over `fade_out` seconds so crowd noise tapers instead of stopping dead."""
    dst = slot_dir(slot, sub) / f"{slug(name)}.mp3"
    a, b = max(0.0, start - 0.1), end + pad
    r = subprocess.run([str(ffmpeg), "-v", "error", "-y", "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", str(src),
                        "-af", f"afade=t=in:st=0:d=0.08,afade=t=out:st={max(0.0, b - a - fade_out):.3f}:d={fade_out:.2f}",
                        "-c:a", "libmp3lame", "-q:a", "3", str(dst)], capture_output=True, text=True)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError("ffmpeg failed: " + r.stderr[-300:])
    return dst

