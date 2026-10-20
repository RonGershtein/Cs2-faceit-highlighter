from pathlib import Path

import pytest

from backend import cinematic as c


def spans(segs):
    return [(round(a, 2), round(b, 2), round(s, 2)) for a, b, s in segs]


def test_keep_windows_merges_close_kills_and_cuts_dead_time():
    # kills 3 s apart stay in one window; a 9 s walk between kills is cut
    assert c.keep_windows([5.0, 8.0], 20.0, lead=2.0, tail=2.5) == [(3.0, 10.5)]
    assert c.keep_windows([2.0, 15.0], 20.0, lead=2.0, tail=2.5) == [(0.0, 4.5), (13.0, 17.5)]
    assert c.keep_windows([], 10.0) == [(0.0, 10.0)]


def test_speed_segments_cover_each_window_without_gaps():
    segs = c.speed_segments([11.0], [(0.0, 14.0)], 0.35, ramp=False)
    assert spans(segs) == [(0.0, 10.3, 1.0), (10.3, 11.5, 0.35), (11.5, 14.0, 1.0)]
    for (a, b, _), (a2, _, _) in zip(segs, segs[1:]):
        assert b == pytest.approx(a2)


def test_speed_segments_ramp_is_symmetric_and_between_1_and_slow():
    segs = c.speed_segments([11.0], [(0.0, 14.0)], 0.35, ramp=True)
    speeds = [s for _, _, s in segs]
    assert speeds[0] == 1.0 and speeds[-1] == 1.0
    ramp_in = speeds[1:4]
    ramp_out = speeds[5:8]
    assert ramp_in == sorted(ramp_in, reverse=True) and ramp_out == sorted(ramp_out)
    assert all(0.35 < s < 1.0 for s in ramp_in + ramp_out)
    assert speeds[4] == 0.35
    assert sum(b - a for a, b, _ in segs) == pytest.approx(14.0)


def test_speed_segments_ramps_share_a_short_gap():
    # two slow windows 0.4 s apart: each ramp gets at most half the gap, no overlap
    segs = c.speed_segments([5.0, 6.6], [(0.0, 10.0)], 0.35, ramp=True)
    for (a, b, _), (a2, _, _) in zip(segs, segs[1:]):
        assert a2 >= b - 1e-9
    assert sum(b - a for a, b, _ in segs) == pytest.approx(10.0)


def test_fill_gaps_fast_forwards_dead_time():
    keep = c.keep_windows([3.0, 11.0], 14.0, lead=2.0, tail=2.5)
    segs = c.fill_gaps(c.speed_segments([11.0], keep, 0.35), 14.0, 6.0)
    assert segs[0] == (0.0, 1.0, 6.0)
    assert (5.5, 9.0, 6.0) in segs
    assert segs[-1] == (13.5, 14.0, 6.0)
    assert sum(b - a for a, b, _ in segs) == pytest.approx(14.0)


def test_skip_waits_keeps_a_warp_on_each_side_of_a_long_wait():
    segs = [(0.0, 2.0, 1.0), (2.0, 102.0, 6.0), (102.0, 105.0, 1.0)]
    out = c.skip_waits(segs, 6.0)
    assert out[0] == (0.0, 2.0, 1.0) and out[-1] == (102.0, 105.0, 1.0)
    speeds = [s for _, _, s in out[1:-1]]
    assert speeds[0] == 6.0 and speeds[2] == c.WAIT_BURST and speeds[3] == c.WAIT_BURST and speeds[-1] == 6.0
    assert speeds[1] == pytest.approx((6.0 * c.WAIT_BURST) ** 0.5)
    assert out[1][0] == 2.0 and out[-2][1] == pytest.approx(102.0)            # the warps touch the action
    gaps = [(a, b2) for (_, b, _), (a, b2, _) in zip(out, out[1:]) if a > b + 0.01]
    assert len(gaps) == 1                                                     # one cut, in the middle of the wait
    waited = sum((b - a) / s for a, b, s in out[1:-1])
    assert waited == pytest.approx(2 * sum(c.WAIT_STEPS_OUT))                 # 100 s / 6 = 16.7 s -> 2.2 s


def test_skip_waits_leaves_short_waits_and_trims_the_edges_of_the_clip():
    short = [(0.0, 2.0, 1.0), (2.0, 14.0, 6.0), (14.0, 16.0, 1.0)]            # 2 s fast-forwarded: kept in full
    assert c.skip_waits(short, 6.0) == short
    lead = [(0.0, 60.0, 6.0), (60.0, 63.0, 1.0)]
    out = c.skip_waits(lead, 6.0)
    assert out[0][0] > 0 and out[-2][1] == pytest.approx(60.0) and out[-1] == (60.0, 63.0, 1.0)   # warp into the action
    tail = [(0.0, 3.0, 1.0), (3.0, 60.0, 6.0)]
    out = c.skip_waits(tail, 6.0)
    assert out[1][0] == 3.0 and out[-1][1] < 60.0 and out[-1][2] == c.WAIT_BURST                  # warp out, then the end
    assert c.skip_waits([(5.0, 1.0, 1.0)], 6.0) == [(5.0, 1.0, 1.0)]
    assert c.skip_waits([(1.0, 3.0, 2.0)], 6.0) == [(1.0, 3.0, 2.0)]          # not a fast-forward


def test_plan_clip_skips_waits_only_when_asked():
    opts = c.options({}, {"cine_trim": "fast", "cine_fast": 6.0})
    _, _, keep, _, plan = c.plan_clip([5.0, 110.0], 113.0, opts)
    on = c.total_duration(plan(keep))
    _, _, keep, _, plan = c.plan_clip([5.0, 110.0], 113.0, {**opts, "cine_skip_waits": False})
    off = c.total_duration(plan(keep))
    assert off - on > 12          # the 100 s wait: 16.7 s fast-forwarded vs a 2.2 s warp


def test_build_filter_flashes_over_a_cut():
    segs = [(0.0, 2.0, 1.0), (2.0, 5.0, 16.0), (90.0, 95.6, 16.0), (95.6, 98.0, 1.0)]
    g = c.build_filter(segs, fps=60, width=1280, height=720, smooth=False, kills=[], impact=False, look=False, fade=False,
                       has_music=False, game_vol=1, music_vol=1, texts=[])
    v1, v2 = g.split("[v1]")[0].split(";")[-1], g.split("[v2]")[0].split(";")[-1]
    assert "fade=t=out" in v1 and "color=white" in v1 and "fade=t=in" not in v1
    assert "fade=t=in:st=0" in v2 and "color=white" in v2 and "fade=t=out" not in v2
    assert g.count("color=white") == 2 and g.count("afade=t=out") == 1 and g.count("afade=t=in") == 1


def test_piece_cache_makes_once_and_waits_for_a_piece_being_made(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(c, "PIECE_CACHE", tmp_path)
    calls = []
    started, release = threading.Event(), threading.Event()

    def make(out):
        calls.append(out)
        started.set()
        release.wait(5)
        out.write_bytes(b"x" * 10)
        return {"seconds": 1.0}
    res = []
    t1 = threading.Thread(target=lambda: res.append(c.cached_piece("k1", make)))
    t1.start()
    started.wait(5)
    t2 = threading.Thread(target=lambda: res.append(c.cached_piece("k1", make)))
    t2.start()
    release.set()
    t1.join(5)
    t2.join(5)
    assert len(calls) == 1 and sorted(r[2] for r in res) == [False, True]
    assert all(r[0] == tmp_path / "k1.mp4" and r[1] == {"seconds": 1.0} for r in res)
    assert not (tmp_path / "k1.part.mp4").exists()


def test_piece_cache_failure_leaves_nothing_and_prune_drops_the_oldest(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(c, "PIECE_CACHE", tmp_path)

    def boom(out):
        out.write_bytes(b"half")
        raise RuntimeError("ffmpeg failed")
    with pytest.raises(RuntimeError):
        c.cached_piece("bad", boom)
    assert list(tmp_path.iterdir()) == []
    for i, name in enumerate(("old", "mid", "new")):
        (tmp_path / f"{name}.mp4").write_bytes(b"x" * 100)
        (tmp_path / f"{name}.json").write_text("{}")
        os.utime(tmp_path / f"{name}.mp4", (1000 + i, 1000 + i))
    assert c.prune_piece_cache(250) == 1
    assert sorted(p.name for p in tmp_path.glob("*.mp4")) == ["mid.mp4", "new.mp4"] and not (tmp_path / "old.json").exists()


def test_piece_key_follows_the_recording(tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"a")
    k1 = c.piece_key(src, {"kills": [1.0]})
    assert k1 == c.piece_key(src, {"kills": [1.0]}) and k1 != c.piece_key(src, {"kills": [2.0]})
    src.write_bytes(b"ab")    # re-recorded
    assert c.piece_key(src, {"kills": [1.0]}) != k1


def test_out_time_maps_through_cuts_and_speeds():
    segs = [(0.0, 2.0, 1.0), (4.0, 5.0, 0.5), (5.0, 7.0, 4.0)]
    assert c.out_time(segs, 1.0) == 1.0
    assert c.out_time(segs, 3.0) == 2.0          # inside the cut -> start of the next segment
    assert c.out_time(segs, 4.5) == pytest.approx(3.0)
    assert c.out_time(segs, 7.0) == pytest.approx(4.5)
    assert c.total_duration(segs) == pytest.approx(4.5)


def test_find_drop_prefers_the_rise_that_fits_the_clip():
    curve = [0.05] * 30 + [0.8] * 50       # quiet 15 s, loud from 15 s (0.5 s windows)
    assert c.find_drop(curve, 0.5, hit_time=4.0, clip_len=12.0) == 15.0
    # the drop must be at or after the kill time so the song can start at drop - hit
    assert c.find_drop(curve, 0.5, hit_time=20.0, clip_len=5.0) >= 20.0
    assert c.find_drop([], 0.5) == 0.0


def test_title_for():
    assert c.title_for({"type": "multikill", "n": 3, "player_name": "s1mple", "round": 12}, "de_mirage") == \
        ("TRIPLE KILL", "3K  ·  s1mple  ·  Mirage  ·  round 12")
    assert c.title_for({"type": "multikill", "n": 3, "player_name": "s1mple", "round": 12}, "de_mirage", with_player=False)[1] == \
        "3K  ·  Mirage  ·  round 12"
    assert c.title_for({"type": "ace", "n": 5, "player_name": "x", "round": 2}, "de_inferno")[0] == "ACE"
    assert c.title_for({"type": "clutch", "n": 3, "player_name": "x", "round": 2}, "")[0] == "1v3"         # no kills listed
    assert c.title_for({"type": "ninja_defuse", "player_name": "x"}, "de_nuke") == ("NINJA DEFUSE", "x  ·  Nuke")


def test_options_coerces_and_clamps():
    o = c.options({}, {"cine_slow": "0.05", "cine_trim": True, "cine_impact": "false", "cine_kills": "ALL", "bogus": 1})
    assert o["cine_slow"] == 0.1 and o["cine_trim"] == "cut" and o["cine_impact"] is False and o["cine_kills"] == "all"
    assert "bogus" not in o
    assert c.options({}, {"cine_trim": "fast"})["cine_trim"] == "fast"


def test_build_filter_structure(tmp_path):
    segs = [(0.0, 1.0, 6.0), (1.0, 3.0, 1.0), (3.0, 4.0, 0.35), (4.0, 5.0, 1.0)]
    f = tmp_path / "f.ttf"
    f.write_bytes(b"x")
    texts, files, card = c.plan_texts(segs, [3.4], width=1920, height=1080, title=("ACE", "x  ·  Mirage"), counter=True,
                                      font=f, tmp_base=tmp_path / "out.mp4")
    assert card is None
    g = c.build_filter(segs, fps=60, width=1920, height=1080, smooth=False, kills=[3.4], impact=True, look=True,
                       fade=True, has_music=True, game_vol=0.3, music_vol=0.7, texts=texts, voices=[0.3, 2.5])
    assert "concat=n=4:v=1:a=1" in g
    assert "atempo=2,atempo=2,atempo=1.5000" in g          # 6x as a chain of gentle steps
    assert "atempo=0.5,atempo=0.7000" in g                 # 0.35x
    assert g.count("zoompan=") == 1 and "s=1920x1080" in g  # impact only in the segment that has the kill
    assert "vignette" in g and "fade=t=in" in g and g.count("drawtext=") == 4
    assert "[2:a]adelay=300:all=1" in g and "[3:a]adelay=2500:all=1" in g
    assert "amix=inputs=4" in g and g.rstrip().endswith("[aout]")
    # a fast-forward drops the frames it does not show inside its own segment, before the join and the look
    assert "setpts=PTS/6.0000,fps=60[v0]" in g and ",fps=60[vc]" in g
    assert "vignette=angle=PI/4.6:dither=0" in g
    assert len(files) == 4 and all(p.exists() for p in files)
    assert files[0].read_text(encoding="utf-8") == "ACE"


def test_plan_texts_player_card_layout(tmp_path):
    f = tmp_path / "f.ttf"
    f.write_bytes(b"x")
    segs = [(0.0, 5.0, 1.0)]
    texts, files, card = c.plan_texts(segs, [], width=1920, height=1080, title=("1v3 CLUTCH", "Dust2  ·  round 8"),
                                      counter=False, font=f, tmp_base=tmp_path / "o.mp4", card={"name": "ron", "photo": None})
    assert card["d"] == 118 and card["idx"] is None and card["initial"].read_text(encoding="utf-8") == "R"
    assert [p.read_text(encoding="utf-8") for p in files] == ["ron", "Dust2  ·  round 8", "1v3 CLUTCH", "R"]
    assert int(texts[2]["y"]) < card["y"] < int(texts[0]["y"])          # big label above the card, name inside it
    g = c.build_filter(segs, fps=60, width=1920, height=1080, smooth=False, kills=[], impact=False, look=True, fade=False,
                       has_music=False, game_vol=1, music_vol=1, texts=texts, card=card)
    assert "color=c=0x2a2f3a:s=118x118" in g and "[ring][av]overlay=3:3" in g and "[vl][card]overlay=x=64:y=" in g
    assert g.count("geq=") == 2 and "[vcard]drawtext=" in g
    assert texts[0]["enable"] is None and texts[1]["enable"]               # the nickname stays, the details go
    assert "fade=t=out" not in g and "[card]overlay=x=64:y=" + str(card["y"]) + ":eof_action=pass[vcard]" in g   # card stays
    # with a photo the avatar comes from an input stream instead of a blank circle
    card["idx"], card["initial"] = 3, None
    g = c.build_filter(segs, fps=60, width=1920, height=1080, smooth=False, kills=[], impact=False, look=False, fade=False,
                       has_music=False, game_vol=1, music_vol=1, texts=texts, card=card)
    assert "[3:v]scale=118:118:force_original_aspect_ratio=increase,crop=118:118" in g and "[vc0][card]overlay" in g


def test_player_card_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "PLAYERS_DIR", tmp_path)
    monkeypatch.setattr(c, "PLAYERS_JSON", tmp_path / "players.json")
    h = {"player": "765", "player_name": "ingame"}
    assert c.player_card(h) == {"name": "ingame", "photo": None, "steamid": "765"}
    (tmp_path / "vALenk.png").write_bytes(b"x")
    assert c.player_card({"player": "1", "player_name": "valenk"})["photo"] == tmp_path / "vALenk.png"   # by nickname
    assert c.player_card({"player": "1", "player_name": "va_lenk!"})["photo"] == tmp_path / "vALenk.png"
    (tmp_path / "765.jpg").write_bytes(b"x")
    (tmp_path / "players.json").write_text('{"765": {"name": "Ron"}}', encoding="utf-8")
    assert c.player_card(h) == {"name": "Ron", "photo": tmp_path / "765.jpg", "steamid": "765"}   # steam id wins
    assert c.player_card(None) is None


def test_fesc_escapes_filtergraph_specials():
    assert c._fesc("C:\\Windows\\Fonts\\a.ttf") == "C\\\\:/Windows/Fonts/a.ttf"   # ':' survives both parsing stages
    assert c._fesc("a,b;c[d]") == r"a\,b\;c\[d\]"
    assert c._fesc("it's") == "it\\\\\\'s"


def test_voice_overrun_and_pad_in_filter(tmp_path):
    a, b = tmp_path / "a.mp3", tmp_path / "b.mp3"
    voices = [(a, 0.3), (b, 19.5)]
    assert c.voice_overrun(voices, {a: 1.0, b: 3.0}, 22.7) == pytest.approx(0.8)
    assert c.voice_overrun(voices, {a: 1.0, b: 1.0}, 22.7) < 0
    assert c.voice_overrun([], {}, 10.0) < 0
    segs = [(0.0, 5.0, 1.0)]
    g = c.build_filter(segs, fps=60, width=1280, height=720, smooth=False, kills=[], impact=False, look=False, fade=True,
                       has_music=False, game_vol=1, music_vol=1, texts=[], voices=[4.0], pad=1.5)
    assert "[vcc]tpad=stop_mode=clone:stop_duration=1.500[vc0]" in g and "[acc]apad=pad_dur=1.500[ac0]" in g
    assert "fade=t=out:st=5.800" in g      # fades use the padded length (5 + 1.5 - 0.7)


def test_plan_voices_keeps_the_same_files_when_replanned(tmp_path, monkeypatch):
    from backend import voice
    monkeypatch.setattr(voice, "VOICE_DIR", tmp_path)
    for slot in ("start", "final"):
        (tmp_path / slot).mkdir()
        for n in ("x.mp3", "y.mp3"):
            (tmp_path / slot / n).write_bytes(b"x")
    first = c.plan_voices([(0.0, 10.0, 1.0)], [4.0], 10.0)
    again = c.plan_voices([(0.0, 14.0, 1.0)], [4.0], 14.0, choice=first)
    assert [p for p, _ in again] == [p for p, _ in first]
    assert [t for _, t in again] == [0.3, 4.4]


def test_union_windows_merges_overlaps_and_short_gaps():
    assert c.union_windows([(6.0, 8.0), (0.0, 2.0), (2.5, 4.0), (9.5, 9.0)], min_cut=1.5) == [(0.0, 4.0), (6.0, 8.0)]


def test_build_filter_ducks_game_audio_and_fast_forward_under_the_voice_lines():
    segs = [(0.0, 6.0, 6.0), (6.0, 9.0, 1.0)]
    g = c.build_filter(segs, fps=60, width=1280, height=720, smooth=False, kills=[], impact=False, look=False, fade=False,
                       has_music=False, game_vol=0.3, music_vol=0.7, texts=[], voices=[0.5], duck=[(0.5, 2.5)])
    assert "atempo=1.5000,volume=0.35" in g                                             # the 6x segment is turned down
    assert "volume='1.00*(1-0.5*min(1\\,between(t\\,0.25\\,2.75)))':eval=frame" in g   # game audio ducks under the line
    assert "[1:a]adelay=500:all=1" in g and "amix=inputs=2" in g


def test_casters_are_never_remembered():
    o = c.options({"cine_voice": True, "cine_slow": 0.5})
    assert o["cine_voice"] is False and o["cine_slow"] == 0.5          # saved settings never turn the casters on
    assert c.options({}, {"cine_voice": True})["cine_voice"] is True   # ticked for this run


def test_fresh_tracks_avoid_the_songs_just_heard():
    from backend.cinematic import fresh_tracks
    names = ["a", "b", "c", "d"]
    assert fresh_tracks(names, []) == names                     # nothing heard yet: any
    assert fresh_tracks(names, ["a", "b"]) == ["c", "d"]          # never-used first
    assert fresh_tracks(names, ["c", "a", "d", "b"]) == ["c", "a"]  # the half heard longest ago
    assert fresh_tracks(["a"], ["a"]) == ["a"]
    assert fresh_tracks([], ["a"]) == []


def test_clutch_counter_counts_down_to_clutch():
    s = c.clutch_states(3, [2.0, 5.0, 9.0], 12.0)
    assert [x["text"] for x in s] == ["1v3", "1v2", "1v1"]                 # gone after the last kill
    assert (s[0]["from"], s[0]["to"], s[-1]["to"]) == (0.0, 2.0, 9.0)
    assert [x["text"] for x in c.clutch_states(3, [2.0, 5.0], 8.0)] == ["1v3", "1v2", "1v1"]   # the last one died otherwise


def test_round_score_follows_the_player_across_the_side_swap():
    me = "1"
    rounds = [{"num": i, "winner": w, "teams": {me: side}} for i, (w, side) in
              enumerate([("T", "T"), ("CT", "T"), ("T", "T"), ("T", "CT"), ("CT", "CT")], start=1)]
    meta = {"rounds": rounds}
    assert c.round_score(meta, 1, me) == (0, 0)
    assert c.round_score(meta, 4, me) == (2, 1)          # won 1 and 3 as T, lost 2
    assert c.round_score(meta, 6, me) == (3, 2)          # after the swap: lost 4 (T won), won 5 as CT
    assert c.round_score({}, 3, me) is None


def test_plan_texts_put_the_clutch_odds_top_left(tmp_path):
    segs = [(0.0, 10.0, 1.0)]
    texts, files, _ = c.plan_texts(segs, [3.0, 7.0], width=1920, height=1080, title=None, counter=False,
                                   font=tmp_path / "f.ttf", tmp_base=tmp_path / "o.mp4",
                                   clutch={"n": 2, "round": 8, "score": (7, 4)})
    words = [p.read_text(encoding="utf-8") for p in files]
    assert words == ["1v2", "1v1", "ROUND 8  ·  7–4"]
    assert all(t["x"] == "54" for t in texts)


def test_track_beats_finds_a_steady_beat():
    import random
    random.seed(1)
    fps, period = 43.07, 0.5                       # 120 BPM, onset frames of ~23 ms
    onset = [random.random() * 0.2 for _ in range(int(fps * 30))]
    true = [1.0 + k * period for k in range(56)]
    for b in true:
        onset[int(round(b * fps))] += 3.0
    beats = c.track_beats(onset, fps)
    hits = sum(1 for b in true if any(abs(b - x) < 0.05 for x in beats))
    assert hits >= 50 and len(beats) <= 62
    gaps = sorted(beats[i + 1] - beats[i] for i in range(len(beats) - 1))
    assert abs(gaps[len(gaps) // 2] - period) < 0.03
    assert c.track_beats([0.0] * 50, fps) == []


def test_split_and_trim_segments():
    segs = [(0.0, 6.0, 6.0), (6.0, 9.0, 1.0)]       # 1 s fast-forward, then 3 s normal
    split = c.split_at_out(segs, 0.25)
    assert split[0] == (0.0, 1.5, 6.0) and split[1] == (1.5, 6.0, 6.0) and c.total_duration(split) == pytest.approx(4.0)
    end = c.split_at_out(segs, c.total_duration(segs) - 0.25)
    assert end[-1] == (8.75, 9.0, 1.0)
    assert c.trim_out_end(segs, 0.5) == [(0.0, 6.0, 6.0), (6.0, 8.5, 1.0)]
    assert c.trim_out_end(segs, 3.5) == [(0.0, 3.0, 6.0)]


def test_edge_filters():
    w = c.edge_filter("whip", "out", 1920, 1080)
    assert w.startswith("scale=2304:1296,crop=1920:1080:x=") and "avgblur=sizeX=90" in w and w.endswith("setsar=1")
    z = c.edge_filter("zoom", "in", 1920, 1080)
    assert "eval=frame" in z and "crop=1920:1080" in z and "(1-t/0.400)" in z and "color=white" in z
    assert c.edge_filter("flash", "in", 1920, 1080).startswith("fade=t=in") and "color=white" in c.edge_filter("flash", "out", 1920, 1080)
    assert c.edge_filter("none", "in", 1920, 1080) == "null"


def test_clutch_counter_starts_when_the_clutch_does():
    # 4 kills: two with teammates alive, then the 1v2 from 6 s on: 1v2 until 8 s, 1v1 until 11 s, CLUTCH
    s = c.clutch_states(2, [2.0, 4.0, 8.0, 11.0], 15.0, start=6.0)
    assert [(x["text"], x["from"], x["to"]) for x in s] == [("1v2", 6.0, 8.0), ("1v1", 8.0, 11.0)]


def test_clutch_start_from_the_round_kills():
    from backend.highlights import clutch_start
    teams = {"me": "T", "a": "T", "b": "T", "x": "CT", "y": "CT", "z": "CT"}
    kills = [{"tick": 100, "victim": "x", "victim_team": "CT"}, {"tick": 200, "victim": "a", "victim_team": "T"},
             {"tick": 300, "victim": "y", "victim_team": "CT"}, {"tick": 400, "victim": "b", "victim_team": "T"},
             {"tick": 500, "victim": "z", "victim_team": "CT"}]
    assert clutch_start(kills, teams, "me") == (400, 1)      # alone from tick 400, against one
    assert clutch_start(kills[:3], teams, "me") is None


def test_clutch_title_names_the_kills():
    h = {"type": "clutch", "n": 2, "kills": [1, 2, 3, 4], "player_name": "a", "round": 5}
    assert c.title_for(h)[0] == "QUAD KILL"
    assert c.title_for({**h, "kills": [1, 2, 3]})[0] == "TRIPLE KILL"
    assert c.title_for({**h, "kills": [1]})[0] == "1v2"
    assert c.title_for({**h, "kills": [1, 2, 3, 4, 5]})[0] == "ACE"


def test_fit_size_fills_the_width_without_running_off():
    assert c.fit_size("GG", 1920, 1080, 0.36) == round(1080 * 0.36)              # short: capped by the height share
    long = c.fit_size("TEAMX5 -GAYEDITION", 1920, 1080, 0.24)
    assert long < round(1080 * 0.24) and long * 18 * 0.62 <= 1920 * 0.87       # long: shrunk to fit the width


def test_card_args_build_an_animated_card(tmp_path):
    files = {k: tmp_path / f"{k}.txt" for k in ("big", "line", "small")}
    args = c.card_args(Path("ff"), Path("p.mp4"), 3.0, Path("o.mp4"), files, {"big": "GG", "line": "TEAM", "small": "2 plays"},
                       Path("f.ttf"), Path("d.ttf"), 1920, 1080, 60.0, 48000, 2, 20, c.OUTRO_SECONDS, "outro")
    g = args[args.index("-filter_complex") + 1]
    assert "xfade=transition=fade" in g and "rotate=0.4" in g and g.count("drawtext=") == 4
    assert args[args.index("-frames:v") + 1] == str(round(c.OUTRO_SECONDS * 60))
    intro = c.card_args(Path("ff"), Path("p.mp4"), 3.0, Path("o.mp4"), files, {"big": "YOUR TEAM", "line": "x", "small": "y"},
                        Path("f.ttf"), Path("d.ttf"), 1920, 1080, 60.0, 48000, 2, 20, c.INTRO_SECONDS, "intro")
    assert "pow(max(0,(on-" in intro[intro.index("-filter_complex") + 1]           # the push in at the end of the intro
