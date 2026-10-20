"""Highlight detection. Pure functions over plain dicts so the rules are unit-testable.

Inputs
  kills:  [{tick, round, attacker, victim, attacker_team, victim_team, weapon, headshot, penetrated,
            noscope, thrusmoke, attackerblind, assister, attacker_name, victim_name}]
  rounds: [{num, start, freeze_end, end, official_end, winner ('CT'|'T'), teams: {steamid: 'CT'|'T'}}]
  bomb:   [{type: planted|defused|begindefuse|exploded, tick, round, player, pos}]
  pos_at: optional callable (steamid, tick) -> (x, y, z) | None  (needed for ninja defuse)

Output: list of highlights sorted by score desc:
  {type, player, player_name, round, start_tick, end_tick, score, kills: [kill ticks], description, ...}
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Callable

TICKRATE = 64
PRE_S, POST_S = 4, 2

MULTIKILL_SCORE = {2: 2, 3: 4, 4: 7, 5: 12}
CLUTCH_SCORE = {1: 3, 2: 6, 3: 9, 4: 12, 5: 15}
NINJA_SCORE = 6
KNIFE_SCORE = 3
NINJA_RADIUS = 500.0


def clutch_start(kills: list[dict], teams: dict, player: str) -> tuple[int, int] | None:
    """(tick, enemies alive) when `player` became the last one alive of their team in a round, from the round's kills
    in order and its {steamid: side}; None when they never were. Pure function; unit-tested."""
    side = teams.get(str(player))
    if side not in ("CT", "T"):
        return None
    alive = {"CT": {s for s, t in teams.items() if t == "CT"}, "T": {s for s, t in teams.items() if t == "T"}}
    for k in sorted(kills, key=lambda k: k["tick"]):
        vt = k.get("victim_team") or teams.get(k.get("victim"))
        if vt in alive:
            alive[vt].discard(k.get("victim"))
        if alive[side] == {str(player)} and alive[_other(side)]:
            return k["tick"], len(alive[_other(side)])
    return None


def _other(team: str) -> str:
    return "T" if team == "CT" else "CT"


def _valid_kill(k: dict) -> bool:
    a, v = k.get("attacker"), k.get("victim")
    if not a or not v or a == v:
        return False
    if k.get("attacker_team") and k.get("victim_team") and k["attacker_team"] == k["victim_team"]:
        return False
    return True


def kill_bonus(k: dict) -> tuple[float, list[str]]:
    bonus, tags = 0.0, []
    if k.get("headshot"):
        tags.append("headshot")
    if k.get("penetrated"):
        bonus += 1
        tags.append("wallbang")
    if k.get("noscope"):
        bonus += 2
        tags.append("noscope")
    if k.get("thrusmoke"):
        bonus += 1
        tags.append("through smoke")
    if k.get("attackerblind"):
        bonus += 2
        tags.append("while blind")
    if k.get("attackerinair"):
        bonus += 1
        tags.append("airborne")
    if k.get("weapon") == "knife":
        bonus += KNIFE_SCORE
        tags.append("knife")
    return bonus, tags


def _window(first_tick: int, last_tick: int, rnd: dict | None, tickrate: int = TICKRATE) -> tuple[int, int]:
    start = first_tick - PRE_S * tickrate
    end = last_tick + POST_S * tickrate
    if rnd:
        start = max(start, rnd["start"])
        end = min(end, rnd.get("official_end", rnd["end"] + 7 * tickrate))
    return int(start), int(end)


WEAPON_NAMES = {"usp_silencer": "USP-S", "hkp2000": "P2000", "glock": "Glock", "elite": "Dualies", "p250": "P250",
                "fiveseven": "Five-SeveN", "tec9": "Tec-9", "cz75a": "CZ75", "deagle": "Deagle", "revolver": "R8",
                "ak47": "AK-47", "m4a1": "M4A4", "m4a1_silencer": "M4A1-S", "famas": "FAMAS", "galilar": "Galil",
                "aug": "AUG", "sg556": "SG 553", "awp": "AWP", "ssg08": "Scout", "scar20": "SCAR-20", "g3sg1": "G3SG1",
                "mac10": "MAC-10", "mp9": "MP9", "mp7": "MP7", "mp5sd": "MP5-SD", "ump45": "UMP-45", "p90": "P90",
                "bizon": "PP-Bizon", "nova": "Nova", "xm1014": "XM1014", "mag7": "MAG-7", "sawedoff": "Sawed-Off",
                "m249": "M249", "negev": "Negev", "hegrenade": "HE grenade", "flashbang": "flashbang",
                "smokegrenade": "smoke", "molotov": "molotov", "incgrenade": "incendiary", "inferno": "molotov",
                "decoy": "decoy", "knife": "knife", "taser": "Zeus", "world": "world", "planted_c4": "C4"}


def weapon_name(w: str | None) -> str:
    return WEAPON_NAMES.get(w or "", w or "?")


def _name(k: dict, who: str, names: dict[str, str]) -> str:
    sid = k.get(who)
    return names.get(sid) or k.get(f"{'attacker' if who == 'attacker' else 'victim'}_name") or str(sid)


def detect(kills: list[dict], rounds: list[dict], bomb: list[dict] | None = None,
           pos_at: Callable | None = None, tickrate: int = TICKRATE) -> list[dict]:
    bomb = bomb or []
    rounds_by_num = {r["num"]: r for r in rounds}
    names: dict[str, str] = {}
    for k in kills:
        if k.get("attacker") and k.get("attacker_name"):
            names[k["attacker"]] = k["attacker_name"]
        if k.get("victim") and k.get("victim_name"):
            names[k["victim"]] = k["victim_name"]

    out: list[dict] = []
    kills_by_round: dict[int, list[dict]] = defaultdict(list)
    for k in sorted(kills, key=lambda k: k["tick"]):
        kills_by_round[k["round"]].append(k)

    # ---- single kills ---------------------------------------------------------
    for k in kills:
        if not _valid_kill(k):
            continue
        rnd = rounds_by_num.get(k["round"])
        bonus, tags = kill_bonus(k)
        s, e = _window(k["tick"], k["tick"], rnd, tickrate)
        desc = f"{_name(k, 'attacker', names)} killed {_name(k, 'victim', names)} with {weapon_name(k.get('weapon'))}"
        if tags:
            desc += " (" + ", ".join(tags) + ")"
        out.append({"type": "knife" if k.get("weapon") == "knife" else "kill", "player": k["attacker"],
                    "player_name": _name(k, "attacker", names), "round": k["round"], "start_tick": s,
                    "end_tick": e, "score": 1 + bonus, "kills": [k["tick"]], "tags": tags, "description": desc})

    # ---- multikills (chains) --------------------------------------------------
    for rnum, rk in kills_by_round.items():
        rnd = rounds_by_num.get(rnum)
        by_attacker: dict[str, list[dict]] = defaultdict(list)
        for k in rk:
            if _valid_kill(k):
                by_attacker[k["attacker"]].append(k)
        for sid, ks in by_attacker.items():
            # every kill of a player in the round is one highlight (2K..ACE), however far apart: two pairs of
            # kills 30 s apart are a 4K, not two 2Ks. The cinematic pace fast-forwards the quiet part between them.
            for ch in [ks]:
                n = len(ch)
                if n < 2:
                    continue
                bonus = sum(kill_bonus(k)[0] for k in ch)
                ticks = [k["tick"] for k in ch]
                collateral = any(ticks[i + 1] - ticks[i] <= 2 for i in range(len(ticks) - 1))
                if collateral:
                    bonus += 2
                s, e = _window(ticks[0], ticks[-1], rnd, tickrate)
                typ = "ace" if n >= 5 else "multikill"
                label = {2: "2K", 3: "3K", 4: "4K"}.get(n, "ACE")
                tags = sorted({t for k in ch for t in kill_bonus(k)[1]})
                desc = f"{label} by {names.get(sid, sid)} in round {rnum}"
                if collateral:
                    desc += " (collateral)"
                out.append({"type": typ, "player": sid, "player_name": names.get(sid, sid), "round": rnum,
                            "start_tick": s, "end_tick": e, "score": MULTIKILL_SCORE.get(min(n, 5), 12) + bonus,
                            "kills": ticks, "n": n, "tags": tags, "description": desc})

    # ---- clutches ---------------------------------------------------------------
    for rnd in rounds:
        rnum = rnd["num"]
        teams = rnd.get("teams") or {}
        alive: dict[str, set] = {"CT": set(), "T": set()}
        for sid, t in teams.items():
            if t in alive:
                alive[t].add(sid)
        if not alive["CT"] or not alive["T"]:
            continue
        situations: dict[str, dict] = {}
        for k in kills_by_round.get(rnum, []):
            v = k.get("victim")
            vt = k.get("victim_team") or teams.get(v)
            if vt in alive:
                alive[vt].discard(v)
            for team in ("CT", "T"):
                if team in situations:
                    continue
                enemies = len(alive[_other(team)])
                if len(alive[team]) == 1 and enemies >= 1:
                    situations[team] = {"player": next(iter(alive[team])), "vs": enemies, "tick": k["tick"]}
        winner = rnd.get("winner")
        sit = situations.get(winner)
        if not sit:
            continue
        sid = sit["player"]
        own_kills = [k for k in kills_by_round.get(rnum, []) if k.get("attacker") == sid and k["tick"] >= sit["tick"]
                     and _valid_kill(k)]
        n = sit["vs"]
        ticks = [k["tick"] for k in own_kills]
        first = min([sit["tick"]] + ticks)
        last = max([sit["tick"]] + ticks)
        s, _ = _window(first, last, rnd, tickrate)
        s = max(rnd["start"], sit["tick"] - 2 * tickrate) if not ticks else s
        e = min(rnd.get("official_end", rnd["end"] + 7 * tickrate), rnd["end"] + 2 * tickrate)
        bonus = sum(kill_bonus(k)[0] for k in own_kills)
        desc = f"1v{n} clutch by {names.get(sid, sid)} in round {rnum}"
        if len(own_kills) >= 2:
            desc += f" ({len(own_kills)}K)"
        out.append({"type": "clutch", "player": sid, "player_name": names.get(sid, sid), "round": rnum,
                    "start_tick": s, "end_tick": e, "score": CLUTCH_SCORE.get(min(n, 5), 15) + bonus,
                    "kills": ticks, "n": n, "tags": [], "description": desc, "clutch_tick": sit["tick"]})

    # ---- ninja defuse ------------------------------------------------------------
    for b in bomb:
        if b.get("type") != "defused":
            continue
        rnum, sid, tick = b["round"], b.get("player"), b["tick"]
        rnd = rounds_by_num.get(rnum)
        if not rnd or not sid:
            continue
        teams = rnd.get("teams") or {}
        alive_t = {s for s, t in teams.items() if t == "T"}
        for k in kills_by_round.get(rnum, []):
            if k["tick"] <= tick and (k.get("victim_team") or teams.get(k.get("victim"))) == "T":
                alive_t.discard(k.get("victim"))
        if not alive_t:
            continue
        begins = [x for x in bomb if x.get("type") == "begindefuse" and x["round"] == rnum and x["tick"] <= tick
                  and x.get("player") == sid]
        begin_tick = begins[-1]["tick"] if begins else tick - 5 * tickrate
        if pos_at is None:
            continue
        me = pos_at(sid, begin_tick)
        if not me:
            continue
        near = False
        for t_sid in alive_t:
            p = pos_at(t_sid, begin_tick)
            if p and math.dist(p[:2], me[:2]) < NINJA_RADIUS:
                near = True
                break
        if near:
            continue
        s = max(rnd["start"], begin_tick - 3 * tickrate)
        e = min(rnd.get("official_end", rnd["end"] + 7 * tickrate), tick + 2 * tickrate)
        out.append({"type": "ninja_defuse", "player": sid, "player_name": names.get(sid, b.get("player_name", sid)),
                    "round": rnum, "start_tick": s, "end_tick": e, "score": NINJA_SCORE + 2 * (len(alive_t) - 1),
                    "kills": [], "n": len(alive_t), "tags": [],
                    "description": f"Ninja defuse by {names.get(sid, b.get('player_name', sid))} vs {len(alive_t)} in round {rnum}"})

    out = _merge(out)
    out.sort(key=lambda h: (-h["score"], h["start_tick"]))
    for i, h in enumerate(out):
        h["id"] = i
    return out


def _merge(hs: list[dict]) -> list[dict]:
    """Merge overlapping windows of the same player for the 'big' types (multikill/ace/clutch/ninja)."""
    big = [h for h in hs if h["type"] in ("multikill", "ace", "clutch", "ninja_defuse")]
    rest = [h for h in hs if h["type"] not in ("multikill", "ace", "clutch", "ninja_defuse")]
    big.sort(key=lambda h: (h["player"], h["round"], h["start_tick"]))
    merged: list[dict] = []
    for h in big:
        last = merged[-1] if merged else None
        if last and last["player"] == h["player"] and last["round"] == h["round"] and h["start_tick"] <= last["end_tick"]:
            # combine
            prio = {"clutch": 4, "ace": 3, "ninja_defuse": 2, "multikill": 1}
            keep, drop = (last, h) if prio[last["type"]] >= prio[h["type"]] else (h, last)
            keep["start_tick"] = min(last["start_tick"], h["start_tick"])
            keep["end_tick"] = max(last["end_tick"], h["end_tick"])
            keep["kills"] = sorted(set(last["kills"]) | set(h["kills"]))
            # score: strongest + half of the other (both describe the same play)
            keep["score"] = round(max(last["score"], h["score"]) + 0.5 * min(last["score"], h["score"]), 1)
            if keep["type"] == "clutch" and len(keep["kills"]) >= 2:
                # the label counts every kill of the play, the ones before the clutch situation included
                keep["description"] = re.sub(r" \(\d+K\)$", "", keep["description"]) + f" ({len(keep['kills'])}K)"
            if not (keep["type"] == "clutch" and drop["type"] in ("multikill", "ace")):
                keep["description"] = keep["description"] + " + " + drop["description"].split(" by ")[0]
            elif drop["type"] == "ace" and "ACE" not in keep["description"]:
                keep["description"] = keep["description"] + " (ACE)"
            keep["tags"] = sorted(set(last.get("tags", [])) | set(h.get("tags", [])))
            merged[-1] = keep
        else:
            merged.append(dict(h))
    return merged + rest
