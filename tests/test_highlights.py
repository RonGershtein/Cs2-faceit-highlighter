from backend.highlights import detect

T = 64


def rnd(num, start, end, winner, teams):
    return {"num": num, "start": start, "freeze_end": start + 15 * T, "end": end, "official_end": end + 7 * T,
            "winner": winner, "teams": teams}


TEAMS = {f"ct{i}": "CT" for i in range(1, 6)} | {f"t{i}": "T" for i in range(1, 6)}


def kill(tick, a, v, rnum=1, **kw):
    k = {"tick": tick, "round": rnum, "attacker": a, "victim": v, "attacker_team": TEAMS.get(a),
         "victim_team": TEAMS.get(v), "weapon": "ak47", "headshot": False, "penetrated": 0, "noscope": False,
         "thrusmoke": False, "attackerblind": False, "assister": None, "attacker_name": a, "victim_name": v}
    k.update(kw)
    return k


def types(hs, t):
    return [h for h in hs if h["type"] == t]


def test_multikill_chain_and_window():
    rounds = [rnd(1, 1000, 9000, "CT", TEAMS)]
    kills = [kill(2000, "ct1", "t1"), kill(2500, "ct1", "t2"), kill(2600, "ct1", "t3"),
             kill(2600 + 20 * T, "ct1", "t4")]  # last one 20 s later -> separate chain of 1
    hs = detect(kills, rounds)
    mk = types(hs, "multikill")
    assert len(mk) == 1 and mk[0]["n"] == 3 and mk[0]["kills"] == [2000, 2500, 2600]
    assert mk[0]["start_tick"] == 2000 - 4 * T and mk[0]["end_tick"] == 2600 + 2 * T
    assert mk[0]["score"] == 4
    assert len(types(hs, "kill")) == 4


def test_ace_overrides_chain_gap():
    rounds = [rnd(1, 1000, 20000, "T", TEAMS)]
    kills = [kill(2000 + i * 20 * T, "t1", f"ct{i + 1}") for i in range(5)]
    hs = detect(kills, rounds)
    aces = types(hs, "ace")
    assert len(aces) == 1 and aces[0]["n"] == 5 and aces[0]["score"] >= 12
    assert not types(hs, "multikill")


def test_teamkill_ignored():
    rounds = [rnd(1, 1000, 9000, "CT", TEAMS)]
    kills = [kill(2000, "ct1", "ct2"), kill(2100, "ct1", "t1")]
    hs = detect(kills, rounds)
    assert len(types(hs, "kill")) == 1
    assert not types(hs, "multikill")


def test_clutch_1v3_winner_only():
    rounds = [rnd(1, 1000, 9000, "CT", TEAMS)]
    kills = [kill(2000, "t1", "ct1"), kill(2100, "t1", "ct2"), kill(2200, "t2", "ct3"), kill(2300, "t3", "ct4"),
             kill(3000, "ct5", "t1"), kill(3200, "ct5", "t2"), kill(3300, "ct5", "t3"), kill(3400, "ct5", "t4"),
             kill(3500, "ct5", "t5")]
    hs = detect(kills, rounds)
    cl = types(hs, "clutch")
    assert len(cl) == 1
    c = cl[0]
    assert c["player"] == "ct5" and c["n"] == 5
    assert c["description"].startswith("1v5 clutch by ct5")
    assert c["kills"] == [3000, 3200, 3300, 3400, 3500]
    # ace in the same window is merged into the clutch
    assert not types(hs, "ace")
    assert "ACE" in c["description"]


def test_clutch_lost_not_counted():
    rounds = [rnd(1, 1000, 9000, "T", TEAMS)]
    kills = [kill(2000, "t1", "ct1"), kill(2100, "t1", "ct2"), kill(2200, "t2", "ct3"), kill(2300, "t3", "ct4"),
             kill(3000, "ct5", "t1"), kill(3200, "t2", "ct5")]
    hs = detect(kills, rounds)
    assert not types(hs, "clutch")


def test_kill_flags_and_knife():
    rounds = [rnd(1, 1000, 9000, "CT", TEAMS)]
    kills = [kill(2000, "ct1", "t1", weapon="knife"), kill(2500, "ct2", "t2", headshot=True, penetrated=1, noscope=True)]
    hs = detect(kills, rounds)
    knife = types(hs, "knife")
    assert len(knife) == 1 and knife[0]["score"] == 4
    k = [h for h in types(hs, "kill") if h["player"] == "ct2"][0]
    assert k["score"] == 4 and set(k["tags"]) == {"headshot", "wallbang", "noscope"}


def test_ninja_defuse():
    rounds = [rnd(1, 1000, 9000, "CT", TEAMS)]
    kills = [kill(2000, "t1", "ct1")]
    bomb = [{"type": "planted", "tick": 2500, "round": 1, "player": "t1"},
            {"type": "begindefuse", "tick": 3000, "round": 1, "player": "ct2"},
            {"type": "defused", "tick": 3500, "round": 1, "player": "ct2", "player_name": "ct2"}]
    positions = {"ct2": (0, 0, 0), "t1": (2000, 0, 0), "t2": (3000, 0, 0), "t3": (3000, 0, 0), "t4": (3000, 0, 0),
                 "t5": (3000, 0, 0)}

    def pos_at(sid, tick):
        return positions.get(sid)

    hs = detect(kills, rounds, bomb, pos_at)
    nd = types(hs, "ninja_defuse")
    assert len(nd) == 1 and nd[0]["player"] == "ct2" and nd[0]["n"] == 5
    # enemy close -> no ninja
    positions["t1"] = (100, 100, 0)
    assert not types(detect(kills, rounds, bomb, pos_at), "ninja_defuse")


def test_sorted_by_score_with_ids():
    rounds = [rnd(1, 1000, 9000, "CT", TEAMS)]
    kills = [kill(2000, "ct1", "t1"), kill(2100, "ct1", "t2"), kill(5000, "ct2", "t3")]
    hs = detect(kills, rounds)
    assert [h["id"] for h in hs] == list(range(len(hs)))
    assert all(hs[i]["score"] >= hs[i + 1]["score"] for i in range(len(hs) - 1))
