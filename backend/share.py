"""Sharing a video: show it in Explorer for the YouTube upload, remember the link YouTube gives, email it.

YouTube's API cannot make a personal app's uploads unlisted (they stay private until Google audits the app), so the
upload itself is done by hand on youtube.com/upload; the app hands over the file, a title and a description, keeps
the link (data/videos/<match or "montages">/shares.json) and sends it to the owner's mailbox (Gmail with an app
password, set in Settings).
"""
from __future__ import annotations

import json
import re
import smtplib
import subprocess
from email.message import EmailMessage
from pathlib import Path

from .config import DATA_DIR, load as load_config

VIDEOS_DIR = DATA_DIR / "videos"
YOUTUBE_URL = re.compile(r"^https?://(www\.|m\.)?(youtube\.com/(watch\?v=|shorts/|live/)|youtu\.be/)[\w-]{6,}[^\s]*$")


def video_path(demo_id: str, file: str) -> Path:
    """The video's file; refuses anything that is not a plain file name inside the app's videos folder."""
    if not file or Path(file).name != file or not re.fullmatch(r"[\w-]+", demo_id or ""):
        raise ValueError("not a video of the app")
    p = VIDEOS_DIR / demo_id / file
    if not p.is_file():
        raise ValueError("the video is not there any more")
    return p


def _shares(demo_id: str) -> tuple[Path, dict]:
    p = VIDEOS_DIR / demo_id / "shares.json"
    try:
        return p, json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (OSError, ValueError):
        return p, {}


def normalize_youtube(url: str) -> str:
    """The link as pasted, trimmed, when it is a YouTube video link; ValueError otherwise. Pure function."""
    url = (url or "").strip()
    if url and not url.startswith("http"):
        url = "https://" + url
    if not YOUTUBE_URL.match(url):
        raise ValueError("that is not a YouTube video link (youtu.be/… or youtube.com/watch?v=…)")
    return url


def video_info(demo_id: str, file: str) -> dict:
    """{"title", "description", "link", "path"} of a video: the title and description suggested for YouTube (the
    plays of a montage listed), and the link saved when it was shared."""
    p = video_path(demo_id, file)
    title, plays = Path(file).stem, []
    try:
        if demo_id == "montages":
            ms = json.loads((VIDEOS_DIR / "montages" / "montages.json").read_text(encoding="utf-8"))
            ms = ms.get("montages", ms) if isinstance(ms, dict) else ms
            e = next((m for m in ms if m.get("file") == file), None)
            if e:
                title = e.get("description") or title
                plays = [c.get("description") for c in e.get("clips", []) if c.get("description")]
        else:
            idx = json.loads((VIDEOS_DIR / demo_id / "videos.json").read_text(encoding="utf-8"))
            e = next((v for v in idx.get("videos", []) + idx.get("long", []) if file in (v.get("file"), v.get("cine"), v.get("reel"))), None)
            if e:
                title = e.get("description") or title
    except (OSError, ValueError):
        pass
    team = ""
    try:
        team = json.loads((DATA_DIR / "squad.json").read_text(encoding="utf-8")).get("team") or ""
    except (OSError, ValueError, AttributeError):
        pass
    title = (f"{team} · {title}" if team and team not in title else title)[:100]
    desc = "\n".join(([f"{len(plays)} plays:"] + [f"• {d}" for d in plays] + [""]) if plays else []) \
        + "Made with CS2 FACEIT Highlighter"
    return {"title": title, "description": desc, "link": _shares(demo_id)[1].get(file), "path": str(p)}


def save_link(demo_id: str, file: str, url: str) -> str:
    video_path(demo_id, file)
    url = normalize_youtube(url)
    p, shares = _shares(demo_id)
    shares[file] = url
    p.write_text(json.dumps(shares, indent=1), encoding="utf-8")
    return url


def reveal(demo_id: str, file: str) -> None:
    """Open Explorer on the video's folder with the file selected, ready to drag onto the upload page."""
    p = video_path(demo_id, file)
    subprocess.Popen(f'explorer /select,"{p}"')


def email_message(to: str, title: str, link: str, description: str) -> EmailMessage:
    """The mail with the link. Pure function; unit-tested."""
    m = EmailMessage()
    m["Subject"] = title
    m["From"] = to
    m["To"] = to
    m.set_content(f"{title}\n\n{link}\n\n{description}\n")
    return m


def email_link(demo_id: str, file: str) -> str:
    """Send the saved link to the address in Settings through Gmail; returns the address."""
    cfg = load_config()
    to, pw = str(cfg.get("share_email") or "").strip(), str(cfg.get("share_app_password") or "").replace(" ", "")
    if not to or not pw:
        raise RuntimeError("set your Gmail address and app password in Settings first")
    info = video_info(demo_id, file)
    if not info["link"]:
        raise RuntimeError("paste the YouTube link first")
    msg = email_message(to, info["title"], info["link"], info["description"])
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls()
            s.login(to, pw)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise RuntimeError("Gmail refused the login: check the address and the app password in Settings")
    except (OSError, smtplib.SMTPException) as e:
        raise RuntimeError(f"sending failed: {e}")
    return to
