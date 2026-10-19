"""Full play-by-play commentary for a highlight, written from the demo events (no language model) and spoken
with the generated voice.

  timeline()      kills / bomb events inside the clip with who was still alive on each side
  write_script()  lines {t (clip seconds), text, kind, priority} from templates, varied at random
  schedule()      output times that do not overlap, dropping low-priority lines when the clip is too dense
  speak()         TTS per line (cached by text + voice), returns [(file, out_time)] like the voice lines
"""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

from . import voice
from .config import DATA_DIR
from .highlights import weapon_name

TTS_CACHE = DATA_DIR / "tts_cache"

SNIPERS = {"awp", "ssg08", "scar20", "g3sg1"}
PISTOLS = {"deagle", "elite", "fiveseven", "glock", "tec9", "usp_silencer", "hkp2000", "p250", "cz75a", "revolver"}
KNIVES = {"knife", "knife_t", "bayonet", "knifegg"}
NADES = {"hegrenade", "inferno", "molotov", "incgrenade", "flashbang", "smokegrenade", "decoy"}


# ---- names the voice can say ------------------------------------------------------------------------

def speakable(name: str) -> str | None:
    """A version of a nickname a TTS voice can pronounce, or None when it would be spelled out letter by letter
    (no vowels, long consonant runs, keyboard mashing). Trailing digits and underscores are dropped."""
    n = str(name or "").strip().strip("_-.").rstrip("0123456789_-")
    letters = [ch for ch in n if ch.isalpha()]
    if len(letters) < 2:
        return None
    vowels = sum(1 for ch in letters if ch.lower() in "aeiouy")
    run, worst = 0, 0
    for ch in letters:
        run = 0 if ch.lower() in "aeiouy" else run + 1
        worst = max(worst, run)
    if vowels / len(letters) < 0.2 or worst >= 5 or len(letters) > 14:
        return None
    return n


def for_speech(text: str) -> str:
    """What the voice gets: mixed-case words (vALenk, InvviS, LufDomingo) spelled letter by letter by a TTS engine
    become one lowercase word; all-caps shouts (ACE, CLUTCHES) are left alone."""
    out = []
    for w in text.split(" "):
        core = w.strip("!?.,;:'\"")
        if len(core) > 2 and any(ch.isupper() for ch in core[1:]) and any(ch.islower() for ch in core):
            w = w.replace(core, core[0] + core[1:].lower())
        out.append(w)
    return " ".join(out)


def spoken_names(meta_players: list[dict]) -> dict[str, str | None]:
    """steam64 -> what the casters call the player: players.json "say" or "name" if set, else the in-game name
    when speakable, else None (templates then say "our man" / skip the name)."""
    try:
        reg = json.loads((DATA_DIR / "players.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        reg = {}
    out = {}
    for pl in meta_players or []:
        sid = str(pl.get("steamid") or "")
        entry = reg.get(sid) if isinstance(reg, dict) else None
        say = entry.get("say") if isinstance(entry, dict) else None
        nick = entry.get("name") if isinstance(entry, dict) else None
        out[sid] = str(say).strip() if say else speakable(nick or pl.get("name") or "")
    return out


# ---- timeline ---------------------------------------------------------------------------------------

def timeline(events: dict, meta: dict, highlight: dict, clip_start_tick: int, clip_end_tick: int) -> dict:
    """Events of the clip's round with alive counts. Kills are tagged with whether the highlight player did them."""
    tr = int(meta.get("tickrate", 64))
    rnd = int(highlight.get("round") or 0)
    sid = str(highlight.get("player") or "")
    rounds = {r["num"]: r for r in meta.get("rounds", [])}
    r = rounds.get(rnd) or {}
    teams = r.get("teams") or {}
    my_team = teams.get(sid, "")
    alive = {"T": sum(1 for t in teams.values() if t == "T"), "CT": sum(1 for t in teams.values() if t == "CT")}
    say = spoken_names(meta.get("players") or [])
    kills = sorted((k for k in events.get("kills", []) if k.get("round") == rnd), key=lambda k: k["tick"])
    out_kills = []
    for k in kills:
        vt = k.get("victim_team") or teams.get(str(k.get("victim")), "")
        if vt in alive and alive[vt] > 0:
            alive[vt] -= 1
        if clip_start_tick <= k["tick"] <= clip_end_tick:
            out_kills.append({"t": (k["tick"] - clip_start_tick) / tr, "mine": str(k.get("attacker")) == sid,
                              "attacker": say.get(str(k.get("attacker")), k.get("attacker_name")),
                              "victim": say.get(str(k.get("victim")), speakable(k.get("victim_name") or "")),
                              "weapon": k.get("weapon") or "", "headshot": bool(k.get("headshot")),
                              "wallbang": bool(k.get("penetrated")), "smoke": bool(k.get("thrusmoke")),
                              "noscope": bool(k.get("noscope")), "blind": bool(k.get("attackerblind")),
                              "alive": dict(alive), "team": my_team, "victim_team": vt})
    bombs = [{"t": (b["tick"] - clip_start_tick) / tr, "type": b["type"], "site": b.get("site"), "mine": str(b.get("player")) == sid}
             for b in events.get("bomb", []) if b.get("round") == rnd and clip_start_tick <= b["tick"] <= clip_end_tick
             and b["type"] in ("planted", "begindefuse")]
    score = meta.get("score") or {}
    player = say.get(sid, speakable(highlight.get("player_name") or ""))
    return {"kills": out_kills, "bombs": bombs, "team": my_team, "round": rnd, "player": player,
            "type": str(highlight.get("type") or ""), "n": int(highlight.get("n") or len(highlight.get("kills") or []) or 0),
            "clip_seconds": (clip_end_tick - clip_start_tick) / tr}


# ---- script -----------------------------------------------------------------------------------------

def _fmt(template: str, ctx: dict) -> str:
    """Fill a template; a missing player name becomes "our man" (capitalised at a sentence start)."""
    c = dict(ctx)
    for key in ("player", "attacker"):
        if not c.get(key):
            c[key] = "our man"
    text = template.format(**c)
    out, cap = [], True
    for ch in text:
        out.append(ch.upper() if cap and ch.isalpha() else ch)
        if ch.isalpha():
            cap = False
        elif ch in ".!?":
            cap = True
    return "".join(out)


def _wclass(w: str) -> str:
    if w in SNIPERS:
        return "sniper"
    if w in PISTOLS:
        return "pistol"
    if w in KNIVES:
        return "knife"
    if w in NADES:
        return "nade"
    return "rifle"


INTRO = {
    "clutch": ["{player} is the last one standing. One against {n}, round {round}. Can he do it?",
               "Round {round}. {player} alone against {n}. Here we go.",
               "It's a one v {n} for {player}. Everybody hold your breath."],
    "multikill": ["Round {round}, and {player} is about to go to work.",
                  "Watch {player} here in round {round}.",
                  "Here comes {player}, round {round}."],
    "ace": ["Round {round}. Keep your eyes on {player}.",
            "{player} in round {round}. Remember this one."],
    "knife": ["Round {round}. {player} has an idea, and it's a bad one for the enemy.",
              "Look at {player} here. The knife is out."],
    "ninja_defuse": ["Round {round}. Bomb's down, and {player} is sneaking in.",
                     "Nobody's watching the bomb. {player} sees it."],
    "kill": ["Round {round}, {player}.", "Watch this from {player}."],
}
KILL_FIRST = {
    "sniper": ["{weapon} shot, {victim} is down!", "Big {weapon} hit on {victim}!", "{victim} falls to the {weapon}!"],
    "rifle": ["{attacker} takes {victim}!", "{victim} is gone!", "First one, {victim} down!", "{attacker} finds {victim}!"],
    "pistol": ["With the pistol! {victim} is down!", "{attacker} wins the pistol duel against {victim}!"],
    "knife": ["WITH THE KNIFE! {victim} gets knifed!", "Oh no, {victim} just got knifed by {attacker}!"],
    "nade": ["The grenade finishes {victim}!", "{victim} goes down to the nade!"],
}
KILL_MORE = {
    "sniper": ["Another one! {victim} with the {weapon}!", "{weapon} again, {victim} is down!", "He's lining them up! {victim}!"],
    "rifle": ["And {victim}! Another one!", "{victim} too! He's on fire!", "That's {count}! {victim} is gone!", "He turns, {victim} is down!"],
    "pistol": ["Pistol again! {victim}!", "{victim} with the pistol! Unbelievable!"],
    "knife": ["KNIFE AGAIN! {victim}!", "Are you kidding me, {victim} knifed as well!"],
    "nade": ["The nade gets {victim} too!", "{victim} cooked by the grenade!"],
}
INTRO_SHORT = {"clutch": ["One against {n}. Can he do it?", "{player}, alone against {n}."],
               "multikill": ["Round {round}, {player}.", "Watch {player}."], "ace": ["Round {round}. Watch {player}."],
               "knife": ["Knife's out."], "ninja_defuse": ["Nobody's watching the bomb."], "kill": ["Round {round}."]}
FINAL_SHORT = {"clutch": ["HE'S DONE IT!", "CLUTCH!", "What a clutch!"], "ace": ["ACE!", "ACE! Five for five!"],
               "multikill": ["{count} for {player}!", "That's {count}!"], "knife": ["Knife kill!"],
               "ninja_defuse": ["Ninja defuse!"], "kill": ["What a kill!"]}
KILL_FIRST_NOV = ["First one down!", "One down!", "{attacker} opens it up!", "Got him! First blood!"]
KILL_MORE_NOV = ["Another one!", "That's {count}!", "And another! He's on fire!", "Down goes {count}!"]
MODS = {"headshot": ["Headshot!", "Right in the head!", "One tap!"],
        "smoke": ["Through the smoke!", "He couldn't even see him!"],
        "wallbang": ["Through the wall!", "Wallbang!"],
        "noscope": ["No scope!", "Didn't even scope in!"],
        "blind": ["He was flashed!", "Blind!"]}
LEFT = {3: ["Three more to go.", "Three left."], 2: ["Two to go.", "Two left, he's got a chance here."],
        1: ["One left. One more!", "Just one more!", "Last one!"]}
FINAL = {
    "clutch": ["HE'S DONE IT! The clutch is complete!", "ARE YOU KIDDING ME?! {player} wins it alone!",
               "{player} CLUTCHES IT! One against {n} and he takes them all!", "What a clutch! Round over!"],
    "ace": ["ACE! {player} takes all five!", "FIVE FOR FIVE! It's an ace for {player}!", "ACE! What is happening!"],
    "multikill": ["{count} for {player}! What a round!", "That's {count} kills! Absolute scenes!",
                  "{player} just dropped {count}! Look at that!"],
    "knife": ["Humiliation! The knife kill from {player}!", "Oh the disrespect! Knife kill!"],
    "ninja_defuse": ["NINJA DEFUSE! They never saw him!", "Defused! Right under their noses!"],
    "kill": ["What a kill from {player}!", "{player} takes it!"],
}
FILLER = ["{alive_enemy} still out there.", "He's got to find {alive_enemy} more.", "Taking his time. Smart.",
          "The whole team is watching this.", "Stay calm, {player}."]
BOMB = {"planted": ["Bomb is down! Now they have to come to him.", "The bomb is planted. Pressure is on the defenders."],
        "begindefuse": ["He's going for the defuse!", "Sticking the defuse!"],
        "enemy_defuse": ["They're on the bomb! He has to go now!", "Defuse started! Where is he?!"]}

NUM = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}

# the second caster (colour commentary) reacts after the play-by-play caller
REACT_KILL = ["That's a big one.", "Clean.", "He's not even blinking.", "Look at the composure.", "Oh, that's filthy!",
              "The crosshair placement there, perfect.", "Didn't hesitate for a second."]
REACT_FINAL = ["What a round! What a player!", "I don't believe what I just saw.", "Absolute scenes!",
               "That is going straight to the highlight reel.", "Unreal. Simply unreal."]
REACT_INTRO = ["Tough spot.", "Not many people pull this off.", "Let's see what he's got."]


def write_script(tl: dict, rng: random.Random | None = None, duo: bool = True) -> list[dict]:
    """Lines {t, text, kind, priority, who} in clip time. priority: 3 must keep, 2 nice, 1 drop first.
    who: 1 = play-by-play caster, 2 = colour caster (reactions; only with duo=True)."""
    rng = rng or random.Random()
    lines = _write_calls(tl, rng)
    if duo:
        extra = []
        pools = {k: rng.sample(v, len(v)) for k, v in (("kill", REACT_KILL), ("final", REACT_FINAL), ("intro", REACT_INTRO))}

        def react(kind: str) -> str:   # no repeats within one clip
            pool = pools[kind]
            return pool.pop() if pool else rng.choice({"kill": REACT_KILL, "final": REACT_FINAL, "intro": REACT_INTRO}[kind])
        for ln in lines:
            if ln["kind"] == "intro" and rng.random() < 0.7:
                extra.append({"t": ln["t"] + 3.2, "text": react("intro"), "kind": "react", "priority": 1, "who": 2})
            elif ln["kind"] == "kill" and rng.random() < 0.75:
                extra.append({"t": ln["t"] + 2.4, "text": react("kill"), "kind": "react", "priority": 2, "who": 2})
            elif ln["kind"] == "final":
                extra.append({"t": ln["t"] + 2.8, "text": react("final"), "kind": "react", "priority": 2, "who": 2})
            elif ln["kind"] == "filler":
                ln["who"] = 2   # the colour caster takes the quiet moments
        lines = sorted(lines + extra, key=lambda l: l["t"])
    return lines


class _Picker:
    """rng.choice that avoids repeating a phrase within one script while alternatives remain."""

    def __init__(self, rng: random.Random):
        self.rng, self.used = rng, set()

    def choice(self, options: list[str]) -> str:
        fresh = [o for o in options if o not in self.used] or list(options)
        pick = self.rng.choice(fresh)
        self.used.add(pick)
        return pick

    def random(self) -> float:
        return self.rng.random()


def _write_calls(tl: dict, rng: random.Random) -> list[dict]:
    """The play-by-play lines (who = 1)."""
    rng = _Picker(rng)   # .choice -> no repeats; .random still needed below
    typ = tl["type"] if tl["type"] in INTRO else ("ace" if tl["n"] >= 5 else "multikill" if tl["n"] >= 2 else "kill")
    ctx = {"player": tl["player"], "n": tl["n"], "round": tl["round"]}
    lines = [{"t": 0.0, "text": _fmt(rng.choice(INTRO[typ]), ctx), "kind": "intro", "priority": 2,
              "short": _fmt(rng.choice(INTRO_SHORT.get(typ, INTRO_SHORT["kill"])), ctx)}]
    mine = [k for k in tl["kills"] if k["mine"]]
    for b in tl["bombs"]:
        if b["mine"] or b["type"] == "planted":
            lines.append({"t": b["t"] + 0.2, "text": rng.choice(BOMB[b["type"]]), "kind": "bomb", "priority": 1})
        elif b["type"] == "begindefuse" and tl["team"] == "T":
            lines.append({"t": b["t"] + 0.1, "text": rng.choice(BOMB["enemy_defuse"]), "kind": "bomb", "priority": 2})
    for i, k in enumerate(mine, start=1):
        wc = _wclass(k["weapon"])
        c = {**ctx, "victim": k["victim"], "attacker": tl["player"], "weapon": weapon_name(k["weapon"]), "count": NUM.get(i, str(i))}
        last = i == len(mine)
        short = None
        if last and typ in FINAL and len(mine) >= 2:
            text = _fmt(rng.choice(FINAL[typ]), {**c, "count": NUM.get(len(mine), str(len(mine)))})
            short = _fmt(rng.choice(FINAL_SHORT.get(typ, FINAL_SHORT["kill"])), c)
            kind, prio = "final", 3
        else:
            if k["victim"]:
                text = _fmt(rng.choice(KILL_FIRST[wc] if i == 1 else KILL_MORE[wc]), c)
            else:
                text = _fmt(rng.choice(KILL_FIRST_NOV if i == 1 else KILL_MORE_NOV), c)
            short = text     # the bare call; the extras below only go on the long form
            mods = [m for m in ("headshot", "smoke", "wallbang", "noscope", "blind") if k.get(m)]
            if mods:
                text += " " + rng.choice(MODS[mods[0]])
            enemy_left = k["alive"].get("CT" if tl["team"] == "T" else "T", 0)
            if not last and typ == "clutch" and enemy_left in LEFT and rng.random() < 0.8:
                text += " " + rng.choice(LEFT[enemy_left])
            kind, prio = "kill", 3
        lines.append({"t": k["t"] + 0.25, "text": text, "kind": kind, "priority": prio, "short": short if short != text else None})
        if last and typ in FINAL and len(mine) < 2:
            lines.append({"t": k["t"] + 2.2, "text": _fmt(rng.choice(FINAL[typ]), c), "kind": "final", "priority": 2})
    # a filler in long quiet stretches between kills (the clip may fast-forward them; schedule() sorts that out)
    for a, b in zip(mine, mine[1:]):
        if b["t"] - a["t"] > 9 and typ == "clutch":
            enemy_left = a["alive"].get("CT" if tl["team"] == "T" else "T", 0)
            lines.append({"t": a["t"] + 4.5, "text": _fmt(rng.choice(FILLER), {**ctx, "alive_enemy": NUM.get(enemy_left, str(enemy_left))}),
                          "kind": "filler", "priority": 1})
    for ln in lines:
        ln.setdefault("who", 1)
    lines.sort(key=lambda l: l["t"])
    return lines


# ---- speaking and scheduling --------------------------------------------------------------------------

COMMENTARY_RATE = "+25%"   # casters talk fast


def tts_cached(text: str, voice_id: str, ffmpeg: Path | None, fx: bool, rate: str = COMMENTARY_RATE) -> Path:
    text = for_speech(text)
    TTS_CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{voice_id}|{int(fx)}|{rate}|{text}".encode("utf-8")).hexdigest()[:16]
    for ext in (".mp3", ".wav"):
        p = TTS_CACHE / f"{key}{ext}"
        if p.is_file():
            return p
    tmp_slot = TTS_CACHE / "_tmp"
    tmp_slot.mkdir(exist_ok=True)
    # generate through voice.tts into a throwaway slot dir, then move the file into the cache
    orig = voice.VOICE_DIR
    try:
        voice.VOICE_DIR = tmp_slot
        p = voice.tts(text, "final", sub="", voice_id=voice_id, ffmpeg=ffmpeg, fx=fx, edge_rate=rate)
    finally:
        voice.VOICE_DIR = orig
    dst = TTS_CACHE / f"{key}{p.suffix}"
    p.replace(dst)
    return dst


def schedule(lines: list[dict], durations: list[float], out_time, total: float, gap: float = 0.25,
             alt_durations: list[float | None] | None = None) -> list[tuple[int, float, bool]]:
    """(line index, output start, use_short) for the lines that fit. Each line starts at its clip time mapped to
    output time and is delayed while the previous line is still talking. When the full line does not fit, the
    short form is tried (alt_durations); when that does not fit either, the previous low-priority line is dropped
    to make room for a priority-3 call, or the line itself is dropped. Lines never overlap."""
    order = sorted(range(len(lines)), key=lambda i: lines[i]["t"])
    placed: list[tuple[int, float, bool]] = []
    alt = alt_durations or [None] * len(lines)
    # how late a line may come after its moment: a call must be prompt, a reaction can trail the call
    slack = {"react": 5.0, "filler": 3.0, "bomb": 2.0, "final": 2.5, "intro": 1.0}

    def dur(i: int, short: bool) -> float:
        return alt[i] if (short and alt[i] is not None) else durations[i]

    def end_of(p: tuple[int, float, bool]) -> float:
        return p[1] + dur(p[0], p[2])

    def next_want(i: int) -> float | None:
        later = [out_time(lines[j]["t"]) for j in order if lines[j]["t"] > lines[i]["t"] and lines[j]["priority"] >= 3]
        return min(later) if later else None

    for i in order:
        want = out_time(lines[i]["t"])
        late = slack.get(lines[i].get("kind", ""), 1.5)
        nxt = next_want(i)
        must = lines[i]["priority"] >= 3

        # closing lines may run past the end: the clip is extended (or the last frame held) to let them finish
        over = 3.5 if lines[i].get("kind") in ("final", "react") else 1.2

        def fits(start: float, short: bool, strict: bool) -> bool:
            end = start + dur(i, short)
            if start - want > late or end > total + over:
                return False
            # strict: do not run into the next call so much that it would come late
            return not (strict and nxt is not None and end + gap > nxt + 1.5)

        def best(start: float) -> bool | None:
            # the full line whenever it fits before the next call, else the short form; a call that must be heard
            # may also run into the next call (which then comes a little late)
            tries = [(False, True), (True, True)] + ([(True, False), (False, False)] if must else [])
            for short, strict in tries:
                if short and alt[i] is None:
                    continue
                if fits(start, short, strict):
                    return short
            return None
        end_prev = end_of(placed[-1]) if placed else -1.0
        choice = best(max(want, end_prev + gap))
        if choice is None and must and placed and lines[placed[-1][0]]["priority"] < 3:
            placed.pop()                       # make room: drop the previous low-priority line
            end_prev = end_of(placed[-1]) if placed else -1.0
            choice = best(max(want, end_prev + gap))
        if choice is None:
            continue
        placed.append((i, max(want, end_prev + gap), choice))
    return placed


def talk_windows(lines: list[dict], durations: list[float], gap: float = 0.25, min_priority: int = 2) -> list[tuple[float, float]]:
    """Clip-time spans the casters need at normal speed to say their lines in full, one after another. The clip
    keeps real footage there instead of cutting or fast-forwarding it, so the lines are heard whole rather than
    in their short forms. Lines below `min_priority` (fillers) get no room reserved."""
    out: list[tuple[float, float]] = []
    end = -1.0
    for i in sorted(range(len(lines)), key=lambda i: lines[i]["t"]):
        if int(lines[i].get("priority", 2)) < min_priority:
            continue
        start = max(float(lines[i]["t"]), end + gap)
        end = start + float(durations[i])
        out.append((float(lines[i]["t"]), end + gap))
    return out


def draft(demo_id: str, highlight: dict, clip_start_tick: int, clip_end_tick: int | None,
          rng: random.Random | None = None, duo: bool = True) -> list[dict]:
    """Write a fresh commentary for a highlight (no speech yet): lines {t, text, kind, priority, who}."""
    parsed = DATA_DIR / "parsed" / demo_id
    events = json.loads((parsed / "events.json").read_text(encoding="utf-8"))
    meta = json.loads((parsed / "meta.json").read_text(encoding="utf-8"))
    if not clip_end_tick:
        clip_end_tick = int(highlight.get("end_tick") or clip_start_tick + 60 * int(meta.get("tickrate", 64)))
    return write_script(timeline(events, meta, highlight, clip_start_tick, clip_end_tick), rng, duo=duo)


def clean_script(lines) -> list[dict]:
    """Validate a user-edited script: keep {t, text, kind, priority, who}, drop empty lines, sort by time."""
    out = []
    for ln in lines or []:
        if not isinstance(ln, dict):
            continue
        text = " ".join(str(ln.get("text") or "").split())[:300]
        try:
            t = max(0.0, float(ln.get("t") or 0))
        except (TypeError, ValueError):
            continue
        if text:
            kind = str(ln.get("kind") or "line")
            prio = int(ln.get("priority") or (3 if kind in ("kill", "final") else 2))
            who = 2 if str(ln.get("who")) == "2" else 1
            short = " ".join(str(ln.get("short") or "").split())[:300] or None
            out.append({"t": round(t, 2), "text": text, "kind": kind, "priority": max(1, min(3, prio)), "who": who,
                        "short": short if short and short != text else None})
    return sorted(out, key=lambda l: l["t"])


def plan(demo_id: str, highlight: dict, clip_start_tick: int, clip_end_tick: int | None, *, voice_id: str,
         ffmpeg: Path | None, ffprobe: Path, fx: bool = True, final_real: Path | None = None,
         script: list[dict] | None = None, voice2_id: str = "") -> dict:
    """Write (or take the saved `script`) and speak the commentary once: {"lines", "files", "durs"}.
    Lines with who=2 use voice2_id (the colour caster); without a second voice the first voice speaks them.
    Schedule it with schedule_plan()."""
    from .cinematic import probe_duration
    lines = clean_script(script) if script else draft(demo_id, highlight, clip_start_tick, clip_end_tick, duo=bool(voice2_id))
    voice_id = voice_id or voice.TTS_VOICES[0]["id"]
    if voice2_id == voice_id:
        voice2_id = voice.other_voice(voice_id)   # two casters with one voice sound like one caster
    files: list[Path] = []
    alts: list[Path | None] = []
    for ln in lines:
        vid = voice2_id if (ln.get("who") == 2 and voice2_id) else voice_id
        if ln["kind"] == "final" and final_real is not None:
            files.append(final_real)
            alts.append(None)
            ln["spoken"] = f"(real caster cut: {final_real.name})"
        else:
            files.append(tts_cached(ln["text"], vid, ffmpeg, fx))
            alts.append(tts_cached(ln["short"], vid, ffmpeg, fx) if ln.get("short") else None)
    return {"lines": lines, "files": files, "durs": [probe_duration(ffprobe, f) for f in files],
            "alts": alts, "alt_durs": [probe_duration(ffprobe, f) if f else None for f in alts],
            "voices": [voice_id, voice2_id]}


def schedule_plan(pl: dict, out_time, total: float) -> tuple[list[tuple[Path, float]], list[str]]:
    """[(file, output time)] of the lines that fit (short forms where needed), and the texts actually spoken."""
    placed = schedule(pl["lines"], pl["durs"], out_time, total, alt_durations=pl.get("alt_durs"))
    out, texts = [], []
    for i, at, short in placed:
        f = pl["alts"][i] if (short and pl.get("alts") and pl["alts"][i]) else pl["files"][i]
        out.append((f, at))
        texts.append(pl["lines"][i]["short"] if short else pl["lines"][i]["text"])
    return out, texts
