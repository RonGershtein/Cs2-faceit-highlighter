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
    "render_parallel": 3,      # montage plays rendered at the same time (FFmpeg uses only a core or two per play)
    "render_cache_gb": 20,     # finished montage pieces kept for the next montage/recap (least recently used go first)
    "render_while_recording": True,  # a montage starts rendering the plays already recorded while CS2 records the rest
    "record_encoder": "auto",  # recording in CS2: "auto" (the graphics card's encoder when it works here, else the CPU),
                               # "x264" (CPU), "nvenc" (NVIDIA), "amf" (AMD) or "qsv" (Intel)
    "record_fast_preroll": False,  # play the unrecorded 2 s before each highlight at 4x (a little faster recording)
    "cine_drop_slowmo": True,  # long montages: slow motion only on the plays whose kill lands on a drop of the song
    "cine_drop_min_plays": 6,  # from how many plays on
    "montage_transition": "mixed",  # between montage plays: "mixed" (whip, zoom, flash in turn), "whip", "zoom", "flash", "none"
    "montage_beat_cuts": True, # end every montage play on a beat of the song (its tail cut a little)
    "montage_outro": True,     # end montages with the end card: GG, the team name and the stats of the plays
    "montage_order": "song",   # squad montages and recaps: "song" (plays ordered to fit the song's drops) or "date"
    "record_demoui_each_demo": True,  # several demos in one CS2 session: close the demo panel again after each playdemo
    "record_hud": "killfeed",  # "killfeed": a clean picture with the kill feed only; "health_ammo": CS2's full HUD
                               # (health and ammo, but also the players bar at the top: CS2 cannot hide only that)
    "export_folder": "",       # where finished clips are copied; empty -> <Downloads>/cs2-watcher/<map>_<date>_<id>
    "share_email": "",         # Gmail address that receives the YouTube links of shared videos
    "share_app_password": "",  # a Gmail app password for it (myaccount.google.com/apppasswords); stays on this PC
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
    "cine_skip_waits": True,   # a long wait is not fast-forwarded in full: a warp in and out of it, the middle cut
    "cine_ramp": True,         # ease in and out of slow motion instead of a hard cut
    "cine_impact": True,       # zoom punch + flash on each kill
    "cine_look": True,         # vignette + colour grade
    "cine_grade": "teal_orange",  # the colour grade: "teal_orange", "dark" (cinematic 3D LUTs) or "classic"
    "cine_fade": True,         # fade in/out at the clip edges
    "cine_title": True,        # title card (3K / ACE / clutch, player, map, round)
    "cine_counter": True,      # kill counter in the corner
    "cine_clutch": True,       # clutches: 1vN counting down at the top left, with the round and the score
    "cine_card": True,         # player card: round avatar (data/players/<steam64>.png, blank until added) + nickname
    "cine_voice": False,       # real caster voice lines from data/voice; per run only (cinematic.PER_RUN_KEYS), never saved
    "cine_voice_volume": 1.0,
    "cine_voice_pick": "",      # JSON {"start": [...], "kill": [...], "final": [...]} of pinned lines; empty = random
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
