"""SQLite storage: demos, per-player stats, highlights. Replay frames stay on disk as files."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

from .config import DB_PATH

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS demos (
    id TEXT PRIMARY KEY,
    filename TEXT,
    source_path TEXT,
    map TEXT,
    server TEXT,
    date TEXT,
    tickrate INTEGER DEFAULT 64,
    total_ticks INTEGER DEFAULT 0,
    rounds INTEGER DEFAULT 0,
    score_a INTEGER DEFAULT 0,
    score_b INTEGER DEFAULT 0,
    team_a TEXT DEFAULT '',
    team_b TEXT DEFAULT '',
    status TEXT DEFAULT 'queued',
    stage TEXT DEFAULT '',
    progress REAL DEFAULT 0,
    error TEXT,
    size INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS players (
    demo_id TEXT,
    steamid TEXT,
    name TEXT,
    team_start TEXT,
    kills INTEGER, deaths INTEGER, assists INTEGER, headshots INTEGER,
    damage INTEGER, adr REAL, kast REAL, rating REAL,
    mk2 INTEGER, mk3 INTEGER, mk4 INTEGER, mk5 INTEGER, clutches INTEGER,
    PRIMARY KEY (demo_id, steamid)
);
CREATE TABLE IF NOT EXISTS highlights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    demo_id TEXT,
    type TEXT,
    steamid TEXT,
    player_name TEXT,
    round INTEGER,
    start_tick INTEGER,
    end_tick INTEGER,
    score REAL,
    description TEXT,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_highlights_demo ON highlights(demo_id);
CREATE TABLE IF NOT EXISTS watched (
    path TEXT PRIMARY KEY,
    size INTEGER,
    demo_id TEXT
);
"""


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(SCHEMA)
            _conn.commit()
        return _conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# ---- demos -----------------------------------------------------------------

def upsert_demo(demo_id: str, **fields) -> None:
    with _lock:
        c = conn()
        exists = c.execute("SELECT 1 FROM demos WHERE id=?", (demo_id,)).fetchone()
        if exists:
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                c.execute(f"UPDATE demos SET {sets} WHERE id=?", (*fields.values(), demo_id))
        else:
            fields.setdefault("created_at", now_iso())
            cols = ", ".join(["id", *fields])
            qs = ", ".join("?" * (len(fields) + 1))
            c.execute(f"INSERT INTO demos ({cols}) VALUES ({qs})", (demo_id, *fields.values()))
        c.commit()


def set_status(demo_id: str, status: str, stage: str = "", progress: float | None = None, error: str | None = None) -> None:
    fields = {"status": status, "stage": stage}
    if progress is not None:
        fields["progress"] = progress
    if error is not None:
        fields["error"] = error
    upsert_demo(demo_id, **fields)


def get_demo(demo_id: str) -> dict | None:
    with _lock:
        r = conn().execute("SELECT * FROM demos WHERE id=?", (demo_id,)).fetchone()
        return dict(r) if r else None


def list_demos() -> list[dict]:
    with _lock:
        return _rows(conn().execute("SELECT * FROM demos ORDER BY date DESC, created_at DESC"))


def delete_demo(demo_id: str) -> None:
    with _lock:
        c = conn()
        c.execute("DELETE FROM highlights WHERE demo_id=?", (demo_id,))
        c.execute("DELETE FROM players WHERE demo_id=?", (demo_id,))
        c.execute("DELETE FROM demos WHERE id=?", (demo_id,))
        c.execute("DELETE FROM watched WHERE demo_id=?", (demo_id,))
        c.commit()


# ---- players ---------------------------------------------------------------

def replace_players(demo_id: str, players: list[dict]) -> None:
    with _lock:
        c = conn()
        c.execute("DELETE FROM players WHERE demo_id=?", (demo_id,))
        for p in players:
            c.execute(
                """INSERT INTO players (demo_id, steamid, name, team_start, kills, deaths, assists, headshots,
                   damage, adr, kast, rating, mk2, mk3, mk4, mk5, clutches)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (demo_id, str(p["steamid"]), p["name"], p.get("team_start", ""), p["kills"], p["deaths"],
                 p["assists"], p["headshots"], p["damage"], p["adr"], p["kast"], p["rating"],
                 p.get("mk2", 0), p.get("mk3", 0), p.get("mk4", 0), p.get("mk5", 0), p.get("clutches", 0)),
            )
        c.commit()


def get_players(demo_id: str) -> list[dict]:
    with _lock:
        return _rows(conn().execute("SELECT * FROM players WHERE demo_id=? ORDER BY rating DESC", (demo_id,)))


def get_player(demo_id: str, steamid: str) -> dict | None:
    with _lock:
        r = conn().execute("SELECT * FROM players WHERE demo_id=? AND steamid=?", (demo_id, str(steamid))).fetchone()
        return dict(r) if r else None


# ---- highlights ------------------------------------------------------------

def replace_highlights(demo_id: str, highlights: list[dict]) -> None:
    with _lock:
        c = conn()
        c.execute("DELETE FROM highlights WHERE demo_id=?", (demo_id,))
        for h in highlights:
            c.execute(
                """INSERT INTO highlights (demo_id, type, steamid, player_name, round, start_tick, end_tick, score, description, data)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (demo_id, h["type"], str(h["player"]), h.get("player_name", ""), h["round"], h["start_tick"],
                 h["end_tick"], h["score"], h["description"], json.dumps(h)),
            )
        c.commit()


def get_highlights(demo_id: str, steamid: str | None = None, include_kills: bool = True) -> list[dict]:
    q = "SELECT data FROM highlights WHERE demo_id=?"
    args: list = [demo_id]
    if steamid:
        q += " AND steamid=?"
        args.append(str(steamid))
    if not include_kills:
        q += " AND type != 'kill'"
    q += " ORDER BY score DESC, start_tick ASC"
    with _lock:
        rows = conn().execute(q, args).fetchall()
    out = []
    for r in rows:
        out.append(json.loads(r["data"]))
    return out


def top_highlight(demo_id: str, steamid: str | None = None) -> dict | None:
    hs = get_highlights(demo_id, steamid, include_kills=False)
    return hs[0] if hs else None


# ---- watch folder ----------------------------------------------------------

def watched_get(path: str) -> dict | None:
    with _lock:
        r = conn().execute("SELECT * FROM watched WHERE path=?", (path,)).fetchone()
        return dict(r) if r else None


def watched_set(path: str, size: int, demo_id: str) -> None:
    with _lock:
        c = conn()
        c.execute("INSERT OR REPLACE INTO watched (path, size, demo_id) VALUES (?,?,?)", (path, size, demo_id))
        c.commit()
