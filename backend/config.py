"""Config loading. config.toml lives next to run.py; missing file -> defaults are written."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DEMOS_DIR = DATA_DIR / "demos"
PARSED_DIR = DATA_DIR / "parsed"
RADARS_DIR = DATA_DIR / "radars"
FRONTEND_DIR = ROOT / "frontend"
DB_PATH = DATA_DIR / "app.db"
CONFIG_PATH = ROOT / "config.toml"

DEFAULTS: dict = {
    "my_steamid": "",          # Steam64 of "me"; empty -> pick from scoreboard in the UI
    "cs2_path": "",            # path to .../Counter-Strike Global Offensive ; empty -> auto-detect
    "watch_folder": "",        # folder polled for new .dem / .dem.zst files (e.g. FACEIT downloads)
    "watch_interval": 15,      # seconds between polls
    "tick_step": 4,            # keep every N-th tick in frames (64/4 = 16 fps)
    "keep_decompressed": False,  # keep the .dem next to the .dem.zst after parsing (needed for instant CS2 launch)
    "host": "127.0.0.1",
    "port": 8000,
    "open_browser": True,
    "record_width": 1920,      # CS2 window size while recording
    "record_height": 1080,
    "record_fps": 60,
    "record_crf": 20,          # x264 quality (lower = better/bigger)
    "record_close_game": True, # quit CS2 when the last highlight is recorded
    "export_folder": "",       # where finished clips are copied; empty -> <Downloads>/cs2-watcher/<map>_<date>_<id>
    "cine_slow": 0.35,         # slow-motion speed around kills (0.35 = 35 % speed)
    "cine_kills": "last",      # which kills get slow motion: "last" (the final kill of the clip) or "all"
    "cine_smooth": False,      # motion-interpolate slow segments (smoother but ~40x slower to encode)
    "cine_game_volume": 0.35,  # game audio level under the music
    "cine_music_volume": 0.7,
    "cine_music": "",          # music file name in data/music; empty -> a random track per clip
    "cine_music_sync": True,   # start the track at its drop so the drop lands on the slow-motion kill
    "cine_trim": "fast",       # dead time between kills: "off" (keep), "cut" (drop it) or "fast" (fast-forward it)
    "cine_trim_lead": 2.0,     # seconds of normal speed kept before each kill when trimming
    "cine_fast": 6.0,          # fast-forward speed for dead time (cine_trim = "fast")
    "cine_ramp": True,         # ease in and out of slow motion instead of a hard cut
    "cine_impact": True,       # zoom punch + flash on each kill
    "cine_look": True,         # vignette + colour grade
    "cine_fade": True,         # fade in/out at the clip edges
    "cine_title": True,        # title card (3K / ACE / clutch, player, map, round)
    "cine_counter": True,      # kill counter in the corner
    "cine_card": True,         # player card: round avatar (data/players/<steam64>.png, blank until added) + nickname
    "cine_voice": True,        # play voice lines from data/voice (start / kill / final) when there are any
    "cine_voice_volume": 1.0,
    "cine_voice_pick": "",      # JSON {"start": [...], "kill": [...], "final": [...]} of pinned lines; empty = random
    "cine_tts_voice": "en-US-ChristopherNeural",   # voice for generated lines ("windows" = offline voice)
    "cine_tts_fx": True,        # stadium treatment for generated lines: compression, arena echo, crowd bed
}


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def write(cfg: dict) -> None:
    lines = ["# cs2-watcher configuration", ""]
    for k, default in DEFAULTS.items():
        lines.append(f"{k} = {_toml_value(cfg.get(k, default))}")
    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            user = tomllib.load(f)
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    else:
        write(cfg)
    cfg["my_steamid"] = str(cfg.get("my_steamid") or "")
    for d in (DATA_DIR, DEMOS_DIR, PARSED_DIR, RADARS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    return cfg


def update(**changes) -> dict:
    cfg = load()
    cfg.update({k: v for k, v in changes.items() if k in DEFAULTS})
    write(cfg)
    return cfg
