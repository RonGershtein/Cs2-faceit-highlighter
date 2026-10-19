import random

from backend import caster


def _events():
    kills = [
        {"tick": 1100, "round": 8, "attacker": "e1", "victim": "t1", "attacker_name": "Enzo", "victim_name": "Tomas", "attacker_team": "CT", "victim_team": "T", "weapon": "ak47"},
        {"tick": 1500, "round": 8, "attacker": "me", "victim": "e1", "attacker_name": "Me", "victim_name": "Enzo", "attacker_team": "T", "victim_team": "CT", "weapon": "awp", "headshot": True, "thrusmoke": True},
        {"tick": 1900, "round": 8, "attacker": "me", "victim": "e2", "attacker_name": "Me", "victim_name": "Evan", "attacker_team": "T", "victim_team": "CT", "weapon": "awp"},
        {"tick": 2600, "round": 8, "attacker": "me", "victim": "e3", "attacker_name": "Me", "victim_name": "Erik", "attacker_team": "T", "victim_team": "CT", "weapon": "knife"},
        {"tick": 9000, "round": 9, "attacker": "me", "victim": "e3", "attacker_name": "Me", "victim_name": "Erik", "attacker_team": "T", "victim_team": "CT", "weapon": "ak47"},
    ]
    bomb = [{"type": "planted", "tick": 1300, "round": 8, "player": "me"}, {"type": "begindefuse", "tick": 2400, "round": 8, "player": "e3"}]
    return {"kills": kills, "bomb": bomb}


def _meta():
    return {"tickrate": 64, "rounds": [{"num": 8, "teams": {"me": "T", "t1": "T", "e1": "CT", "e2": "CT", "e3": "CT"}}]}


HL = {"player": "me", "player_name": "Mike", "type": "clutch", "n": 3, "round": 8}


def test_timeline_counts_alive_and_tags_my_kills():
    tl = caster.timeline(_events(), _meta(), HL, 1000, 3000)
    assert [k["mine"] for k in tl["kills"]] == [False, True, True, True]
    assert tl["kills"][0]["alive"] == {"T": 1, "CT": 3} and tl["kills"][-1]["alive"] == {"T": 1, "CT": 0}
    assert [round(k["t"], 4) for k in tl["kills"]] == [1.5625, 7.8125, 14.0625, 25.0]
    assert [b["type"] for b in tl["bombs"]] == ["planted", "begindefuse"] and tl["team"] == "T"


def test_script_covers_the_story_with_two_casters():
    tl = caster.timeline(_events(), _meta(), HL, 1000, 3000)
    lines = caster.write_script(tl, random.Random(3))
    kinds = [l["kind"] for l in lines]
    assert kinds[0] == "intro" and "final" in kinds and kinds.count("kill") == 2 and "bomb" in kinds
    assert {l["who"] for l in lines} == {1, 2} and all(l["who"] == 1 for l in lines if l["kind"] in ("kill", "final", "intro"))
    reacts = [l["text"] for l in lines if l["kind"] == "react"]
    assert len(reacts) == len(set(reacts))                       # no repeated reaction in one clip
    texts = " ".join(l["text"] for l in lines)
    assert "Enzo" in texts and "Evan" in texts
    assert all(l["t"] <= m["t"] for l, m in zip(lines, lines[1:]))
    solo = caster.write_script(tl, random.Random(3), duo=False)
    assert all(l["who"] == 1 for l in solo)


def test_clean_script_validates_user_edits():
    out = caster.clean_script([{"t": "4.5", "text": "  hello   there ", "kind": "kill"}, {"t": 1, "text": ""}, "junk",
                               {"t": 2, "text": "x", "who": "2", "priority": 9}])
    assert out == [{"t": 2.0, "text": "x", "kind": "line", "priority": 3, "who": 2, "short": None},
                   {"t": 4.5, "text": "hello there", "kind": "kill", "priority": 3, "who": 1, "short": None}]


def test_schedule_avoids_overlap_and_drops_low_priority():
    lines = [{"t": 0.0, "priority": 2}, {"t": 1.0, "priority": 1}, {"t": 1.2, "priority": 3}, {"t": 9.0, "priority": 3}]
    durs = [2.0, 1.5, 1.0, 3.0]
    placed = caster.schedule(lines, durs, lambda t: t, total=12.0)
    idx = [i for i, _, _ in placed]
    assert 2 in idx and 3 in idx and 1 not in idx          # the filler is dropped to make room for the kill line
    starts = {i: at for i, at, _ in placed}
    assert starts[2] >= 2.25 and starts[3] == 9.0
    placed = caster.schedule([{"t": 0.0, "priority": 3}, {"t": 0.5, "priority": 3}], [5.0, 1.0], lambda t: t, total=10.0)
    assert [i for i, _, _ in placed] == [0]                # a call that would come far too late is dropped


def test_schedule_prefers_the_short_form_over_dropping():
    lines = [{"t": 0.0, "priority": 3, "kind": "kill"}, {"t": 3.0, "priority": 3, "kind": "kill"}]
    placed = caster.schedule(lines, [6.0, 2.0], lambda t: t, total=20.0, alt_durations=[2.5, None])
    assert placed == [(0, 0.0, True), (1, 3.0, False)]     # long first call would delay the second too much -> short form


def test_speakable_names():
    assert caster.speakable("qwqwqwqwwqwq") is None          # no vowels -> spelled out by a voice
    assert caster.speakable("Sniiitch_") == "Sniiitch"
    assert caster.speakable("vALenk") == "vALenk" and caster.for_speech("vALenk takes it! ACE!") == "valenk takes it! ACE!"
    assert caster.for_speech("And LufDomingo! One tap!") == "And Lufdomingo! One tap!"
    assert caster.speakable("s1mple") == "s1mple" and caster.speakable("holyshnizel") == "holyshnizel"
    assert caster.speakable("xXx_Pr0_xXx") is None and caster.speakable("Devqin") == "Devqin"


def test_script_without_a_speakable_name_uses_our_man():
    hl = dict(HL, player_name="qwqwqwqwwqwq")
    ev = _events()
    for k in ev["kills"]:
        if k["attacker"] == "me":
            k["attacker_name"] = "qwqwqwqwwqwq"
        if k["victim"] == "e2":
            k["victim_name"] = "xxxxxxxx"
    tl = caster.timeline(ev, _meta(), hl, 1000, 3000)
    assert tl["player"] is None and tl["kills"][2]["victim"] is None
    text = " ".join(l["text"] for l in caster.write_script(tl, random.Random(1)))
    assert "qwqwqwqwwqwq" not in text and "xxxxxxxx" not in text and "our man" in text.lower()
    assert "None" not in text


def test_no_repeated_phrasing_in_one_script():
    ev = _events()
    ev["bomb"] += [{"type": "begindefuse", "tick": 2500, "round": 8, "player": "e2"}]
    tl = caster.timeline(ev, _meta(), HL, 1000, 3000)
    for seed in range(6):
        lines = [l["text"] for l in caster.write_script(tl, random.Random(seed)) if l["kind"] == "bomb"]
        assert len(lines) == len(set(lines)), lines


def test_schedule_prefers_the_full_line_when_it_fits():
    lines = [{"t": 0.0, "priority": 3, "kind": "kill"}, {"t": 8.0, "priority": 3, "kind": "kill"}]
    placed = caster.schedule(lines, [5.0, 2.0], lambda t: t, total=20.0, alt_durations=[2.0, None])
    assert placed == [(0, 0.0, False), (1, 8.0, False)]


def test_talk_windows_chain_the_lines_at_normal_speed():
    lines = [{"t": 0.0, "priority": 2}, {"t": 10.0, "priority": 3}, {"t": 11.0, "priority": 2}, {"t": 12.0, "priority": 1}]
    wins = caster.talk_windows(lines, [3.0, 4.0, 2.0, 9.0])
    assert wins == [(0.0, 3.25), (10.0, 14.25), (11.0, 16.5)]   # the reaction waits for the call; a filler reserves nothing
