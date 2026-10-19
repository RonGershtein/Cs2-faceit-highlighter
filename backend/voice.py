"""Voice lines for cinematic clips: short sound bites played at the clip start, on kills and after the final kill.

data/voice/<slot>/*.mp3|wav          generic lines of a slot (slot in SLOTS)
data/voice/<slot>/<sub>/*.mp3|wav    lines for one situation: the highlight type (ace, clutch, multikill, knife,
                                     ninja_defuse) for the start/final slots, the kill number (2, 3, 4, 5) for the
                                     kill slot. A matching sub-folder wins when it has files, else a generic line plays.
Files are added through the UI (upload) or generated from text with the Windows voice (System.Speech via PowerShell;
also works from WSL through powershell.exe). The famous caster lines below are offered as presets for the generator.
"""
from __future__ import annotations

import os
import platform
import random
import re
import shutil
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

PRESETS = [
    {"slot": "start", "sub": "", "text": "Welcome... to the major!"},
    {"slot": "start", "sub": "", "text": "Here we go!"},
    {"slot": "start", "sub": "clutch", "text": "He is all alone now. Can he do it?"},
    {"slot": "kill", "sub": "", "text": "Oh, look at the flick of the wrist!"},
    {"slot": "kill", "sub": "", "text": "What a shot!"},
    {"slot": "kill", "sub": "2", "text": "Two down!"},
    {"slot": "kill", "sub": "3", "text": "Three! He is on fire!"},
    {"slot": "kill", "sub": "4", "text": "Four! Can he get the ace?"},
    {"slot": "final", "sub": "", "text": "Are you kidding me?!"},
    {"slot": "final", "sub": "", "text": "Look at the time, look at the positioning, look at the absolute scenes!"},
    {"slot": "final", "sub": "", "text": "Absolute bedlam on the B site!"},
    {"slot": "final", "sub": "", "text": "What is happening?! What is actually happening in this arena?!"},
    {"slot": "final", "sub": "", "text": "I don't believe what just happened!"},
    {"slot": "final", "sub": "ace", "text": "ACE! Five for five! Are you kidding me?!"},
    {"slot": "final", "sub": "clutch", "text": "He's done it! What a clutch!"},
    {"slot": "final", "sub": "knife", "text": "With the knife! Oh, the disrespect!"},
    {"slot": "final", "sub": "ninja_defuse", "text": "Ninja defuse! They never saw him!"},
]


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
    slots = {}
    for s in SLOTS:
        names = [p.name for p in files(s)]
        for sub in subs_present(s):
            names += [f"{sub}/{p.name}" for p in files(s, sub)]
        slots[s] = names
    return {"dir": str(VOICE_DIR), "slots": slots, "subs": SUBS, "tts": tts_available(), "presets": PRESETS}


def split_name(name: str) -> tuple[str, str]:
    """'ace/x.mp3' -> ('ace', 'x.mp3'); 'x.mp3' -> ('', 'x.mp3'). Rejects anything deeper or with '..'."""
    parts = [p for p in str(name).replace("\\", "/").split("/") if p]
    if not parts or len(parts) > 2 or any(p in (".", "..") for p in parts):
        raise ValueError("bad name")
    return (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])


def pick(slot: str, prefer: list[str] | None = None) -> Path | None:
    """A random line: from the first preferred sub-folder that has files, else a generic one."""
    for sub in prefer or []:
        try:
            fs = files(slot, sub)
        except ValueError:
            continue
        if fs:
            return random.choice(fs)
    fs = files(slot)
    return random.choice(fs) if fs else None


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


# ---- text to speech (Windows voice) --------------------------------------------------------------

def _powershell() -> str | None:
    if platform.system() == "Windows":
        return shutil.which("powershell") or shutil.which("powershell.exe")
    return shutil.which("powershell.exe")   # WSL


def tts_available() -> bool:
    return _powershell() is not None


def _win_path(p: Path) -> str:
    if platform.system() == "Windows":
        return str(p)
    r = subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True)
    return r.stdout.strip() or str(p)


def tts(text: str, slot: str, rate: int = 2, sub: str = "") -> Path:
    """Speak `text` into data/voice/<slot>[/<sub>]/<slug>.wav with the first installed English voice (or the default)."""
    ps = _powershell()
    if not ps:
        raise RuntimeError("text-to-speech needs Windows (PowerShell with System.Speech)")
    text = " ".join(str(text).split())
    if not text:
        raise ValueError("empty text")
    dst = slot_dir(slot, sub) / f"{slug(text)}.wav"
    q = lambda s: str(s).replace("'", "''")  # noqa: E731
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$v = $s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'en*' } | Select-Object -First 1; "
        "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) }; "
        f"$s.Rate = {int(rate)}; $s.Volume = 100; "
        f"$s.SetOutputToWaveFile('{q(_win_path(dst))}'); $s.Speak('{q(text)}'); $s.Dispose()"
    )
    r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True,
                       timeout=60, env={**os.environ, "POWERSHELL_TELEMETRY_OPTOUT": "1"})
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size < 100:
        raise RuntimeError("text-to-speech failed: " + (r.stderr or r.stdout)[-400:].strip())
    return dst
