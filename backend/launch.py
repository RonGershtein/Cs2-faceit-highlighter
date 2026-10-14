"""Open CS2 at a tick. Windows-first; also works from WSL by shelling out to cmd.exe.

Flow:
 1. Make sure a plain .dem exists inside <CS2>/game/csgo/ as replay_<id>.dem (demos must live there).
 2. Write <CS2>/game/csgo/cfg/hl.cfg with `demo_gototick <tick>` (+ spec_player) so the user can `exec hl`
    in the console once the demo is loaded.
 3. Launch `steam://rungameid/730//+playdemo replay_<id>.dem` .
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path

from .config import load as load_config
from .ingest import ensure_dem

APP_ID = "730"
CS2_SUBDIR = Path("steamapps/common/Counter-Strike Global Offensive")


def is_wsl() -> bool:
    return "microsoft" in platform.release().lower() or Path("/mnt/c/Windows").exists()


def _win_to_wsl(p: str) -> str:
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if m and is_wsl():
        return f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")
    return p.replace("\\\\", "\\")


def steam_roots() -> list[Path]:
    cands: list[Path] = []
    if platform.system() == "Windows":
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                cands.append(Path(winreg.QueryValueEx(k, "SteamPath")[0]))
        except Exception:  # noqa: BLE001
            pass
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(env):
                cands.append(Path(os.environ[env]) / "Steam")
    elif is_wsl():
        cands += [Path("/mnt/c/Program Files (x86)/Steam"), Path("/mnt/c/Program Files/Steam")]
    else:
        home = Path.home()
        cands += [home / ".steam/steam", home / ".local/share/Steam", home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
    return [c for c in cands if c.exists()]


def find_cs2_path() -> Path | None:
    cfg = load_config()
    if cfg.get("cs2_path"):
        p = Path(_win_to_wsl(cfg["cs2_path"]))
        if p.exists():
            return p
    for root in steam_roots():
        direct = root / CS2_SUBDIR
        if direct.exists():
            return direct
        vdf = root / "steamapps/libraryfolders.vdf"
        if vdf.exists():
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="ignore")):
                lib = Path(_win_to_wsl(m.group(1)))
                if (lib / CS2_SUBDIR).exists():
                    return lib / CS2_SUBDIR
    return None


def find_steam_exe() -> Path | None:
    for root in steam_roots():
        for name in ("steam.exe", "steam.sh", "steam"):
            p = root / name
            if p.exists():
                return p
    return None


def _wsl_to_win(p: Path) -> str:
    m = re.match(r"^/mnt/([a-z])/(.*)$", str(p))
    return f"{m.group(1).upper()}:\\" + m.group(2).replace("/", "\\") if m else str(p)


def cs2_running() -> bool:
    try:
        if platform.system() == "Windows" or is_wsl():
            out = subprocess.run(["tasklist.exe", "/FI", "IMAGENAME eq cs2.exe", "/NH"], capture_output=True,
                                 text=True, timeout=10, cwd="/mnt/c" if is_wsl() else None).stdout
            return "cs2.exe" in out.lower()
        out = subprocess.run(["pgrep", "-x", "cs2"], capture_output=True, text=True, timeout=10).stdout
        return bool(out.strip())
    except Exception:  # noqa: BLE001
        return False


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _launch_with_args(cs2: Path, args: list[str]) -> bool:
    """Start CS2 through the Steam client: steam.exe -applaunch 730 <args>.

    Running cs2.exe directly (what CS Demo Manager does) is refused with "Access is denied" when the caller
    is a WSL process, and the steam:// URL scheme drops +commands, so -applaunch is the reliable path.
    """
    if platform.system() == "Windows":
        # Native Windows: run cs2.exe directly with the +commands, exactly like CS Demo Manager.
        cs2_exe = cs2 / "game" / "bin" / "win64" / "cs2.exe"
        if cs2_exe.exists():
            subprocess.Popen([str(cs2_exe), *args], cwd=str(cs2_exe.parent), close_fds=True,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            return True
    exe = find_steam_exe()
    if exe is None:
        return False
    full = ["-applaunch", APP_ID, *args]
    if platform.system() == "Windows":
        subprocess.Popen([str(exe), *full], close_fds=True)
    elif is_wsl():
        arglist = ",".join(_ps_quote(a) for a in full)
        cmd = f"Start-Process -FilePath {_ps_quote(_wsl_to_win(exe))} -ArgumentList {arglist}"
        subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", cmd], cwd="/mnt/c",
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen([str(exe), *full], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def _win_path(p: Path) -> str:
    return _wsl_to_win(p) if is_wsl() else str(p)


def _open_url(url: str) -> None:
    if platform.system() == "Windows":
        os.startfile(url)  # type: ignore[attr-defined]
    elif is_wsl():
        subprocess.Popen(["cmd.exe", "/c", "start", "", url], cwd="/mnt/c", stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    else:
        opener = shutil.which("xdg-open") or shutil.which("open")
        if not opener:
            raise RuntimeError("no URL opener found")
        subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def launch(demo_id: str, tick: int, player_name: str | None = None, player_steamid: str | None = None) -> dict:
    cs2 = find_cs2_path()
    if cs2 is None:
        raise RuntimeError("CS2 install not found; set cs2_path in config.toml")
    csgo_dir = cs2 / "game" / "csgo"
    if not csgo_dir.exists():
        raise RuntimeError(f"{csgo_dir} does not exist")
    replay_name = f"replay_{demo_id[:8]}.dem"
    ensure_dem(demo_id, csgo_dir / replay_name)

    start_tick = max(0, int(tick))
    lines = [f"demo_gototick {start_tick}"]
    if player_name:
        safe = player_name.replace('"', "")
        lines.append(f'spec_player "{safe}"')
    if player_steamid:
        lines.append(f"// spec_lock_to_accountid {int(player_steamid) - 76561197960265728}")
    cfg_dir = csgo_dir / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "hl.cfg").write_text("\n".join(lines) + "\n", encoding="utf-8")

    console_cmd = f"playdemo {replay_name}; exec hl"
    if cs2_running():
        return {"ok": False, "already_running": True, "demo": str(csgo_dir / replay_name), "cfg": str(cfg_dir / "hl.cfg"),
                "hint": f"CS2 is already running. Open the console and type:  {console_cmd}"}
    demo_arg = _win_path(csgo_dir / replay_name)
    # -insecure is required for +playdemo to be honoured at launch (same as CS Demo Manager). It only
    # disables VAC for this session, so CS2 refuses VAC-secured servers until restarted normally from Steam.
    launched = _launch_with_args(cs2, ["-insecure", "-novid", "+playdemo", f'"{demo_arg}"' if is_wsl() else demo_arg,
                                       "+demo_gototick", str(start_tick)])
    if not launched:
        url = f"steam://rungameid/{APP_ID}//{urllib.parse.quote('+playdemo ' + replay_name)}"
        _open_url(url)
    return {
        "ok": True,
        "demo": str(csgo_dir / replay_name),
        "cfg": str(cfg_dir / "hl.cfg"),
        "hint": f"When the demo is loaded open the console and type:  exec hl   (jumps to tick {start_tick}"
                + (f", spectating {player_name}" if player_name else "") + "). "
                f"If the demo does not start by itself, type:  {console_cmd}",
    }
