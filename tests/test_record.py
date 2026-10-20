from pathlib import Path

from backend.record import build_cfg, raw_dir_for


def hl(i, start, end, player="76561198000000001", typ="multikill", rnd=3):
    return {"id": i, "start_tick": start, "end_tick": end, "player": player, "player_name": "Some One",
            "type": typ, "round": rnd, "description": f"hl {i}"}


def test_build_cfg_schedules_in_order_and_quits():
    hs = [hl(0, 5000, 5600), hl(1, 9000, 9500)]
    names = {"76561198000000001": "Some One"}
    text, seqs = build_cfg(hs, Path("C:/x/raw"), names, {"record_fps": 60, "record_crf": 20}, 64)
    assert "mirv_cmd clear" in text and "mirv_cmd enabled 1" in text
    assert "mirv_cmd addAtTick 100 demo_gototick 4871" in text          # first jump from tick 100 to setup-1 (2 s pre-roll)
    assert "mirv_cmd addAtTick 4872 hl_s1" in text
    assert "mirv_cmd addAtTick 4904 hl_p1" in text and "mirv_cmd addAtTick 4992 hl_p1" in text   # spectator switch repeated
    assert "spec_player_by_accountid 39734273;" in text                 # 76561198000000001 -> Steam3 account id
    assert "mirv_cmd addAtTick 5000 mirv_streams record start" in text
    assert "mirv_cmd addAtTick 5600 mirv_streams record end" in text
    assert "mirv_cmd addAtTick 5616 demo_gototick 8871" in text         # next jump 16 ticks after previous end (2 s pre-roll)
    assert text.rstrip().splitlines()[-2] == "mirv_cmd addAtTick 9628 quit"
    assert "spec_player Some;" in text                                  # first token of the name
    assert "{QUOTE}C:/x/raw/seq1\\\\video.mp4{QUOTE}" in text
    assert [s["name"] for s in seqs] == ["01_multikill_r3_Some_One", "02_multikill_r3_Some_One"]
    rec_name = text.split("mirv_streams record name ")[1].split(";")[0]
    assert rec_name == "cs2watcher/demo/seq1" and ":" not in rec_name and " " not in rec_name


def test_build_cfg_separates_overlapping_windows():
    hs = [hl(0, 5000, 5600), hl(1, 5610, 5800)]
    text, seqs = build_cfg(hs, Path("C:/x/raw"), {}, {}, 64)
    assert seqs[1]["start"] == 5600 + 64 and seqs[1]["end"] >= seqs[1]["start"] + 64
    assert "mirv_cmd addAtTick 96 " not in text or seqs[0]["start"] >= 96


def test_build_cfg_no_quit_when_disabled():
    text, _ = build_cfg([hl(0, 5000, 5600)], Path("C:/x/raw"), {}, {"record_close_game": False}, 64)
    assert " quit" not in text


def test_raw_dir_has_no_spaces():
    assert " " not in str(raw_dir_for("abc")) or str(raw_dir_for("abc")).startswith("C:")


def test_pick_long_clips_orders_and_prefers_cinematic():
    from backend.record import pick_long_clips

    index = {"videos": [
        {"highlight_id": 2, "file": "b.mp4", "start_tick": 9000, "seconds": 8.0, "audio": True, "cine": "b_cine.mp4", "cine_info": {"seconds": 12.5}},
        {"highlight_id": 1, "file": "a.mp4", "start_tick": 5000, "seconds": 6.0, "audio": False},
        {"highlight_id": 3, "file": "c.mp4", "start_tick": 100, "seconds": 3.0},
    ]}
    plain = pick_long_clips(index, [1, 2], cinematic=False)
    assert [c["file"] for c in plain] == ["a.mp4", "b.mp4"]
    assert [c["audio"] for c in plain] == [False, True]
    cine = pick_long_clips(index, [1, 2], cinematic=True)
    assert [c["file"] for c in cine] == ["a.mp4", "b_cine.mp4"]  # a has no cinematic version -> plain clip
    assert cine[1]["seconds"] == 12.5 and cine[1]["audio"] is True


def test_concat_reencode_args_adds_silence_for_mute_clips():
    from backend.record import concat_reencode_args

    args = concat_reencode_args([{"path": Path("a.mp4"), "audio": True, "seconds": 5},
                                 {"path": Path("b.mp4"), "audio": False, "seconds": 7}], Path("out.mp4"), 1280, 720, 60)
    graph = args[args.index("-filter_complex") + 1]
    assert args.count("-i") == 3 and "anullsrc" in " ".join(args)
    assert "[0:a]" in graph and "[2:a]" in graph and "[1:a]" not in graph
    assert "concat=n=2:v=1:a=1[v][a]" in graph and "scale=1280:720" in graph
    assert args[-1] == "out.mp4"


def test_long_video_name():
    from backend.record import long_video_name

    assert long_video_name(3, False, 0).startswith("long_3clips_")
    assert "_cinematic_" in long_video_name(2, True, 0)


def test_delete_clips_removes_files_and_index_entries(tmp_path, monkeypatch):
    import json
    from backend import record as rec

    monkeypatch.setattr(rec, "VIDEOS_DIR", tmp_path / "videos")
    monkeypatch.setattr(rec, "export_folder_for", lambda demo_id, cfg=None: tmp_path / "dl")
    d = tmp_path / "videos" / "demo1"; d.mkdir(parents=True); (tmp_path / "dl").mkdir()
    for n in ("a.mp4", "a_cine.mp4", "b.mp4"):
        (d / n).write_bytes(b"x"); (tmp_path / "dl" / n).write_bytes(b"x")
    idx = {"videos": [{"highlight_id": 1, "file": "a.mp4", "cine": "a_cine.mp4", "start_tick": 1},
                      {"highlight_id": 2, "file": "b.mp4", "start_tick": 2}],
           "long": [{"file": "long_x.mp4"}]}
    (d / "videos.json").write_text(json.dumps(idx))
    r = rec.delete_clips("demo1", [1, 99], downloads=False)
    assert r["deleted"] == [1] and r["files"] == 2
    assert not (d / "a.mp4").exists() and not (d / "a_cine.mp4").exists() and (d / "b.mp4").exists()
    assert (tmp_path / "dl" / "a.mp4").exists()  # Downloads copy kept
    after = rec.videos_index("demo1")
    assert [v["highlight_id"] for v in after["videos"]] == [2] and after["long"]
    r = rec.delete_clips("demo1", [2], downloads=True)
    assert r["files"] == 2 and not (tmp_path / "dl" / "b.mp4").exists()
    import pytest
    with pytest.raises(RuntimeError):
        rec.delete_clips("demo1", [5])


def test_pick_long_clips_all_cinematic():
    from backend.record import pick_long_clips
    index = {"videos": [
        {"highlight_id": 1, "file": "a.mp4", "start_tick": 5000, "seconds": 10.0, "audio": True},
        {"highlight_id": 2, "file": "b.mp4", "start_tick": 9000, "seconds": 8.0, "audio": True, "cine": "b_cine.mp4", "cine_info": {"seconds": 12.5}},
        {"highlight_id": 3, "file": "c.mp4", "start_tick": 1000, "seconds": 8.0, "audio": True, "cine": "c_cine.mp4", "cine_info": {"seconds": 9.0}},
    ]}
    assert [c["file"] for c in pick_long_clips(index, None, cinematic=True)] == ["c_cine.mp4", "b_cine.mp4"]
    assert [c["file"] for c in pick_long_clips(index, None, cinematic=False)] == ["c.mp4", "a.mp4", "b.mp4"]


def _saved_setup(tmp_path, monkeypatch, demo=None):
    import json
    from backend import record as r
    monkeypatch.setattr(r, "VIDEOS_DIR", tmp_path)
    monkeypatch.setattr(r, "MONTAGE_DIR", tmp_path / r.MONTAGE_ID)
    monkeypatch.setattr(r.db, "get_demo", lambda i: demo if demo and i == "ab12cd34ef567890" else None)
    d = tmp_path / "ab12cd34ef567890"
    d.mkdir()
    for n in ("01_clutch.mp4", "01_clutch_cine.mp4", "02_ace.mp4", "long_2clips.mp4"):
        (d / n).write_bytes(b"x" * 10)
    idx = {"videos": [{"highlight_id": 1, "file": "01_clutch.mp4", "cine": "01_clutch_cine.mp4", "description": "1v3 clutch",
                       "seconds": 20, "cine_info": {"seconds": 23}, "player": "ron", "round": 8},
                      {"highlight_id": 2, "file": "02_ace.mp4", "description": "ace", "seconds": 15}],
           "long": [{"file": "long_2clips.mp4", "description": "2 highlights", "seconds": 35}],
           "export_folder": "C:\\Users\\x\\Downloads\\cs2-watcher\\de_dust2_2026-10-19_ab12cd34"}
    (d / "videos.json").write_text(json.dumps(idx))
    return r, d


def test_saved_clips_survive_a_deleted_demo(tmp_path, monkeypatch):
    r, _ = _saved_setup(tmp_path, monkeypatch)                       # no demo in the database any more
    cs = r.saved_clips()
    assert sorted(c["kind"] for c in cs) == ["cinematic", "clip", "clip", "long"]
    cine = next(c for c in cs if c["kind"] == "cinematic")
    assert cine["seconds"] == 23 and cine["description"] == "1v3 clutch" and cine["player"] == "ron"
    assert all(c["map"] == "de_dust2" and c["date"] == "2026-10-19" and not c["demo_exists"] for c in cs)   # from the folder name


def test_stamp_match_keeps_the_details(tmp_path, monkeypatch):
    demo = {"map": "de_mirage", "date": "2026-10-01T20:00:00", "team_a": "A", "team_b": "B", "score_a": 13, "score_b": 9,
            "filename": "x.dem", "status": "done"}
    r, _ = _saved_setup(tmp_path, monkeypatch, demo)
    assert r.saved_clips()[0]["demo_exists"]
    monkeypatch.setattr(r.db, "get_demo", lambda i: None)            # the demo is deleted afterwards
    c = r.saved_clips()[0]
    assert c["map"] == "de_mirage" and c["match"]["score_a"] == 13 and not c["demo_exists"]


def test_delete_saved(tmp_path, monkeypatch):
    import pytest
    r, d = _saved_setup(tmp_path, monkeypatch)
    r.delete_saved("ab12cd34ef567890", "01_clutch_cine.mp4")          # only the cinematic version
    assert (d / "01_clutch.mp4").exists() and not (d / "01_clutch_cine.mp4").exists()
    assert sorted(c["kind"] for c in r.saved_clips()) == ["clip", "clip", "long"]
    r.delete_saved("ab12cd34ef567890", "long_2clips.mp4")
    r.delete_saved("ab12cd34ef567890", "02_ace.mp4")
    assert [c["file"] for c in r.saved_clips()] == ["01_clutch.mp4"]
    with pytest.raises(RuntimeError):
        r.delete_saved("ab12cd34ef567890", "nope.mp4")


def test_stream_sig():
    from backend.record import stream_sig
    a = ("  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), 1920x1080 [SAR 1:1 DAR 16:9], 60 fps\n"
         "  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, stereo, fltp, 195 kb/s (default)\n")
    assert stream_sig(a) == (("h264", "1920x1080"), ("aac", "44100", "stereo"))
    assert stream_sig(a.replace("1920x1080", "1280x720")) != stream_sig(a)
    assert stream_sig("no streams")[0] is None


def test_cinematic_queue(monkeypatch):
    import pytest
    from backend import record as r
    launched = []
    monkeypatch.setattr(r, "_launch_cinematic", lambda d, vs, o=None: launched.append((d, [v["highlight_id"] for v in vs])) or {"total": len(vs)})
    monkeypatch.setattr(r, "_cine_thread", None)
    monkeypatch.setattr(r, "_cine_queue", [])
    monkeypatch.setattr(r, "_cine_job", {"demo_id": "aaa", "status": "running", "highlight_ids": [1]})
    assert r.start_cinematic("bbb", [{"highlight_id": 2}])["queued"] == 1             # another match waits in line
    assert r.start_cinematic("bbb", [{"highlight_id": 2}, {"highlight_id": 3}])["total"] == 1   # 2 is already queued
    with pytest.raises(RuntimeError):
        r.start_cinematic("aaa", [{"highlight_id": 1}])                               # being made right now
    assert [q["demo_id"] for q in r.cinematic_status()["queued"]] == ["bbb", "bbb"]
    r._cine_job["status"] = "done"
    r._start_next_cinematic()
    assert launched == [("bbb", [2])] and len(r._cine_queue) == 1
    assert r.clear_cinematic_queue() == 1 and r.cinematic_status()["queued"] == []


def test_match_highlight_relinks_by_player_round_and_time():
    from backend.record import match_highlight
    v = {"highlight_id": 11, "player_steamid": "765", "round": 12, "type": "multikill", "start_tick": 65472, "end_tick": 66080}
    hs = [{"id": 0, "player": "765", "round": 12, "type": "kill", "start_tick": 65696, "end_tick": 66080},
          {"id": 3, "player": "765", "round": 12, "type": "multikill", "start_tick": 62824, "end_tick": 66080},
          {"id": 4, "player": "999", "round": 12, "type": "multikill", "start_tick": 65000, "end_tick": 66080}]
    assert match_highlight(v, hs)["id"] == 3          # the merged 4K, not the single kill or another player
    assert match_highlight({**v, "round": 5}, hs) is None


def test_squad_highlights():
    from backend.record import squad_highlights, in_squad
    members = [{"name": "invis", "steamid": "765"}, {"name": "vALenk", "steamid": ""}]
    assert in_squad("765", "InvviS", members) and in_squad("1", "valenk", members) and not in_squad("2", "someone", members)
    hs = [{"id": 1, "type": "multikill", "player": "765", "player_name": "InvviS", "kills": [1, 2, 3], "start_tick": 900},
          {"id": 2, "type": "multikill", "player": "765", "player_name": "InvviS", "kills": [1, 2], "start_tick": 100},   # 2K
          {"id": 3, "type": "clutch", "player": "9", "player_name": "vALenk", "kills": [1, 2, 3, 4], "start_tick": 500},
          {"id": 4, "type": "ace", "player": "8", "player_name": "enemy", "kills": [1, 2, 3, 4, 5], "start_tick": 50},
          {"id": 5, "type": "kill", "player": "765", "player_name": "InvviS", "kills": [1], "start_tick": 10}]
    assert [h["id"] for h in squad_highlights(hs, members)] == [3, 1]          # match order, 3K+ of the squad only
    assert [h["id"] for h in squad_highlights(hs, members, 4)] == [3]


def test_montage_timeline():
    from backend.record import montage_timeline
    infos = [{"seconds": 20, "hits": [], "duck": [(0.3, 2.0)]},
             {"seconds": 25, "hits": [12.5, 14], "duck": [(1, 3)]},
             {"seconds": 10, "hits": [4], "duck": []}]
    total, hit, duck = montage_timeline(infos)
    assert total == 55 and hit == 32.5 and duck == [(0.3, 2.0), (21.0, 23.0)]   # first slow-mo kill is in play 2


def test_montage_music_args():
    from pathlib import Path
    from backend.cinematic import montage_music_args
    a = montage_music_args(Path("ff"), Path("v.mp4"), Path("song.mp3"), Path("o.mp4"), 60, 12.5, 0.7, [(5, 7)])
    assert a[a.index("-stream_loop") + 1] == "-1" and a[a.index("-ss") + 1] == "12.500" and "-c:v" in a and a[a.index("-c:v") + 1] == "copy"
    g = a[a.index("-filter_complex") + 1]
    assert "atrim=start=0:end=60.000" in g and "afade=t=out:st=57.500" in g and r"between(t\,4.75\,7.25)" in g
    assert "-ss" not in montage_music_args(Path("ff"), Path("v.mp4"), Path("s.mp3"), Path("o.mp4"), 60, 0, 0.7)


def test_start_squad_reel_with_picked_highlights(monkeypatch):
    import backend.record as rec
    hs = [hl(1, 9000, 9500), hl(2, 5000, 5600), hl(3, 12000, 12500)]
    monkeypatch.setattr(rec.db, "get_highlights", lambda demo_id: hs)
    monkeypatch.setattr(rec.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(rec, "_squad_job", None)
    r = rec.start_squad_reel("demo1", highlight_ids=[1, 2])
    assert r["highlight_ids"] == [2, 1] and r["label"] == "Montage" and r["picked"]   # match order, only the picked ones
    rec._squad_job["status"] = "done"
    import pytest
    with pytest.raises(RuntimeError):
        rec.start_squad_reel("demo1", highlight_ids=[3])                                  # one play is not a montage


def test_squad_progress_never_reports_done_early():
    from backend.record import squad_progress
    run = {"status": "running", "total": 4}
    assert squad_progress({**run, "stage": "recording", "needs_recording": True}, 1.0) == 0.35   # recording done != montage done
    assert squad_progress({**run, "stage": "cinematic", "piece_i": 0, "piece_frac": 0}) == 0.0
    assert squad_progress({**run, "stage": "cinematic", "piece_i": 2, "piece_frac": 0}) == 0.44
    assert squad_progress({**run, "stage": "cinematic", "piece_i": 4, "piece_frac": 0}) == 0.88
    assert 0.88 < squad_progress({**run, "stage": "joining", "piece": "laying the song"}) < 1
    assert squad_progress({"status": "done"}) == 1.0


def test_montage_plays_go_back_to_the_original_recording(tmp_path, monkeypatch):
    import json
    import pytest
    import backend.record as rec
    monkeypatch.setattr(rec, "VIDEOS_DIR", tmp_path)
    (tmp_path / "d1").mkdir()
    (tmp_path / "d1" / "videos.json").write_text(json.dumps({"videos": [
        {"highlight_id": 1, "file": "a.mp4", "cine": "a_cine.mp4", "start_tick": 1},
        {"highlight_id": 2, "file": "b.mp4", "start_tick": 2}], "long": [{"file": "long_x.mp4"}]}))
    plays = rec.montage_plays([{"demo_id": "d1", "file": "b.mp4"}, {"demo_id": "d1", "file": "a_cine.mp4"}])
    assert [(d, v["file"]) for d, v in plays] == [("d1", "b.mp4"), ("d1", "a.mp4")]   # picked order kept
    with pytest.raises(RuntimeError):
        rec.montage_plays([{"demo_id": "d1", "file": "long_x.mp4"}])
    with pytest.raises(RuntimeError):
        rec.montage_plays([{"demo_id": "montages", "file": "montage_2clips.mp4"}])


def test_stamp_match_saves_kill_timing_with_each_clip(tmp_path, monkeypatch):
    import json
    import backend.cinematic as cine
    import backend.record as rec
    monkeypatch.setattr(rec, "VIDEOS_DIR", tmp_path)
    monkeypatch.setattr(rec, "match_info", lambda demo_id: {"map": "de_nuke"})
    calls = []

    def ctx(demo_id, hid, start, end=None):
        calls.append(hid)
        return {"kills": [2.5, 4.0], "highlight": {"id": hid, "player_name": "x"}, "map": "de_nuke"}
    monkeypatch.setattr(cine, "clip_context", ctx)
    (tmp_path / "d1").mkdir()
    p = tmp_path / "d1" / "videos.json"
    p.write_text(json.dumps({"videos": [{"highlight_id": 7, "file": "a.mp4", "start_tick": 100}]}))
    rec.stamp_match("d1")
    v = json.loads(p.read_text())["videos"][0]
    assert v["ctx"]["kills"] == [2.5, 4.0] and v["ctx"]["highlight_id"] == 7
    rec.stamp_match("d1")
    assert calls == [7]                      # stamped once, not on every gallery listing


def test_recap_plays_period_squad_order_and_best_of():
    from backend.record import recap_plays, recap_period_name
    me = [{"name": "me", "steamid": "1"}]
    v = lambda hid, t, typ="multikill", sid="1", tick=0: {"highlight_id": hid, "type": typ, "player_steamid": sid, "player": "x",
                                                           "start_tick": tick, "file": f"{hid}.mp4"}
    matches = [
        {"demo_id": "b", "date": "2026-10-20", "videos": [v(1, 0, tick=500), v(2, 0, "ace", tick=100)],
         "highlights": {1: {"kills": [1, 2, 3]}, 2: {"kills": [1, 2, 3, 4, 5]}}},
        {"demo_id": "a", "date": "2026-10-03", "videos": [v(3, 0, "clutch"), v(4, 0, sid="9"), v(5, 0, "kill")],
         "highlights": {3: {"kills": [1, 2, 3]}, 4: {"kills": [1, 2, 3]}, 5: {"kills": [1]}}},
        {"demo_id": "c", "date": "2026-09-30", "videos": [v(6, 0, "ace")], "highlights": {}},   # outside the month
        {"demo_id": "d", "date": "2026-10-10", "videos": [v(7, 0, "ace")], "highlights": {}},   # deleted match: type decides
    ]
    got = recap_plays(matches, me, "2026-10-01", "2026-10-31")
    assert [p["video"]["highlight_id"] for p in got] == [3, 7, 2, 1]           # oldest match first, match order inside
    best = recap_plays(matches, me, "2026-10-01", "2026-10-31", best_of=2)
    assert [p["video"]["highlight_id"] for p in best] == [7, 2]                 # the two aces, still in time order
    assert [p["video"]["highlight_id"] for p in recap_plays(matches, me, "2026-10-01", "2026-10-31", 4)] == [7, 2]
    assert recap_period_name("2026-10-01", "2026-10-31") == "October 2026"
    assert recap_period_name("2026-01-01", "2026-12-31") == "2026"


def test_intro_dates_and_stats():
    from backend.record import intro_dates
    from backend.cinematic import intro_stats
    assert intro_dates(["2026-10-17"]) == "17 Oct 2026"
    assert intro_dates(["2026-10-20T19:00:00", "2026-10-03"]) == "3–20 Oct 2026"
    assert intro_dates(["2026-09-28", "2026-10-20"]) == "28 Sep – 20 Oct 2026"
    assert intro_dates(["2025-12-30", "2026-01-02"]) == "30 Dec 2025 – 2 Jan 2026"
    assert intro_dates([]) == ""
    assert intro_stats([5, 4, 3, 3], ["ace", "multikill", "clutch", "multikill"]) == "4 plays · 1 ace · 1 4K · 1 clutch · 15 kills"
    assert intro_stats([3], ["multikill"]) == "1 play · 3 kills"


def test_multi_match_recording_progress():
    from backend.record import squad_progress
    st = {"status": "running", "stage": "recording", "needs_recording": True, "rec_n": 4, "total": 10}
    assert squad_progress({**st, "rec_i": 0}, 1.0) == round(0.35 / 4, 3)
    assert squad_progress({**st, "rec_i": 3}, 1.0) == 0.35          # last match recorded = recording share done


def test_team_name_is_kept_when_members_change(tmp_path, monkeypatch):
    import backend.record as rec
    monkeypatch.setattr(rec, "SQUAD_PATH", tmp_path / "squad.json")
    rec.save_squad([{"name": "a", "steamid": "1"}], team="The Boys", intro=True)
    rec.save_squad([{"name": "b", "steamid": "2"}])
    assert rec.load_team() == {"team": "The Boys", "intro": True}
    assert [m["name"] for m in rec.load_squad()] == ["b"]
    rec.save_squad(intro=False)
    assert rec.load_team()["intro"] is False and [m["name"] for m in rec.load_squad()] == ["b"]


def test_build_cfg_hides_demo_ui_once_before_recording():
    # from PR #1 (slipknot94), with the setup tick computed instead of hard-coded
    hs = [hl(0, 5000, 5600), hl(1, 9000, 9500)]
    text, _ = build_cfg(hs, Path("C:/x/raw"), {}, {}, 64)
    lines = text.splitlines()
    ui = [ln for ln in lines if ln.endswith(" demoui")]
    assert len(ui) == 1                                                       # a toggle: exactly once
    assert "demoui" not in "".join(lines[:lines.index("mirv_cmd enabled 1") + 1])
    setup = int(ui[0].split()[2])
    assert setup < 5000 and lines.index(ui[0]) < lines.index("mirv_cmd addAtTick 5000 mirv_streams record start")
    assert f"mirv_cmd addAtTick {setup} hl_s1" in lines                       # with the first highlight's setup


def test_build_cfg_hud_shows_health_and_ammo_by_default():
    text, _ = build_cfg([hl(0, 5000, 5600)], Path("C:/x/raw"), {}, {}, 64)
    assert "cl_draw_only_deathnotices 0" in text and "cl_drawhud_force_radar -1" in text
    text, _ = build_cfg([hl(0, 5000, 5600)], Path("C:/x/raw"), {}, {"record_hud": "killfeed"}, 64)
    assert "cl_draw_only_deathnotices 1" in text and "cl_drawhud_force_radar" not in text


def test_squad_players_names_and_photos(tmp_path, monkeypatch):
    import json
    import backend.cinematic as cine
    import backend.record as rec
    monkeypatch.setattr(rec, "SQUAD_PATH", tmp_path / "squad.json")
    monkeypatch.setattr(cine, "PLAYERS_DIR", tmp_path / "players")
    monkeypatch.setattr(cine, "PLAYERS_JSON", tmp_path / "players.json")
    assert rec.photo_key("7656 1197", "x") == "76561197" and rec.photo_key("", "vAL enk!") == "vALenk"
    rec.save_squad_players([{"name": "qwqw", "steamid": "765611", "card_name": "Ron"}, {"name": "vALenk", "steamid": "", "card_name": "ignored"}])
    assert json.loads((tmp_path / "players.json").read_text()) == {"765611": {"name": "Ron"}}
    rec.save_player_photo("765611", "qwqw", b"png1", ".png")
    rec.save_player_photo("765611", "qwqw", b"jpg2", ".jpg")                 # replaces the older one
    assert sorted(f.name for f in (tmp_path / "players").iterdir()) == ["765611.jpg"]
    rec.save_player_photo("", "vALenk", b"p", ".png")
    ps = {p["name"]: p for p in rec.squad_players()}
    assert ps["qwqw"]["photo"] == "765611.jpg" and ps["qwqw"]["card_name"] == "Ron" and ps["vALenk"]["photo"] == "vALenk.png"
    rec.remove_player_photo("765611", "qwqw")
    assert rec.squad_players()[0]["photo"] is None and json.loads((tmp_path / "players.json").read_text()) == {"765611": {"name": "Ron"}}
    import pytest
    with pytest.raises(RuntimeError):
        rec.save_player_photo("1", "a", b"x", ".gif")
