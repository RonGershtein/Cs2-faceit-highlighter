# CS2 Demo Viewer & Highlight Extractor — Implementation Plan

## Goal

A local web app. I drop a FACEIT demo (`.dem.zst` or `.dem`) into it, and it:

1. Decompresses and parses the demo on my PC (no cloud, no CS2 needed for this).
2. Lets me watch the match as a **2D top-down replay** in the browser (like Scope.gg / Leetify).
3. **Automatically finds highlights** (kills, multi-kills, clutches, aces, ninja defuses) and lets me jump to them.
4. Keeps a **library** of all my demos with per-match stats.
5. Lets me **open CS2 directly at a highlight** with one click (optional, uses my local CS2 install).
6. (Later) Renders highlights to **MP4** by driving CS2 + HLAE + FFmpeg in the background, or exports a sequence list for CS Demo Manager.

Runs with one command, opens at `http://localhost:8000`.

## Non-goals (v1)

- No 3D / first-person view in the browser (requires the game engine).
- No accounts, no cloud, no multi-user.
- No live-match tracking.

## Environment / assumptions

- OS: Windows 10/11 (CS2 installed via Steam). Code must stay cross-platform except the "launch CS2" and video features.
- Python 3.11+.
- My Steam64 ID will be set in config so "my" highlights are detected by default (ask me for it; otherwise let me pick myself from the scoreboard).
- Internet is available during setup (pip, radar image download).

## Stack

| Layer | Choice | Why |
|---|---|---|
| Demo parsing | `demoparser2` (Python, Rust core) | Fast (full match in ~10–30 s), well maintained, supports CS2 |
| Decompression | `zstandard` | FACEIT ships `.dem.zst` |
| Backend | FastAPI + uvicorn | Simple, async, serves API + static files |
| Storage | SQLite (via `sqlite3` / SQLModel) + JSON files per demo | Zero setup; replay frames are big so they stay as files, not DB rows |
| Frontend | Plain HTML + JS + `<canvas>`, no build step | Easy to run, easy to debug; replay rendering is just drawing circles on an image |
| Packaging | `requirements.txt`, `run.py` | `python run.py` starts everything and opens the browser |

Do **not** introduce React/Vite/TypeScript unless the canvas UI becomes unmanageable; keep v1 dependency-light.

## Project layout

```
cs2-replay/
├── run.py                  # starts uvicorn, opens browser
├── requirements.txt
├── config.toml             # my steam64, cs2 path, demo watch folder
├── backend/
│   ├── app.py              # FastAPI app, routes, static mount
│   ├── ingest.py           # decompress + parse + write artifacts
│   ├── parser.py           # demoparser2 wrappers (ticks, events, header)
│   ├── highlights.py       # highlight detection rules
│   ├── maps.py             # radar metadata + world→radar transform
│   ├── db.py               # sqlite: demos, players, highlights
│   └── launch.py           # "open CS2 at tick" (Windows)
├── frontend/
│   ├── index.html          # library page
│   ├── match.html          # replay viewer
│   ├── js/
│   │   ├── api.js
│   │   ├── replay.js       # canvas renderer + playback engine
│   │   ├── timeline.js     # rounds/kills/highlights bar
│   │   └── ui.js
│   └── css/style.css
├── data/
│   ├── radars/             # <map>.png + <map>.json (pos_x, pos_y, scale)
│   ├── demos/              # copied .dem files
│   ├── parsed/<demo_id>/   # frames.json(.gz), events.json, meta.json
│   └── app.db
└── tests/
    └── test_parse.py       # runs against a sample demo
```

## Data pipeline

### Ingest (`POST /api/demos` with file upload, or drop in watch folder)

1. Save upload to `data/demos/<sha1>.dem(.zst)`.
2. If `.zst` → stream-decompress with `zstandard` to `.dem`.
3. Parse header (`demoparser2.DemoParser(path).parse_header()`) → map name, server name, duration/ticks.
4. Parse events:
   - `player_death` (attacker, victim, weapon, headshot, penetrated, noscope, thrusmoke, attackerblind, assister)
   - `round_start`, `round_end` (winner, reason), `round_freeze_end`
   - `bomb_planted`, `bomb_defused`, `bomb_exploded`
   - `weapon_fire` (optional, v2)
   - `player_hurt` (for damage stats)
   - grenade events: `smokegrenade_detonate`, `hegrenade_detonate`, `flashbang_detonate`, `inferno_startburn`, `decoy_started`
5. Parse ticks with `parse_ticks([...])` for props:
   `X, Y, Z, yaw, health, armor_value, is_alive, team_num, active_weapon_name, name, steamid, has_defuser, has_helmet, flash_duration, is_scoped, balance` — plus bomb carrier via `inventory` or `is_bomb_planted` where available.
   Request only every N-th tick (`ticks=range(0, max_tick, step)`), **step = 4** (16 fps) for v1. Keep step configurable.
6. Grenade projectiles: `parse_grenades()` gives projectile positions per tick → downsample same as players.
7. Write:
   - `meta.json` — map, ticks, tickrate (64), players (steamid, name, team per half), rounds (start/end/freeze_end tick, winner, score), bomb events.
   - `frames.json.gz` — array of frames: `{t: tick, p: [[sid, x, y, z, yaw, hp, alive, team, weapon, flags], ...], g: [[type, x, y, z], ...]}`. Keys are short on purpose; file is 5–20 MB gzipped per match.
   - `events.json` — kills with ticks and all flags, bomb events, round events.
8. Run highlight detection → `highlights.json` and rows in SQLite.
9. Insert demo + player stats rows (kills, deaths, assists, ADR, HS%, KAST, simple HLTV-ish rating) into SQLite.

Ingest runs in a background task; the API exposes `GET /api/demos/{id}/status` so the UI can show a progress bar (decompress → parse → highlights → done).

### Coordinates: world → radar (`maps.py`)

Each CS2 map has a radar overview with `pos_x`, `pos_y`, `scale` (from the game's `resource/overviews/<map>.txt`, now stored in community JSON). Transform:

```
px = (x - pos_x) / scale
py = (pos_y - y) / scale
```

Radar images are 1024×1024. Maps needed for v1: de_mirage, de_inferno, de_dust2, de_nuke, de_ancient, de_anubis, de_vertigo, de_overpass, de_train. Source the PNG + JSON from a maintained open-source repo (e.g. CS Demo Manager's or awpy's map assets) via a `scripts/fetch_radars.py`; fall back to extracting from my local CS2 install with Source2Viewer CLI if I ask. Nuke/Vertigo have two levels — v1 may use a single image with a Z threshold to switch; mark as known limitation.

## Highlight detection (`highlights.py`)

Input: kills (tick, attacker, victim, headshot, weapon, flags), rounds, player alive counts per tick (derive from frames), bomb events.

Rules (each produces `{type, player, round, start_tick, end_tick, score, kills:[...], description}`):

| Type | Rule | Base score |
|---|---|---|
| `multikill` | 2+ kills by same player in one round within 15 s of each other (chain) | 2K=2, 3K=4, 4K=7, 5K/ace=12 |
| `ace` | 5 kills in a round by one player | 12 |
| `clutch` | player is last alive on team vs N≥1 enemies and team wins the round | 1v1=3, 1v2=6, 1v3=9, 1v4=12, 1v5=15 |
| `ninja_defuse` | bomb defused while ≥1 enemy alive and no enemy within 500 units of bomb at defuse start | 6 |
| `collateral` / `wallbang` / `noscope` / `through_smoke` / `while_blind` | flags on kill | +1–3 bonus on the containing kill |
| `knife` | knife kill | 3 |
| `eco_frag` | kill while attacker weapon value ≪ victim's (v2) | — |
| `kill` | every single kill (so I can watch any kill) | 1 |

Clip window: `start_tick = first_kill - 4 s`, `end_tick = last_kill + 2 s` (ticks = seconds × 64). Merge overlapping windows for the same player. Sort by score desc. "My highlights" = filter by configured Steam64.

Unit-test the rules with hand-written kill lists before wiring to real demos.

## API

```
POST   /api/demos                     upload .dem/.dem.zst  → {id}
GET    /api/demos                     list (map, date, score, my K/D, status)
GET    /api/demos/{id}                meta + scoreboard
GET    /api/demos/{id}/status         ingest progress
GET    /api/demos/{id}/frames         gzipped frames (static file)
GET    /api/demos/{id}/events         kills, rounds, bomb
GET    /api/demos/{id}/highlights     sorted highlights, ?player=steam64
DELETE /api/demos/{id}
GET    /api/radars/{map}.png|json
POST   /api/demos/{id}/launch         {tick, pov_steamid} → launches CS2 (Windows)
GET    /api/demos/{id}/csdm-sequences export highlights as CS Demo Manager JSON
```

## Frontend

### Library (`index.html`)
- Drag-and-drop zone + file picker; progress bar per demo while ingesting.
- Table: date, map, score, my K/D/A, HS%, rating, top highlight. Click → viewer.

### Viewer (`match.html?id=…`)
- Canvas with radar image; draws per frame: players as circles (CT blue / T orange, dead = ×, me = ring), view cone from yaw, name labels, HP bar, weapon icon/text, bomb icon, grenades (smoke = grey circle for 18 s, molly = orange area, HE/flash = flash), kill lines fading for 1 s.
- Controls: play/pause, speed 0.5×–8×, scrub bar, round selector, prev/next round, prev/next kill, keyboard (space, ←/→ = 5 s, ,/. = frame).
- Timeline strip under canvas: rounds as segments colored by winner, kill ticks as marks, highlights as larger marks with icons.
- Side panel: live scoreboard (alive, HP, weapon, money), kill feed for current round.
- Highlights panel: list sorted by score, filter "mine / all", click = jump to `start_tick` and auto-play to `end_tick`; "Play all my highlights" loops through them.
- Buttons per highlight: **Open in CS2**, **Copy CSDM sequence**.
- Playback engine: frames are 16 fps; interpolate position/yaw between frames for smoothness at any speed. `requestAnimationFrame` loop, tick ↔ time via tickrate.

## Launch CS2 at a highlight (`launch.py`, Windows)

- Copy/symlink the `.dem` into `<CS2>/game/csgo/` (demos must live there), name `replay_<id>.dem`.
- Run `steam://rungameid/730//+playdemo replay_<id>.dem +demo_gototick <tick>` via `os.startfile`, or if CS2 is already running write the commands to a `.cfg` and tell me to `exec` it.
- Also give `spec_player <name>` / `spec_lock_to_accountid` hint for POV.
- Config: CS2 install path auto-detected from Steam `libraryfolders.vdf`, overridable in `config.toml`.

## Stages (build in this order, test after each)

**Stage 0 — Skeleton** (30 min)
`run.py`, FastAPI serving `frontend/`, `requirements.txt`, config loading, SQLite schema. Acceptance: `python run.py` opens an empty library page.

**Stage 1 — Ingest + parse** (2 h)
Upload → decompress → parse header/events/ticks → write `meta/events/frames`. CLI entry `python -m backend.ingest path.dem.zst` for testing. Acceptance: the FACEIT demo I attached parses without errors; `meta.json` shows the correct map, 10 players and round count; print kill count.

**Stage 2 — Radar + 2D replay** (3 h)
Fetch radars, coordinate transform, canvas renderer, play/pause/scrub/speed, round jump. Acceptance: players move correctly on de_* radar, positions match known spots (spawn at round start).

**Stage 3 — Events overlay + timeline** (2 h)
Kill feed, kill lines, bomb, grenades, round timeline, next/prev kill. Acceptance: pressing "next kill" lands exactly where a kill happens on the map.

**Stage 4 — Highlights** (2 h)
Detection rules + tests, highlights API, panel, auto-play, "my highlights". Acceptance: the attached demo lists my multi-kills/clutches with correct rounds; clicking plays the window.

**Stage 5 — Library + stats** (1.5 h)
Scoreboard stats, library table, delete, watch-folder auto-ingest (poll FACEIT download folder). Acceptance: dropping 3 demos yields 3 rows with correct scores.

**Stage 6 — Open in CS2 + CSDM export** (1 h)
Acceptance: clicking "Open in CS2" launches the game at the right round with the demo loaded; CSDM import file loads in CS Demo Manager.

**Stage 7 (later) — Local MP4 rendering**
Backend drives CS2 + HLAE (`mirv_streams`, `startmovie`) + FFmpeg per highlight, queue with progress, output `data/videos/<demo>/<highlight>.mp4`, merge into a reel. Reference CS Demo Manager's implementation for the HLAE command sequence. Only start after Stages 0–6 are solid.

## Performance notes

- Parse ticks with an explicit tick list (step 4) rather than all ticks, and only needed props — this is the difference between 5 s and 60 s.
- Keep frames as a gzipped static file; the browser decompresses natively via `Content-Encoding: gzip`. Don't send frames through JSON API per request.
- Canvas: pre-render the radar once; draw only entities per frame; cap at display refresh rate.
- Ingest in a background thread/process so the UI stays responsive; demoparser2 releases the GIL in Rust.

## Testing

- `tests/test_parse.py`: ingest the sample demo (path from env var), assert map, player count, rounds ≥ 16, kills > 0, every kill tick inside a round.
- `tests/test_highlights.py`: synthetic kill lists → expected multikill/clutch/ace output.
- Manual: compare 3 random kills' positions vs the FACEIT match room / in-game demo.

## Open questions for me (ask before Stage 1)

1. My Steam64 ID (or I'll pick myself from the scoreboard).
2. Where FACEIT downloads land (watch folder), e.g. `C:\Users\<me>\Downloads`.
3. CS2 install path if auto-detect fails.
4. Any other platform besides FACEIT (Valve MM demos use the same parser; just confirm).

## References

- demoparser2: https://github.com/LaihoE/demoparser
- CS Demo Manager (reference for radar assets, HLAE/FFmpeg video pipeline, sequence format): https://github.com/akiver/cs-demo-manager
- awpy (map data / coordinate transforms): https://github.com/pnxenopoulos/awpy
- HLAE: https://github.com/advancedfx/advancedfx
- Browser-rendered demo write-up (architecture reference): https://healeycodes.com/rendering-counter-strike-demos-in-the-browser
