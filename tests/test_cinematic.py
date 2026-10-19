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
    assert c.title_for({"type": "clutch", "n": 3, "player_name": "x", "round": 2}, "")[0] == "1v3 CLUTCH"
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
    assert ",fps=60[vc]" in g
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
