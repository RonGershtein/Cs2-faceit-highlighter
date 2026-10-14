"""demoparser2 wrappers: header, events, rounds, sampled ticks -> frames, grenades, stats.

Output of parse_demo() is a plain dict of JSON-serialisable pieces:
  meta      -> meta.json
  events    -> events.json
  frames    -> frames.json.gz (list of frames)
  stats     -> per-player scoreboard rows
"""
from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from typing import Callable

import numpy as np
import pandas as pd
from demoparser2 import DemoParser

TICKRATE = 64
TEAM_NAMES = {2: "T", 3: "CT"}

TICK_PROPS = ["X", "Y", "Z", "yaw", "health", "armor_value", "is_alive", "team_num",
              "active_weapon_name", "name", "steamid", "has_defuser", "has_helmet",
              "flash_duration", "is_scoped", "balance", "ducking", "inventory"]
OPTIONAL_PROPS = ["team_clan_name"]

EVENT_NAMES = ["player_death", "player_hurt", "round_start", "round_end", "round_freeze_end",
               "round_officially_ended", "begin_new_match", "bomb_planted", "bomb_defused",
               "bomb_exploded", "bomb_begindefuse", "bomb_dropped", "bomb_pickup",
               "smokegrenade_detonate", "smokegrenade_expired", "hegrenade_detonate",
               "flashbang_detonate", "inferno_startburn", "inferno_expire", "decoy_started",
               "decoy_detonate", "cs_win_panel_match"]

GRENADE_TYPES = {
    "CSmokeGrenadeProjectile": "smoke",
    "CHEGrenadeProjectile": "he",
    "CMolotovProjectile": "molotov",
    "CFlashbangProjectile": "flash",
    "CDecoyProjectile": "decoy",
}

# flag bits per player per frame
F_DEFUSER, F_HELMET, F_SCOPED, F_BOMB, F_BLIND, F_DUCKING = 1, 2, 4, 8, 16, 32

KNIFE_NAMES = {"bayonet", "karambit", "m9 bayonet", "knife", "knife_t", "knife_tactical", "knife_canis",
               "knife_css", "knife_cord", "knife_skeleton", "knife_outdoor", "knife_widowmaker", "knife_gypsy_jackknife",
               "knife_ursus", "knife_stiletto", "knife_push", "knife_falchion", "knife_survival_bowie", "knife_butterfly",
               "knife_flip", "knife_gut", "knife_karambit", "knife_m9_bayonet", "knife_tactical", "knife_kukri"}


def norm_weapon(name) -> str:
    if name is None or (isinstance(name, float) and math.isnan(name)):
        return ""
    s = str(name)
    low = s.lower()
    if "knife" in low or low in KNIFE_NAMES or low == "bayonet" or low == "karambit":
        return "knife"
    return s


def _sid(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, float):
        if math.isnan(v):
            return None
        return str(int(v))
    s = str(v)
    return s if s and s != "nan" and s != "0" else None


def _events_dict(parser: DemoParser, names: list[str]) -> dict[str, pd.DataFrame]:
    try:
        parsed = parser.parse_events(names, other=["total_rounds_played", "is_warmup_period", "game_phase"])
    except Exception:  # noqa: BLE001 - some names may be unknown on old demos
        parsed = parser.parse_events(names)
    return {name: df for name, df in parsed}


def _detect_rounds(ev: dict[str, pd.DataFrame]) -> list[dict]:
    """Return live match rounds (knife round & warmup excluded)."""
    starts = ev.get("round_start", pd.DataFrame())
    ends = ev.get("round_end", pd.DataFrame())
    freeze = ev.get("round_freeze_end", pd.DataFrame())
    offic = ev.get("round_officially_ended", pd.DataFrame())
    bnm = ev.get("begin_new_match", pd.DataFrame())
    match_begin = int(bnm["tick"].max()) if len(bnm) else 0

    if "winner" not in ends.columns:
        ends = ends.assign(winner=None)
    ends = ends[ends["winner"].notna() & (ends["tick"] > match_begin)].sort_values("tick")
    if "is_warmup_period" in ends.columns:
        ends = ends[~ends["is_warmup_period"].fillna(False).astype(bool)]
    start_ticks = sorted(int(t) for t in starts["tick"].tolist()) if len(starts) else []
    freeze_ticks = sorted(int(t) for t in freeze["tick"].tolist()) if len(freeze) else []
    offic_ticks = sorted(int(t) for t in offic["tick"].tolist()) if len(offic) else []

    rounds: list[dict] = []
    prev_end = -1
    for _, r in ends.iterrows():
        end_tick = int(r["tick"])
        cand = [t for t in start_ticks if prev_end < t <= end_tick]
        if not cand:
            continue
        start_tick = max(cand) if not rounds else min(cand)
        fe = [t for t in freeze_ticks if start_tick <= t <= end_tick]
        freeze_end = min(fe) if fe else start_tick
        oe = [t for t in offic_ticks if t >= end_tick]
        official_end = min(oe) if oe else end_tick + 7 * TICKRATE
        rounds.append({
            "num": len(rounds) + 1,
            "start": start_tick,
            "freeze_end": freeze_end,
            "end": end_tick,
            "official_end": official_end,
            "winner": str(r["winner"]),
            "reason": str(r.get("reason", "")) if r.get("reason") is not None else "",
        })
        prev_end = end_tick
    return rounds


def _round_for_tick(rounds: list[dict], tick: int) -> int | None:
    for r in rounds:
        if r["start"] <= tick <= r["official_end"]:
            return r["num"]
    return None


class PosIndex:
    """Nearest-frame position lookup: pos_at(steamid, tick) -> (x, y, z) or None."""

    def __init__(self, df: pd.DataFrame):
        self.by_sid: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
        for sid, g in df.groupby("steamid", sort=False):
            g = g.sort_values("tick")
            self.by_sid[str(sid)] = (g["tick"].to_numpy(), g["X"].to_numpy(), g["Y"].to_numpy(),
                                     g["Z"].to_numpy(), g["team_num"].to_numpy())

    def pos_at(self, sid: str, tick: int):
        e = self.by_sid.get(str(sid))
        if e is None:
            return None
        ticks = e[0]
        i = int(np.searchsorted(ticks, tick, side="right")) - 1
        if i < 0:
            i = 0
        if abs(int(ticks[i]) - tick) > TICKRATE * 2:
            return None
        return float(e[1][i]), float(e[2][i]), float(e[3][i])

    def team_at(self, sid: str, tick: int) -> str | None:
        e = self.by_sid.get(str(sid))
        if e is None:
            return None
        i = int(np.searchsorted(e[0], tick, side="right")) - 1
        if i < 0:
            return None
        t = e[4][i]
        return TEAM_NAMES.get(int(t)) if not (isinstance(t, float) and math.isnan(t)) else None


def parse_demo(path: str, step: int = 4, progress: Callable[[str, float], None] | None = None) -> dict:
    def report(stage: str, frac: float) -> None:
        if progress:
            progress(stage, frac)

    parser = DemoParser(path)
    header = parser.parse_header()
    map_name = header.get("map_name", "unknown")
    report("events", 0.05)

    ev = _events_dict(parser, EVENT_NAMES)
    rounds = _detect_rounds(ev)
    if not rounds:
        raise RuntimeError("no rounds found in demo")

    first_tick = max(0, rounds[0]["start"] - 2 * TICKRATE)
    last_tick = rounds[-1]["official_end"] + 5 * TICKRATE
    report("ticks", 0.15)

    props = TICK_PROPS + OPTIONAL_PROPS
    wanted = list(range(first_tick, last_tick + 1, step))
    try:
        df = parser.parse_ticks(props, ticks=wanted)
    except Exception:  # noqa: BLE001
        df = parser.parse_ticks(TICK_PROPS, ticks=wanted)
    df = df[df["steamid"] > 0]
    df = df.sort_values(["tick", "steamid"]).reset_index(drop=True)
    report("frames", 0.45)

    # ---- players -----------------------------------------------------------
    info = parser.parse_player_info()
    players: list[dict] = []
    sid_index: dict[str, int] = {}
    seen = set()
    for _, r in info.iterrows():
        sid = _sid(r["steamid"])
        if not sid or sid in seen:
            continue
        seen.add(sid)
        players.append({"steamid": sid, "name": str(r["name"]), "team_start": "", "clan": ""})
    for sid, name in df.drop_duplicates("steamid")[["steamid", "name"]].itertuples(index=False):
        sid = _sid(sid)
        if sid and sid not in seen:
            seen.add(sid)
            players.append({"steamid": sid, "name": str(name), "team_start": "", "clan": ""})
    for i, p in enumerate(players):
        sid_index[p["steamid"]] = i

    pos = PosIndex(df)

    # team per round per player (team at freeze end of each round)
    for r in rounds:
        teams = {}
        for p in players:
            t = pos.team_at(p["steamid"], r["freeze_end"] + 1) or pos.team_at(p["steamid"], r["start"] + 1)
            if t:
                teams[p["steamid"]] = t
        r["teams"] = teams
    for p in players:
        for r in rounds:
            t = r["teams"].get(p["steamid"])
            if t:
                p["team_start"] = t
                break
    if "team_clan_name" in df.columns:
        first = df[df["tick"] >= rounds[0]["freeze_end"]].drop_duplicates("steamid")
        for sid, clan in first[["steamid", "team_clan_name"]].itertuples(index=False):
            sid = _sid(sid)
            if sid in sid_index and isinstance(clan, str):
                players[sid_index[sid]]["clan"] = clan

    # ---- frames ------------------------------------------------------------
    weapons: list[str] = []
    weapon_index: dict[str, int] = {}

    def widx(name: str) -> int:
        if name not in weapon_index:
            weapon_index[name] = len(weapons)
            weapons.append(name)
        return weapon_index[name]

    cols = {c: df[c].to_numpy() for c in ["tick", "steamid", "X", "Y", "Z", "yaw", "health", "armor_value",
                                           "is_alive", "team_num", "active_weapon_name", "has_defuser",
                                           "has_helmet", "flash_duration", "is_scoped", "balance", "ducking"]}
    inv = df["inventory"].tolist()
    n = len(df)
    # flash_duration never resets after a flash, so detect changes and compute a "blind until" tick
    blind_until: dict[str, float] = {}
    last_flash: dict[str, float] = {}
    frames: list[dict] = []
    cur_tick = None
    cur: list | None = None

    def truthy(v) -> bool:
        return bool(v) and not (isinstance(v, float) and math.isnan(v))

    def num(v, default=0.0) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return default
        return default if math.isnan(f) else f

    for i in range(n):
        t = int(cols["tick"][i])
        if t != cur_tick:
            cur_tick = t
            cur = []
            frames.append({"t": t, "p": cur})
        sid = _sid(cols["steamid"][i])
        if sid is None or sid not in sid_index:
            continue
        flags = 0
        if truthy(cols["has_defuser"][i]):
            flags |= F_DEFUSER
        if truthy(cols["has_helmet"][i]):
            flags |= F_HELMET
        if truthy(cols["is_scoped"][i]):
            flags |= F_SCOPED
        fd = num(cols["flash_duration"][i])
        if fd != last_flash.get(sid):
            last_flash[sid] = fd
            if fd > 0:
                blind_until[sid] = t + fd * TICKRATE
        if t < blind_until.get(sid, -1):
            flags |= F_BLIND
        if truthy(cols["ducking"][i]):
            flags |= F_DUCKING
        items = inv[i]
        if isinstance(items, (list, tuple, np.ndarray)) and any("C4" in str(x) for x in items):
            flags |= F_BOMB
        team = cols["team_num"][i]
        team_s = TEAM_NAMES.get(int(num(team, 0)), "")
        cur.append([
            sid_index[sid],
            round(num(cols["X"][i]), 1), round(num(cols["Y"][i]), 1), round(num(cols["Z"][i]), 1),
            int(round(num(cols["yaw"][i]))),
            int(num(cols["health"][i])), int(num(cols["armor_value"][i])),
            1 if truthy(cols["is_alive"][i]) else 0,
            team_s,
            widx(norm_weapon(cols["active_weapon_name"][i])),
            flags,
            int(num(cols["balance"][i])),
        ])
    frames.sort(key=lambda f: f["t"])
    frame_ticks = [f["t"] for f in frames]
    report("grenades", 0.6)

    # ---- grenade projectiles ---------------------------------------------
    try:
        g = parser.parse_grenades()
    except Exception:  # noqa: BLE001
        g = pd.DataFrame()
    if len(g):
        g = g[g["grenade_type"].isin(GRENADE_TYPES.keys()) & g["x"].notna()]
        g = g[(g["tick"] >= first_tick) & (g["tick"] <= last_tick) & ((g["tick"] - first_tick) % step == 0)]
        by_tick: dict[int, list] = defaultdict(list)
        for gt, x, y, z, t in g[["grenade_type", "x", "y", "z", "tick"]].itertuples(index=False):
            by_tick[int(t)].append([GRENADE_TYPES[gt], round(float(x), 1), round(float(y), 1), round(float(z), 1)])
        for f in frames:
            gl = by_tick.get(f["t"])
            if gl:
                f["g"] = gl
    report("kills", 0.7)

    # ---- kills ---------------------------------------------------------------
    kills: list[dict] = []
    deaths = ev.get("player_death", pd.DataFrame())
    for _, r in deaths.iterrows():
        tick = int(r["tick"])
        rnd = _round_for_tick(rounds, tick)
        if rnd is None:
            continue
        victim = _sid(r.get("user_steamid"))
        attacker = _sid(r.get("attacker_steamid"))
        assister = _sid(r.get("assister_steamid"))
        if victim is None:
            continue
        rteams = rounds[rnd - 1]["teams"]
        vteam = rteams.get(victim) or pos.team_at(victim, tick)
        ateam = (rteams.get(attacker) or pos.team_at(attacker, tick)) if attacker else None
        apos = pos.pos_at(attacker, tick) if attacker else None
        vpos = pos.pos_at(victim, tick)
        kills.append({
            "tick": tick,
            "round": rnd,
            "attacker": attacker,
            "victim": victim,
            "assister": assister,
            "attacker_name": str(r.get("attacker_name") or ""),
            "victim_name": str(r.get("user_name") or ""),
            "attacker_team": ateam,
            "victim_team": vteam,
            "weapon": norm_weapon(r.get("weapon")),
            "headshot": bool(r.get("headshot", False)),
            "penetrated": int(r.get("penetrated", 0) or 0),
            "noscope": bool(r.get("noscope", False)),
            "thrusmoke": bool(r.get("thrusmoke", False)),
            "attackerblind": bool(r.get("attackerblind", False)),
            "attackerinair": bool(r.get("attackerinair", False)),
            "assistedflash": bool(r.get("assistedflash", False)),
            "apos": [round(v, 1) for v in apos] if apos else None,
            "vpos": [round(v, 1) for v in vpos] if vpos else None,
        })
    kills.sort(key=lambda k: k["tick"])

    # ---- bomb ----------------------------------------------------------------
    bomb: list[dict] = []
    for name, kind in [("bomb_planted", "planted"), ("bomb_defused", "defused"), ("bomb_exploded", "exploded"),
                       ("bomb_begindefuse", "begindefuse"), ("bomb_dropped", "dropped"), ("bomb_pickup", "pickup")]:
        d = ev.get(name)
        if d is None or not len(d):
            continue
        for _, r in d.iterrows():
            tick = int(r["tick"])
            rnd = _round_for_tick(rounds, tick)
            if rnd is None:
                continue
            sid = _sid(r.get("user_steamid"))
            p = pos.pos_at(sid, tick) if sid else None
            bomb.append({"type": kind, "tick": tick, "round": rnd, "player": sid,
                         "player_name": str(r.get("user_name") or ""),
                         "site": _site_name(r.get("site")) if kind in ("planted", "defused") else None,
                         "pos": [round(v, 1) for v in p] if p else None,
                         "haskit": bool(r.get("haskit", False)) if kind == "begindefuse" else None})
    bomb.sort(key=lambda b: b["tick"])

    # ---- grenade areas ---------------------------------------------------------
    def paired(start_name: str, end_name: str, default_len: int) -> list[dict]:
        s = ev.get(start_name)
        if s is None or not len(s):
            return []
        e = ev.get(end_name)
        ends_by_ent: dict[int, list[int]] = defaultdict(list)
        if e is not None and len(e) and "entityid" in e.columns:
            for ent, t in e[["entityid", "tick"]].itertuples(index=False):
                ends_by_ent[int(ent)].append(int(t))
        out = []
        for _, r in s.iterrows():
            t = int(r["tick"])
            ent = int(r.get("entityid", -1) or -1)
            later = [x for x in ends_by_ent.get(ent, []) if x >= t]
            end = min(later) if later else t + default_len
            out.append({"start": t, "end": end, "x": round(float(r["x"]), 1), "y": round(float(r["y"]), 1),
                        "z": round(float(r["z"]), 1), "player": _sid(r.get("user_steamid"))})
        return out

    def points(name: str) -> list[dict]:
        d = ev.get(name)
        if d is None or not len(d):
            return []
        return [{"tick": int(r["tick"]), "x": round(float(r["x"]), 1), "y": round(float(r["y"]), 1),
                 "z": round(float(r["z"]), 1), "player": _sid(r.get("user_steamid"))} for _, r in d.iterrows()]

    smokes = paired("smokegrenade_detonate", "smokegrenade_expired", 20 * TICKRATE)
    fires = paired("inferno_startburn", "inferno_expire", 7 * TICKRATE)
    flashes = points("flashbang_detonate")
    hes = points("hegrenade_detonate")
    report("stats", 0.8)

    # ---- stats -------------------------------------------------------------------
    hurt = ev.get("player_hurt", pd.DataFrame())
    stats = _compute_stats(players, rounds, kills, hurt, pos)

    score_a = sum(1 for r in rounds if r["winner"] == _team_in_round(rounds, r["num"], players, "A"))
    score_b = len(rounds) - score_a
    team_a_players = [p["steamid"] for p in players if p["team_start"] == "CT"]
    team_b_players = [p["steamid"] for p in players if p["team_start"] == "T"]

    meta = {
        "map": map_name,
        "server": header.get("server_name", ""),
        "tickrate": TICKRATE,
        "step": step,
        "first_tick": frame_ticks[0] if frame_ticks else first_tick,
        "last_tick": frame_ticks[-1] if frame_ticks else last_tick,
        "players": players,
        "weapons": weapons,
        "rounds": rounds,
        "score": {"a": score_a, "b": score_b},
        "team_a": {"name": _clan_name(players, team_a_players) or "Team A (CT start)", "players": team_a_players},
        "team_b": {"name": _clan_name(players, team_b_players) or "Team B (T start)", "players": team_b_players},
        "flags": {"defuser": F_DEFUSER, "helmet": F_HELMET, "scoped": F_SCOPED, "bomb": F_BOMB,
                  "blind": F_BLIND, "ducking": F_DUCKING},
    }
    events = {"kills": kills, "bomb": bomb, "smokes": smokes, "fires": fires, "flashes": flashes, "hes": hes,
              "rounds": rounds}
    report("done-parse", 0.85)
    return {"meta": meta, "events": events, "frames": frames, "stats": stats, "pos": pos}


def _site_name(v) -> str | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    # entity index of the site trigger; cannot map to A/B reliably without entity names
    return str(n)


def _clan_name(players: list[dict], sids: list[str]) -> str:
    names = [p["clan"] for p in players if p["steamid"] in sids and p.get("clan")]
    return names[0] if names else ""


def _team_in_round(rounds: list[dict], num: int, players: list[dict], side: str) -> str:
    """Which side (CT/T) team A (= CT in round 1) plays in the given round."""
    r = rounds[num - 1]
    for p in players:
        if p["team_start"] == "CT":
            t = r["teams"].get(p["steamid"])
            if t:
                return t if side == "A" else ("T" if t == "CT" else "CT")
    # fallback: assume halves of 12
    first_half = num <= 12
    return ("CT" if first_half else "T") if side == "A" else ("T" if first_half else "CT")


def _compute_stats(players: list[dict], rounds: list[dict], kills: list[dict], hurt: pd.DataFrame,
                   pos: PosIndex) -> list[dict]:
    n_rounds = max(1, len(rounds))
    st = {p["steamid"]: {"steamid": p["steamid"], "name": p["name"], "team_start": p["team_start"],
                         "kills": 0, "deaths": 0, "assists": 0, "headshots": 0, "damage": 0,
                         "mk1": 0, "mk2": 0, "mk3": 0, "mk4": 0, "mk5": 0, "clutches": 0,
                         "kast_rounds": 0} for p in players}
    per_round_kills: dict[tuple[int, str], int] = defaultdict(int)
    kast: dict[tuple[int, str], bool] = defaultdict(bool)
    survived: dict[tuple[int, str], bool] = {}

    for r in rounds:
        for sid in r["teams"]:
            survived[(r["num"], sid)] = True

    kills_by_round: dict[int, list[dict]] = defaultdict(list)
    for k in kills:
        kills_by_round[k["round"]].append(k)

    for k in kills:
        a, v, rnd = k["attacker"], k["victim"], k["round"]
        if v in st:
            st[v]["deaths"] += 1
            survived[(rnd, v)] = False
        teamkill = a is not None and k["attacker_team"] and k["attacker_team"] == k["victim_team"]
        if a and a in st and a != v and not teamkill:
            st[a]["kills"] += 1
            if k["headshot"]:
                st[a]["headshots"] += 1
            per_round_kills[(rnd, a)] += 1
            kast[(rnd, a)] = True
        if k["assister"] and k["assister"] in st and not teamkill:
            st[k["assister"]]["assists"] += 1
            kast[(rnd, k["assister"])] = True
        # traded: a teammate kills the attacker within 5 s
        if a and v in st:
            for k2 in kills_by_round[rnd]:
                if k2["victim"] == a and k2["tick"] > k["tick"] and k2["tick"] - k["tick"] <= 5 * TICKRATE \
                        and k2["attacker_team"] == k["victim_team"]:
                    kast[(rnd, v)] = True
                    break

    for (rnd, sid), alive in survived.items():
        if alive:
            kast[(rnd, sid)] = True
    for (rnd, sid), ok in kast.items():
        if ok and sid in st:
            st[sid]["kast_rounds"] += 1
    for (rnd, sid), c in per_round_kills.items():
        if sid in st and 1 <= c <= 5:
            st[sid][f"mk{c}"] += 1

    if hurt is not None and len(hurt):
        for _, r in hurt.iterrows():
            tick = int(r["tick"])
            rnd = _round_for_tick(rounds, tick)
            if rnd is None:
                continue
            a, v = _sid(r.get("attacker_steamid")), _sid(r.get("user_steamid"))
            if not a or a not in st or a == v:
                continue
            rteams = rounds[rnd - 1]["teams"]
            if rteams.get(a) and rteams.get(a) == rteams.get(v):
                continue
            try:
                dmg = int(r.get("dmg_health", 0) or 0)
            except (TypeError, ValueError):
                dmg = 0
            st[a]["damage"] += min(dmg, 100)

    out = []
    for s in st.values():
        rounds_played = sum(1 for r in rounds if s["steamid"] in r["teams"]) or n_rounds
        s["adr"] = round(s["damage"] / rounds_played, 1)
        s["kast"] = round(100.0 * s["kast_rounds"] / rounds_played, 1)
        kill_rating = s["kills"] / rounds_played / 0.679
        surv_rating = (rounds_played - s["deaths"]) / rounds_played / 0.317
        rmk = (s["mk1"] + 4 * s["mk2"] + 9 * s["mk3"] + 16 * s["mk4"] + 25 * s["mk5"]) / rounds_played / 1.277
        s["rating"] = round((kill_rating + 0.7 * surv_rating + rmk) / 2.7, 2)
        s["rounds"] = rounds_played
        out.append(s)
    out.sort(key=lambda s: s["rating"], reverse=True)
    return out
