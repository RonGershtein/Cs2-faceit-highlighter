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
    return {"dir": str(VOICE_DIR), "slots": {s: listing_names(s) for s in SLOTS}, "subs": SUBS, "tts": tts_available(),
            "tts_voices": TTS_VOICES, "presets": PRESETS, "crowd_bed": CROWD_BED.is_file()}


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


# ---- text to speech ------------------------------------------------------------------------------
# Neural voices through edge-tts (Microsoft Edge's online voices; needs internet) sound far more natural than the
# built-in Windows voice, which stays as the offline fallback.

TTS_VOICES = [
    {"id": "en-US-ChristopherNeural", "label": "Christopher (deep, energetic)"},
    {"id": "en-US-GuyNeural", "label": "Guy (broadcast)"},
    {"id": "en-US-AndrewNeural", "label": "Andrew (warm)"},
    {"id": "en-US-BrianNeural", "label": "Brian (young)"},
    {"id": "en-GB-RyanNeural", "label": "Ryan (British)"},
    {"id": "en-US-AriaNeural", "label": "Aria (female)"},
    {"id": "windows", "label": "Windows voice (offline, robotic)"},
]


def other_voice(voice_id: str) -> str:
    """The voice for a second caster when it would be the same as the first: the first neural voice that differs
    (the offline Windows voice has only one voice, so it stays)."""
    if voice_id == "windows":
        return voice_id
    return next((v["id"] for v in TTS_VOICES if v["id"] not in (voice_id, "windows")), voice_id)


def voice_label(voice_id: str) -> str:
    """'Guy' for en-US-GuyNeural (the label's first word), else the id itself."""
    for v in TTS_VOICES:
        if v["id"] == voice_id:
            return v["label"].split(" (")[0]
    return voice_id


def edge_available() -> bool:
    try:
        import edge_tts  # noqa: F401
        return True
    except ImportError:
        return False


def tts_edge(text: str, dst: Path, voice_id: str, rate: str = "+18%", pitch: str = "+10Hz") -> Path:
    import asyncio
    import edge_tts

    async def run():
        await edge_tts.Communicate(text, voice_id, rate=rate, pitch=pitch).save(str(dst))
    asyncio.run(run())
    if not dst.exists() or dst.stat().st_size < 100:
        raise RuntimeError("edge-tts produced no audio")
    return dst

def _powershell() -> str | None:
    if platform.system() == "Windows":
        return shutil.which("powershell") or shutil.which("powershell.exe")
    return shutil.which("powershell.exe")   # WSL


def tts_available() -> bool:
    return edge_available() or _powershell() is not None


def _win_path(p: Path) -> str:
    if platform.system() == "Windows":
        return str(p)
    r = subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True)
    return r.stdout.strip() or str(p)


CROWD_BED = VOICE_DIR / "_crowd.mp3"   # optional crowd-noise bed for the stadium effect (any audio file works)


def caster_fx_args(ffmpeg: Path, src: Path, dst: Path, bed: Path | None, crowd_vol: float = 0.22) -> list[str]:
    """FFmpeg command that makes a dry TTS line sound like a caster on a stage mic: push and compress the voice,
    a little arena slap-back, a crowd bed underneath (the file `bed`, or synthetic noise), loudness-normalised.
    Pure function; unit-tested."""
    voice = ("[0:a]aresample=44100,highpass=f=110,lowpass=f=9000,"
             "acompressor=threshold=-20dB:ratio=5:attack=4:release=90:makeup=4,"
             "aecho=0.75:0.35:45|85:0.22|0.12,volume=1.2[v]")
    args = [str(ffmpeg), "-y", "-i", str(src)]
    if bed and bed.is_file():
        args += ["-stream_loop", "-1", "-i", str(bed)]
        crowd = f"[1:a]aresample=44100,volume={crowd_vol:.2f}[c]"
    else:
        args += ["-f", "lavfi", "-i", "anoisesrc=color=pink:sample_rate=44100:amplitude=0.5"]
        crowd = f"[1:a]bandpass=f=900:width_type=o:w=2.2,tremolo=f=0.6:d=0.4,volume={crowd_vol * 0.5:.2f}[c]"
    graph = (f"{voice};{crowd};[v][c]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
             "afade=t=in:st=0:d=0.05,loudnorm=I=-16:TP=-1.5:LRA=7[out]")
    return args + ["-filter_complex", graph, "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "2", str(dst)]


def caster_fx(ffmpeg: Path, src: Path) -> Path:
    """Apply the stadium treatment in place (the result is always an .mp3 next to `src`; a .wav source is removed)."""
    dst = src.with_suffix(".mp3")
    tmp = src.with_name(src.stem + ".fx.tmp.mp3")
    r = subprocess.run(caster_fx_args(ffmpeg, src, tmp, CROWD_BED if CROWD_BED.is_file() else None),
                       capture_output=True, text=True)
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg failed: " + r.stderr[-300:])
    if src != dst:
        src.unlink(missing_ok=True)
    tmp.replace(dst)
    return dst


def tts(text: str, slot: str, rate: int = 2, sub: str = "", voice_id: str = "", ffmpeg: Path | None = None,
        fx: bool = False, edge_rate: str = "+18%") -> Path:
    """Speak `text` into data/voice/<slot>[/<sub>]/<slug>.mp3 with a neural voice (edge-tts), or .wav with the
    Windows voice when voice_id is "windows" or edge-tts is unavailable / offline. fx=True (needs ffmpeg) runs the
    stadium treatment afterwards (caster_fx)."""
    p = _tts_raw(text, slot, rate, sub, voice_id, edge_rate)
    if fx and ffmpeg and Path(ffmpeg).exists():
        p = caster_fx(Path(ffmpeg), p)
    return p


def _tts_raw(text: str, slot: str, rate: int, sub: str, voice_id: str, edge_rate: str = "+18%") -> Path:
    text = " ".join(str(text).split())
    if not text:
        raise ValueError("empty text")
    voice_id = voice_id or TTS_VOICES[0]["id"]
    if voice_id != "windows" and edge_available():
        try:
            return tts_edge(text, slot_dir(slot, sub) / f"{slug(text)}.mp3", voice_id, rate=edge_rate)
        except Exception as e:  # noqa: BLE001  (offline, service hiccup) -> Windows voice
            if not _powershell():
                raise RuntimeError(f"neural voice failed ({e}); no offline voice available") from e
    ps = _powershell()
    if not ps:
        raise RuntimeError("text-to-speech needs internet (neural voice) or Windows (offline voice)")
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
