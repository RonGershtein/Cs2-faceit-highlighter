# CS2 FACEIT Highlighter

Drop a FACEIT (or Valve MM) CS2 demo into a local web app and get:

- a **2D top-down replay** in the browser (players, view cones, HP, smokes, molotovs, grenades, bomb, kill lines)
- **automatic highlights**: multi-kills, aces, clutches, ninja defuses, knife kills, with a "mine" filter
- per-match **stats** (K/D/A, ADR, HS%, KAST, rating) and a library of all your matches
- **Open in CS2** at any highlight, or watch the whole demo in the game
- **MP4 recording** of selected highlights (CS2 + HLAE + FFmpeg, first-person, with game audio), a combined reel,
  and a **cinematic** version with slow-motion on the final kill and your own music

Everything runs on your PC. Nothing is uploaded anywhere.

![library](docs/library.png)
![viewer](docs/viewer.png)

## Requirements

| | |
|---|---|
| OS | Windows 10/11 for the full feature set (CS2 launching and recording). The replay viewer and highlights also work on Linux/macOS. |
| Python | 3.11 or 3.12 from [python.org](https://www.python.org/downloads/). Tick **"Add python.exe to PATH"** in the installer. |
| CS2 | installed through Steam (only needed for "Open in CS2" and recording) |
| Disk | ~250 MB per demo kept by the app, plus ~35 MB per recorded 20 s clip |

HLAE and FFmpeg are downloaded by the app itself when you first click **Install HLAE + FFmpeg** (about 100 MB).

## Install and run (Windows)

1. Download this repository: **Code → Download ZIP** and unzip it, or `git clone https://github.com/RonGershtein/Cs2-faceit-highlighter`.
   Put it in a folder **without spaces** in the path, for example `C:\cs2-highlighter`.
2. Double-click **`start.bat`**.
   The first run creates a Python environment, installs the dependencies, downloads the radar images (1–3 minutes)
   and puts a **CS2 Highlighter** icon on your Desktop.
3. Your browser opens at **http://localhost:8000**. From now on just double-click the Desktop icon to start the app;
   close its (minimized) console window to stop it.

### Commands

Everything can also be done from a terminal. `make` on Linux/macOS/WSL, `cs2w.bat` on Windows (same commands):

| command | what it does |
|---|---|
| `make install` / `cs2w install` | create the Python env, install dependencies, download radars |
| `make run` / `cs2w run` | start the app |
| `make doctor` / `cs2w doctor` | check Python, dependencies, config, CS2 path, HLAE/FFmpeg, disk |
| `make tools` / `cs2w tools` | download HLAE + FFmpeg for recording (same as the button in the app) |
| `make test` / `cs2w test` | run the unit tests |
| `make parse DEMO=x.dem.zst` / `cs2w parse x.dem.zst` | parse one demo from the CLI |
| `make shortcut` / `cs2w shortcut` | (re)create the Desktop icon |
| `make update` / `cs2w update` | `git pull` + reinstall dependencies |
| `make clean` / `cs2w clean` | remove the Python env (keeps your data) |

### First-time setup (2 minutes)

1. Click **⚙ Settings** (top right).
2. **Demo download folder**: click **Browse…** and pick the folder your browser saves FACEIT demos to (usually `Downloads`).
   Every new demo that lands there is parsed automatically.
3. **Save clips to**: where recorded videos are copied. Leave empty for `Downloads\cs2-watcher`.
4. Save. Drop your first demo on the big drop area (or just download one from FACEIT).
5. Open the match. The app asks **"Who are you in this match?"** — click your name. From then on, "Mine" filters show your highlights and the library shows your stats.

## Using it

### Library
Match cards show the score from your side, win/loss, your K/D/A, ADR, HS% and rating, and the top highlight. Click a card to open the viewer. Hover a card and click ✕ to remove a match from the app (your original download and any recorded clips are kept).

### Viewer
- **Play / pause**: `space`. `←` `→` seek 5 s, `,` `.` one frame, `[` `]` previous/next kill, `PgUp` `PgDn` previous/next round, `L` toggles the lower level on Nuke/Vertigo/Train.
- Click the timeline to jump. Rounds are colored by the winning side; marks are kills, bars are highlights.
- **Highlights** tab: click **▶ Play** to watch the moment in 2D. **Open in CS2** starts the game with the demo; when it has loaded, open the console (`` ` ``) and type `exec hl` to jump to the highlight spectating the player.
- **Watch in CS2** (top bar) opens the whole demo from round one.
- The handle between the map and the panel can be dragged to resize the panel.

### Recording highlights to MP4
1. Click **Install HLAE + FFmpeg** once (in the Highlights panel). HLAE is the tool every CS movie maker uses; it is only used for demo playback.
2. Tick the highlights you want (or **Select all**) and click **Record N selected**.
3. Read the checklist: **close CS2 and the FACEIT client** first. Click **Start recording**.
4. CS2 opens in a window and records the clips one by one in your first-person view, then closes itself.
   Budget about a minute per highlight. Don't touch the game meanwhile.
5. Each card gets a **▶ Video** button. Clips are in the app and copied to your clips folder; with 2+ clips you also get **reel.mp4**.

CS2 runs with `-insecure` while recording, which only disables VAC for that session. When you want to play, start CS2 normally from Steam as usual.

### Cinematic clips (slow motion + music)
Click **✨ Cinematic**, add one or more music files (MP3/M4A/WAV; use royalty-free music if you plan to publish), then **Make cinematic versions**.
The final kill of each clip gets a slow-motion moment, the music plays under the game sound, and a `reel_cinematic.mp4` is built. Takes ~5 s per clip.

### Config file
`config.toml` is created next to `start.bat` on first run. Most of it is editable from Settings; the rest:

| key | default | meaning |
|---|---|---|
| `tick_step` | 4 | keep every N-th tick for the 2D replay (4 = 16 fps) |
| `record_width` / `record_height` / `record_fps` | 1920 / 1080 / 60 | recording resolution and frame rate |
| `record_crf` | 20 | x264 quality, lower = better and bigger |
| `record_close_game` | true | quit CS2 after the last clip |
| `cine_slow` | 0.35 | slow-motion speed |
| `cine_kills` | "last" | `last` or `all` kills get slow motion |
| `cine_smooth` | false | motion-interpolated slow motion (much slower to encode) |
| `cine_game_volume` / `cine_music_volume` | 0.35 / 0.7 | audio mix |

Restart the app after editing the file by hand.

## Troubleshooting

- **The page says "API.… is not a function" or looks broken after an update**: hard refresh with `Ctrl+F5`.
- **"Open in CS2" opens the game at round one**: that is expected, type `exec hl` in the console to jump.
- **Recording: CS2 opens and sits in the menu / nothing recorded**: close the FACEIT client, make sure CS2 was fully closed before starting, then try again. If the demo plays but nothing happens, open the console and type `exec cs2watcher_rec`.
- **Recording: no audio in the clip**: update to the latest version of this repo (fixed) and re-record.
- **A demo stays at "parsing"**: look at the console window for the error; remove the match and drop it again.
- **Python not found**: install Python 3.11/3.12 from python.org with "Add to PATH" ticked, then run `start.bat` again.
- **Something else is off**: run `cs2w doctor` (or `make doctor`); it lists what is missing and how to fix it.

## Safety

HLAE and `-insecure` are the standard, long-established way of recording CS demos (CS Demo Manager works the same way). They are never used on live servers and the game refuses VAC-secured servers while in that mode. Keep the FACEIT client closed while recording.

## Development

```
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt    # or .venv/bin on Linux
python scripts/fetch_radars.py
python run.py                       # http://localhost:8000
pytest tests/test_highlights.py tests/test_record.py
CS2_SAMPLE_DEMO=path/to/match.dem.zst pytest tests/test_parse.py
python -m backend.ingest path/to/match.dem.zst   # parse one demo from the CLI
```

```
run.py / start.bat       start the server (+ browser); cs2w.bat and Makefile wrap the common commands
scripts/doctor.py        environment check; scripts/fetch_tools.py downloads HLAE + FFmpeg; scripts/make_shortcut.ps1 Desktop icon
backend/app.py           FastAPI routes, static frontend
backend/ingest.py        decompress → parse → artifacts → highlights → sqlite; watch folder
backend/parser.py        demoparser2: rounds, sampled frames, kills, grenades, stats
backend/highlights.py    highlight rules (unit tested)
backend/maps.py          radar calibration (from CS Demo Manager) and world→radar transform
backend/launch.py        open CS2 at a tick
backend/record.py        HLAE + FFmpeg recording pipeline
backend/cinematic.py     slow-motion + music post-processing
frontend/                index.html (library), match.html (viewer), js/replay.js (canvas renderer)
data/                    demos, parsed matches, videos, music, app.db   (not in git)
tools/                   HLAE and FFmpeg                                (downloaded, not in git)
```

Parsing happens on your PC, so hosting this on a serverless platform is not an option; a VPS or a static viewer fed with the parsed JSON would be.

## Credits

- [demoparser2](https://github.com/LaihoE/demoparser) for parsing
- [HLAE](https://github.com/advancedfx/advancedfx) and [FFmpeg](https://ffmpeg.org) ([BtbN builds](https://github.com/BtbN/FFmpeg-Builds)) for recording
- radar images and calibration from [CS Demo Manager](https://github.com/akiver/cs-demo-manager), whose recording approach this project follows

## License

MIT
