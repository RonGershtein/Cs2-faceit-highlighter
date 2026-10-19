"""Voice lines for cinematic clips: short sound bites played at the clip start, on kills and after the final kill.

data/voice/<slot>/*.mp3|wav  with slot in SLOTS. Files are added through the UI (upload) or generated from text with
the Windows voice (System.Speech via PowerShell; also works from WSL through powershell.exe). The famous caster lines
below are offered as presets for the generator.
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

PRESETS = [
    {"slot": "start", "text": "Welcome... to the major!"},
    {"slot": "start", "text": "Here we go!"},
    {"slot": "kill", "text": "Oh, look at the flick of the wrist!"},
    {"slot": "kill", "text": "What a shot!"},
    {"slot": "final", "text": "Are you kidding me?!"},
    {"slot": "final", "text": "Look at the time, look at the positioning, look at the absolute scenes!"},
    {"slot": "final", "text": "Absolute bedlam on the B site!"},
    {"slot": "final", "text": "What is happening?! What is actually happening in this arena?!"},
    {"slot": "final", "text": "I don't believe what just happened!"},
]


def slot_dir(slot: str) -> Path:
    if slot not in SLOTS:
        raise ValueError(f"unknown voice slot {slot!r}")
    d = VOICE_DIR / slot
    d.mkdir(parents=True, exist_ok=True)
    return d


def files(slot: str) -> list[Path]:
    return sorted(p for p in slot_dir(slot).iterdir() if p.suffix.lower() in VOICE_EXT and p.is_file())


def listing() -> dict:
    return {"dir": str(VOICE_DIR), "slots": {s: [p.name for p in files(s)] for s in SLOTS}, "tts": tts_available(),
            "presets": PRESETS}


def pick(slot: str) -> Path | None:
    fs = files(slot)
    return random.choice(fs) if fs else None


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:48].rstrip("-") or "line")


def cut(ffmpeg: Path, src: Path, start: float, end: float, slot: str, name: str, pad: float = 0.08) -> Path:
    """Cut [start, end] of a longer recording into data/voice/<slot>/<name>.mp3 with a short fade on both ends."""
    dst = slot_dir(slot) / f"{slug(name)}.mp3"
    a, b = max(0.0, start - pad), end + pad
    r = subprocess.run([str(ffmpeg), "-v", "error", "-y", "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", str(src),
                        "-af", f"afade=t=in:st=0:d=0.05,afade=t=out:st={max(0.0, b - a - 0.12):.3f}:d=0.12",
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


def tts(text: str, slot: str, rate: int = 2) -> Path:
    """Speak `text` into data/voice/<slot>/<slug>.wav with the first installed English voice (or the default one)."""
    ps = _powershell()
    if not ps:
        raise RuntimeError("text-to-speech needs Windows (PowerShell with System.Speech)")
    text = " ".join(str(text).split())
    if not text:
        raise ValueError("empty text")
    dst = slot_dir(slot) / f"{slug(text)}.wav"
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
