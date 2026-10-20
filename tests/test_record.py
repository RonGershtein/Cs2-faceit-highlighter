from pathlib import Path

from backend.record import build_cfg, encoder_args, planned_entry, raw_dir_for


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


def test_build_cfg_hud_is_the_kill_feed_only_by_default():
    text, _ = build_cfg([hl(0, 5000, 5600)], Path("C:/x/raw"), {}, {}, 64)
    assert "cl_draw_only_deathnotices 1" in text and "cl_drawhud_force_radar" not in text
    text, _ = build_cfg([hl(0, 5000, 5600)], Path("C:/x/raw"), {}, {"record_hud": "health_ammo"}, 64)
    assert "cl_draw_only_deathnotices 0" in text and "cl_drawhud_force_radar -1" in text


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


def test_match_groups_get_one_soundtrack_over_the_whole_video(tmp_path, monkeypatch):
    import backend.record as rec
    monkeypatch.setattr(rec, "MONTAGE_DIR", tmp_path)
    seen = []

    def fake(plays, overrides, work_dir, out, base=0, last=True, intro=None, follow=None, reorder=None):
        seen.append(([v["file"] for _, v in plays], overrides["cine_music"], intro is not None))
        return {"seconds": 20.0, "music": "Granite.mp3", "songs": ["Granite.mp3", "Blood Sugar.mp3"], "order": [2, 0, 1]}
    monkeypatch.setattr(rec, "_render_song_montage", fake)
    groups = [[("d1", {"file": "a.mp4"})], [("d2", {"file": "b.mp4"}), ("d2", {"file": "c.mp4"})]]
    made = rec._render_match_groups(groups, tmp_path / "o.mp4", "s", {"team": "x"}, "Granite.mp3")
    assert seen == [(["a.mp4", "b.mp4", "c.mp4"], "Granite.mp3", True)]      # all matches in one render, one song
    assert made["music"] == ["Granite.mp3", "Blood Sugar.mp3"]
    assert [v["file"] for _, v in made["plays"]] == ["c.mp4", "a.mp4", "b.mp4"]   # the order they were joined in


def test_music_bed_follows_a_short_song_with_the_next_ones():
    from backend.cinematic import bed_tracks_needed, music_bed_args
    assert bed_tracks_needed(200, 30, 120, [180, 180]) == 0                     # 170 s of song left for 120 s
    assert bed_tracks_needed(200, 30, 300, [180, 180]) == 1                     # 170 + 177 covers 300
    assert bed_tracks_needed(100, 0, 600, [180, 180]) == 2                      # runs out: all of them
    args = music_bed_args(Path("ff"), Path("a.mp3"), 12.5, [Path("b.mp3"), Path("c.mp3")], Path("bed.m4a"))
    g = args[args.index("-filter_complex") + 1]
    assert args[:6] == ["ff", "-y", "-ss", "12.500", "-i", "a.mp3"]
    assert g == "[0:a][1:a]acrossfade=d=3:c1=tri:c2=tri[x1];[x1][2:a]acrossfade=d=3:c1=tri:c2=tri[x2]" and args[args.index("-map") + 1] == "[x2]"


def test_build_cfg_chains_to_the_next_demo_instead_of_quitting():
    hs = [hl(0, 5000, 5600)]
    text, _ = build_cfg(hs, Path("C:/x/raw"), {}, {}, 64, "d1", next_cfg="cs2watcher_rec_2")
    lines = text.splitlines()
    assert "mirv_cmd addAtTick 5728 exec cs2watcher_rec_2" in lines and not any(ln.endswith(" quit") for ln in lines)
    text2, _ = build_cfg([hl(1, 9000, 9500)], Path("C:/x/raw2"), {}, {}, 64, "d2", part=2, playdemo="replay_d2.dem")
    l2 = text2.splitlines()
    assert l2[-1] == 'playdemo "replay_d2.dem"'                                # loads its demo after scheduling
    assert l2.index("mirv_cmd clear") < l2.index('playdemo "replay_d2.dem"')    # the old demo's commands are gone
    assert any(ln.startswith('alias hl2_s1 "') for ln in l2) and "mirv_cmd addAtTick 8872 hl2_s1" in l2
    assert any("settings add ffmpeg p2_1 " in ln for ln in l2)                  # names do not collide with demo 1
    assert l2[-2:-1] == ['echo "[cs2watcher] commands scheduled"'] and any(ln.endswith(" quit") for ln in l2)


def test_plan_sessions_chains_demos_and_splits_on_a_collision():
    from backend.record import plan_sessions, can_chain
    part = lambda d, hs: {"demo_id": d, "hs": hs, "raw_dir": Path(f"C:/r/{d}"), "names": {}, "tickrate": 64,
                          "dem_name": f"replay_{d}.dem", "last_end": max(h["end_tick"] for h in hs)}
    a, b, c = part("a", [hl(0, 5000, 5600)]), part("b", [hl(1, 9000, 9500)]), part("c", [hl(2, 400, 900)])
    sess = plan_sessions([a, b], {})
    assert [[p["demo_id"] for p in s] for s in sess] == [["a", "b"]]
    assert a["cfg_name"] == "cs2watcher_rec" and b["cfg_name"] == "cs2watcher_rec_2"
    assert "exec cs2watcher_rec_2" in a["cfg_text"] and 'playdemo "replay_b.dem"' in b["cfg_text"]
    assert can_chain(5728, [100, 8872], 64) and not can_chain(5728, [5750], 64)
    late = part("late", [hl(3, 5800, 6200)])        # its setup tick lands right after demo a's handover
    assert [[p["demo_id"] for p in s] for s in plan_sessions([a, late, c], {})] == [["a"], ["late", "c"]]


def test_deleting_a_video_that_is_still_open(tmp_path, monkeypatch):
    import json
    import pytest
    import backend.record as rec
    monkeypatch.setattr(rec, "MONTAGE_DIR", tmp_path)
    (tmp_path / "m.mp4").write_bytes(b"x")
    (tmp_path / "montages.json").write_text(json.dumps({"montages": [{"file": "m.mp4"}]}))
    real_unlink, calls = Path.unlink, []

    def busy_twice(self, missing_ok=False):
        calls.append(self.name)
        if len(calls) <= 2:
            raise PermissionError(32, "in use")
        return real_unlink(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", busy_twice)
    assert rec.delete_saved("montages", "m.mp4")["ok"] and not (tmp_path / "m.mp4").exists()   # retried until free
    (tmp_path / "n.mp4").write_bytes(b"x")
    (tmp_path / "montages.json").write_text(json.dumps({"montages": [{"file": "n.mp4"}]}))
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(PermissionError(32, "in use")))
    monkeypatch.setattr(rec.time, "sleep", lambda s: None)
    with pytest.raises(rec.FileBusy):
        rec.delete_saved("montages", "n.mp4")
    assert rec.montages_index() == [{"file": "n.mp4"}]          # still listed, so it can be deleted again


def test_cleanup_replays_only_while_cs2_is_closed(tmp_path, monkeypatch):
    import backend.launch as la
    csgo = tmp_path / "game" / "csgo"; csgo.mkdir(parents=True)
    for n in ("replay_aaaa1111.dem", "replay_bbbb2222.dem", "other.dem"):
        (csgo / n).write_bytes(b"x")
    monkeypatch.setattr(la, "find_cs2_path", lambda: tmp_path)
    monkeypatch.setattr(la, "cs2_running", lambda: True)
    assert la.cleanup_replays() == 0 and len(list(csgo.iterdir())) == 3          # CS2 open: nothing touched
    monkeypatch.setattr(la, "cs2_running", lambda: False)
    assert la.cleanup_replays(keep={"replay_bbbb2222.dem"}) == 1                # a match no longer in the app
    assert sorted(p.name for p in csgo.iterdir()) == ["other.dem", "replay_bbbb2222.dem"]
    import backend.db as db
    monkeypatch.setattr(db, "list_demos", lambda: [{"id": "bbbb2222cccc"}])
    assert la.cleanup_replays() == 0                                             # by default: the library's matches stay


def test_song_montage_renders_plays_in_parallel_and_keeps_their_order(tmp_path, monkeypatch):
    import threading, time as _t
    import backend.cinematic as cine
    import backend.record as rec
    monkeypatch.setattr(rec, "VIDEOS_DIR", tmp_path)
    monkeypatch.setattr(rec, "load_config", lambda: {"render_parallel": 3, "montage_outro": False})
    monkeypatch.setattr(rec, "_squad_job", {"status": "running", "total": 5})
    (tmp_path / "d").mkdir()
    live, peak, lock = [0], [0], threading.Lock()

    def fake(demo_id, v, ffmpeg, ffprobe, vdir, overrides=None, progress=None, montage_piece=False, **kw):
        with lock:
            live[0] += 1; peak[0] = max(peak[0], live[0])
        progress("encoding", 0.5)
        _t.sleep(0.15 if v["file"] == "0.mp4" else 0.05)          # the first play is the slowest
        (tmp_path / "d" / f"m{v['file']}").write_bytes(b"x")
        with lock:
            live[0] -= 1
        return {**v, "cine": f"m{v['file']}", "cine_info": {"seconds": 1.0, "name": v["file"]}}
    monkeypatch.setattr(cine, "cinematic_for_video", fake)
    order = []
    monkeypatch.setattr(rec, "_join", lambda files, infos, out, lst: (order.extend(i["name"] for i in infos), out.write_bytes(b"j")))
    monkeypatch.setattr(cine, "pick_music", lambda opts, exclude=None: None)
    plays = [("d", {"file": f"{i}.mp4"}) for i in range(5)]
    rec._render_song_montage(plays, None, tmp_path, tmp_path / "out.mp4")
    assert peak[0] == 3                                                    # three at once
    assert order == [f"{i}.mp4" for i in range(5)]                          # joined in play order anyway
    assert not list((tmp_path / "d").glob("m*.mp4"))                        # the pieces are cleaned up


def test_plan_drop_slowmo_puts_slow_motion_only_on_the_drops():
    from backend.record import plan_drop_slowmo
    # every play: 10 s at normal speed, 14 s in slow motion with its kill at 6 s (lead 2 s); other leads move it
    def play():
        vs = [{"slow": True, "lead": None, "seconds": 14.0, "hit": 6.0}, {"slow": False, "lead": None, "seconds": 10.0, "hit": None}]
        vs += [{"slow": True, "lead": l, "seconds": 14.0 + (l - 2.0), "hit": 6.0 + (l - 2.0)} for l in (1.0, 1.5, 2.5, 3.0, 3.5)]
        return vs
    drops = [40.0, 63.5, 99.0]             # song time
    chosen, offset = plan_drop_slowmo([play() for _ in range(6)], 3.5, drops, lambda hit, total: 40.0 - hit)
    assert offset == 40.0 - 9.5             # the first play's kill (3.5 s intro + 6 s) takes the main drop
    assert chosen[0]["slow"] and chosen[0]["lead"] is None
    # montage time of the drops: 63.5 -> 33.0, 99 -> 68.5. Play 2 starts at 17.5: normal. Play 3 starts at 27.5:
    # its kill at 27.5 + 6 = 33.5 is half a second late, the lead 1.5 variant puts it on 33.0; play 6 (at 61.0)
    # reaches 68.5 only with the longest lead
    assert [c["slow"] for c in chosen] == [True, False, True, False, False, True]
    assert chosen[2]["lead"] == 1.5 and chosen[5]["lead"] == 3.5
    t = 3.5 + sum(c["seconds"] for c in chosen[:5])
    assert abs(t + chosen[5]["hit"] - 68.5) <= 0.3            # the last drop lands on play 6's kill


def test_find_drops_and_clip_timing():
    from backend.cinematic import find_drops, clip_timing
    curve = [0.1] * 40 + [1.0] * 20 + [0.1] * 30 + [1.0] * 20 + [0.2] * 10   # rises at 20 s and 45 s (0.5 s steps)
    drops = find_drops(curve, min_gap=10.0)
    assert len(drops) == 2 and abs(drops[0] - 20.0) <= 1.0 and abs(drops[1] - 45.0) <= 1.0
    opts = {"cine_slow": 0.5, "cine_kills": "all", "cine_trim": "off", "cine_ramp": False}
    slow, normal = clip_timing([5.0], 10.0, opts), clip_timing([5.0], 10.0, {**opts, "_no_slow": True})
    assert normal == {"seconds": 10.0, "hit": None, "tail": 5.0} and slow["seconds"] > 10.0 and slow["hit"] > 5.0
    moved = clip_timing([5.0], 10.0, {**opts, "_lead": 2.0})                 # start 3 s in: the kill comes 3 s sooner
    assert abs(moved["hit"] - (slow["hit"] - 3.0)) < 1e-6


def test_session_log_says_which_demo_recorded_what(tmp_path):
    from backend.record import _keep_session_log
    csgo = tmp_path / "csgo"; csgo.mkdir()
    (csgo / "console.log").write_text("Recording to seq1\n")
    seq_ok, seq_missing = tmp_path / "a" / "seq1", tmp_path / "b" / "seq1"
    seq_ok.mkdir(parents=True); seq_missing.mkdir(parents=True); (seq_ok / "video.mp4").write_bytes(b"x")
    sess = [{"cfg_name": "cs2watcher_rec", "demo_id": "a", "dem_name": "replay_a.dem", "seqs": [{"folder": seq_ok}],
             "cfg_text": "quit", "out_dir": tmp_path / "out_a"},
            {"cfg_name": "cs2watcher_rec_2", "demo_id": "b", "dem_name": "replay_b.dem", "seqs": [{"folder": seq_missing}],
             "cfg_text": "quit", "out_dir": tmp_path / "out_b"}]
    _keep_session_log(csgo, sess, 0)
    text = (tmp_path / "out_b" / "record_log.txt").read_text()
    assert "cs2watcher_rec: a (replay_a.dem) recorded 1 of 1" in text and "cs2watcher_rec_2: b (replay_b.dem) recorded 0 of 1" in text
    assert "Recording to seq1" in text


def test_encoder_args_auto_and_by_name():
    assert encoder_args({"record_crf": 20, "record_encoder": "x264"}) == "-c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p"
    assert encoder_args({"record_crf": 20}, auto="x264").startswith("-c:v libx264")          # auto, nothing found
    amf = encoder_args({"record_crf": 20}, auto="amf")
    assert amf.startswith("-c:v h264_amf") and "-b:v 20M -maxrate 32M" in amf
    assert "h264_nvenc" in encoder_args({"record_encoder": "nvenc"}) and "-cq 18" in encoder_args({"record_encoder": "nvenc"})
    assert "h264_qsv" in encoder_args({"record_encoder": "qsv"})
    assert encoder_args({"record_encoder": "bogus"}).startswith("-c:v libx264")
    text, _ = build_cfg([hl(0, 5000, 5600)], Path("C:/x/raw"), {}, {"record_encoder": "amf"}, 64)
    assert '"-c:v h264_amf' in text and "libx264" not in text


def test_pick_encoder_only_takes_an_encoder_that_works():
    from backend.record import pick_encoder
    assert pick_encoder(["amd"], lambda e: True) == "amf"
    assert pick_encoder(["nvidia", "intel"], lambda e: e == "qsv") == "qsv"     # NVENC failed: Intel's next
    assert pick_encoder(["amd"], lambda e: False) == "x264"                    # broken driver: the CPU
    assert pick_encoder([], lambda e: True) == "x264"


def test_build_cfg_fast_preroll_is_back_to_normal_before_recording():
    plain, _ = build_cfg([hl(0, 5000, 5600)], Path("C:/x/raw"), {}, {}, 64)
    assert "demo_timescale" not in plain
    text, _ = build_cfg([hl(0, 5000, 5600)], Path("C:/x/raw"), {}, {"record_fast_preroll": True}, 64)
    ticks = {ln.split(" ", 3)[3]: int(ln.split()[2]) for ln in text.splitlines() if ln.startswith("mirv_cmd addAtTick")}
    assert ticks["demo_timescale 4"] == 4873                       # just after the seek (setup 4872)
    assert ticks["demo_timescale 1"] == 4976 < 4992                # normal speed before the last spectator switch
    assert ticks["mirv_streams record start"] == 5000


def test_planned_entry_is_what_the_mux_writes():
    _, seqs = build_cfg([hl(7, 5000, 5640)], Path("C:/x/raw"), {"76561198000000001": "Some One"}, {}, 64)
    e = planned_entry(seqs[0], 64)
    assert e["file"] == "01_multikill_r3_Some_One.mp4" and e["highlight_id"] == 7
    assert e["start_tick"] == 5000 and e["end_tick"] == 5640 and e["seconds"] == 10.0


def test_mux_finished_waits_for_the_next_clip_and_a_still_file(tmp_path, monkeypatch):
    import backend.record as r
    now = [100.0]
    monkeypatch.setattr(r.time, "time", lambda: now[0])
    muxed, indexed = [], []
    monkeypatch.setattr(r, "encode_one", lambda sq, out_dir, tr: muxed.append(sq["n"]) or {"file": f"{sq['n']}.mp4", "highlight_id": sq["n"]})
    monkeypatch.setattr(r, "write_index", lambda d, outs: indexed.extend(outs))
    monkeypatch.setattr(r, "export_clips", lambda d, outs: None)
    p = {"demo_id": "d1", "out_dir": tmp_path / "out", "tickrate": 64}
    sqs = [{"n": n, "name": f"c{n}", "folder": tmp_path / f"seq{n}", "start": 0, "end": 640} for n in (1, 2)]
    seqs = [(p, sq) for sq in sqs]
    timing = []
    for sq in sqs:
        sq["folder"].mkdir()
    (sqs[0]["folder"] / "video.mp4").write_bytes(b"x" * 10)
    r._mux_finished(seqs, game_gone=False, timing=timing)
    now[0] = 108.0
    (sqs[0]["folder"] / "video.mp4").write_bytes(b"x" * 20)       # still growing, the next one not started
    r._mux_finished(seqs, game_gone=False, timing=timing)
    assert muxed == []
    now[0] = 109.0
    (sqs[1]["folder"] / "video.mp4").write_bytes(b"x")            # the next one started: the first is finished
    r._mux_finished(seqs, game_gone=False, timing=timing)
    assert muxed == [1] and indexed[0]["file"] == "1.mp4"
    assert timing[0]["seconds"] == 10.0 and timing[0]["wall"] == 8.0 and timing[0]["speed"] == 1.25
    r._mux_finished(seqs, game_gone=False, timing=timing)
    assert muxed == [1]                                           # the last one waits for the game to quit
    now[0] = 111.0
    r._mux_finished(seqs, game_gone=True, timing=timing)
    assert muxed == [1, 2] and len(timing) == 2


def test_game_state_ignores_a_failed_check_while_the_game_runs():
    from backend.record import GAME_GONE_CHECKS, game_state
    running, seen, gone = game_state(False, False, 0)
    assert (running, seen) == (False, False)                      # still starting: not seen yet
    running, seen, gone = game_state(True, seen, gone)
    assert running and seen and gone == 0
    for _ in range(GAME_GONE_CHECKS - 1):                         # a few failed checks (tasklist timing out)
        running, seen, gone = game_state(False, seen, gone)
        assert running
    running, seen, gone = game_state(True, seen, gone)            # the game was there all along
    assert running and gone == 0
    for _ in range(GAME_GONE_CHECKS):
        running, seen, gone = game_state(False, seen, gone)
    assert not running and seen                                   # really gone: the session ends


def test_recover_recordings_makes_clips_cs2_recorded_unwatched(tmp_path, monkeypatch):
    import json
    import backend.record as r
    vids = tmp_path / "videos"
    monkeypatch.setattr(r, "VIDEOS_DIR", vids)
    monkeypatch.setattr(r, "raw_dir_for", lambda d: vids / d / "raw")
    monkeypatch.setattr(r, "_job", None)
    monkeypatch.setattr(r, "export_clips", lambda d, outs: None)
    monkeypatch.setattr(r, "match_info", lambda d: None)
    monkeypatch.setattr(r, "export_folder_for", lambda d, cfg=None: tmp_path / "exp")
    running = [True]
    monkeypatch.setattr(r, "cs2_running", lambda: running[0])
    monkeypatch.setattr(r, "encode_one", lambda sq, out_dir, tr: {**r.planned_entry(sq, tr), "size": 1, "audio": True}
                        if (Path(sq["folder"]) / "video.mp4").exists() else None)
    raw = vids / "d1" / "raw"
    _, seqs = build_cfg([hl(1, 5000, 5640), hl(2, 9000, 9640)], raw, {}, {}, 64)
    p = {"demo_id": "d1", "tickrate": 64, "out_dir": vids / "d1", "take_root": tmp_path / "takes", "raw_dir": raw,
         "seqs": seqs}
    for sq in seqs:
        Path(sq["folder"]).mkdir(parents=True)
    (Path(seqs[0]["folder"]) / "video.mp4").write_bytes(b"v")      # CS2 got to the first highlight only
    r.write_planned(p)
    assert r.recover_recordings(["d1"]) == 0 and (raw / "planned.json").exists()   # the game still runs
    running[0] = False
    assert r.recover_recordings() == 1
    idx = json.loads((vids / "d1" / "videos.json").read_text())
    assert [v["highlight_id"] for v in idx["videos"]] == [1]
    assert not raw.exists()                                          # cleaned up like after a normal recording
    assert r.recover_recordings() == 0                               # nothing left to do


def test_song_drops_in_montage_follow_the_soundtrack():
    from backend.record import song_drops_in_montage
    songs = [([(0.0, 100.0)], [20.0, 60.0, 99.0, 120.0], [1.0, 0.5, 0.7, 0.9]),   # plays to 100 s: 120 is never heard
             ([(10.0, 90.0)], [12.0, 40.0], [0.8, 1.0])]                         # 12 s is inside the crossfade
    got = song_drops_in_montage(songs, offset=15.0)
    # song 1 from 15 s: 20 -> 5, 60 -> 45, 99 -> 84; song 2 starts at 85 - 3 = 82, its 40 s at 82 + 30 = 112
    assert got == [(5.0, 1.0), (45.0, 0.5), (84.0, 0.7), (112.0, 1.0)]
    assert song_drops_in_montage(songs, offset=50.0)[0] == (10.0, 0.5)     # drops before the offset are gone


def test_song_drops_skip_the_cut_quiet_stretch():
    from backend.record import song_drops_in_montage
    # one song in two pieces: 0-100 and 140-200 (the quiet 100-140 is cut); drops at 50, 120 (cut) and 160
    songs = [([(0.0, 100.0), (140.0, 200.0)], [50.0, 120.0, 160.0], [1.0, 1.0, 0.6])]
    assert song_drops_in_montage(songs, offset=0.0) == [(50.0, 1.0), (117.0, 0.6)]   # 160 -> 100 - 3 + 20


def test_bed_pieces_takes_what_the_montage_needs():
    from backend.record import bed_pieces
    songs = [("a", [(10.0, 100.0), (130.0, 190.0)]), ("b", [(5.0, 150.0)]), ("c", [(0.0, 120.0)])]
    used, n = bed_pieces(songs, offset=40.0, total=60.0)
    assert used == [("a", 40.0, 100.0), ("a", 130.0, 190.0)] and n == 1     # 60 s is not 62: the next piece too
    used, n = bed_pieces(songs, offset=40.0, total=200.0)
    assert [u[0] for u in used] == ["a", "a", "b"] and n == 2
    used, n = bed_pieces(songs, offset=40.0, total=1000.0)
    assert n == 3 and len(used) == 4                                          # the songs run out: all of them


def test_plan_song_order_puts_kills_on_the_drops_and_big_plays_on_big_drops():
    from backend.record import plan_song_order
    def play(base, hit):
        vs = [{"slow": True, "lead": None, "seconds": base + 4, "hit": hit}, {"slow": False, "lead": None, "seconds": base, "hit": None}]
        vs += [{"slow": True, "lead": l, "seconds": base + 4 + (l - 2), "hit": hit + (l - 2)}
               for l in [x * 0.25 for x in range(3, 17)] if abs(l - 2) > .01]
        return vs
    # a long play (20 s) and three short ones (10 s); kills 6 s into each
    variants = [play(10, 6), play(10, 6), play(20, 6), play(10, 6)]
    scores = [0.6, 0.6, 1.0, 0.6]
    drops = [(9.5, 1.0), (23.0, 0.4), (57.0, 1.0)]   # montage time: the opener's kill, then two more
    order, chosen, offset = plan_song_order(variants, scores, 3.5, lambda hit: 30.0 - hit, lambda off: drops)
    assert sorted(order) == [0, 1, 2, 3]
    t, hit_at = 3.5, {}
    for i in order:
        v = chosen[i]
        if v["slow"]:
            hit_at[i] = round(t + v["hit"], 2)
        t += v["seconds"]
    landed = [d for d, _ in drops if any(abs(h - d) <= 0.3 for h in hit_at.values())]
    assert landed == [9.5, 23.0, 57.0]                # every drop gets a kill
    assert offset == 30.0 - (3.5 + chosen[order[0]]["hit"])   # the opener's kill on the main drop


def test_plan_song_order_keeps_the_same_player_apart_when_it_costs_nothing():
    from backend.record import plan_song_order
    normal = lambda s: [{"slow": True, "lead": None, "seconds": s + 4, "hit": 5.0}, {"slow": False, "lead": None, "seconds": s, "hit": None}]  # noqa: E731
    variants = [normal(10), normal(10), normal(10)]
    order, _, _ = plan_song_order(variants, [1.0, 0.5, 0.5], 0.0, lambda hit: 0.0, lambda off: [(5.0, 1.0)],
                                  players=["a", "a", "b"])
    assert order == [0, 2, 1]


def test_song_span_cuts_the_quiet_intro_and_outro():
    from backend.cinematic import song_span
    curve = [0.02] * 20 + [0.3] * 300 + [0.05] * 30     # 10 s quiet intro, 150 s loud, 15 s fade-out (0.5 s steps)
    a, b = song_span(curve)
    assert 8.0 <= a <= 10.0 and 159.0 <= b <= 161.5   # a second or so of lead-in, the fade-out gone
    assert song_span([0.3] * 40) == (0.0, 20.0)         # too short to trim: the whole song


def test_plan_song_order_puts_the_biggest_play_on_the_biggest_drop():
    from backend.record import plan_song_order
    same = lambda: [{"slow": True, "lead": None, "seconds": 14.0, "hit": 6.0}, {"slow": False, "lead": None, "seconds": 10.0, "hit": None}]  # noqa: E731
    # three equal plays: kills at 6, 20 and 34 s when all are slow; the drop at 20 is small, the one at 34 big
    drops = [(6.0, 1.0), (20.0, 0.2), (34.0, 1.0)]
    order, chosen, _ = plan_song_order([same(), same(), same()], [0.6, 1.0, 0.6], 0.0, lambda hit: 0.0, lambda off: drops)
    assert order.index(1) in (0, 2)                    # the big play takes a big drop, not the small one
    assert all(chosen[i]["slow"] for i in order)       # and all three drops still get a kill


def test_song_pieces_cut_a_long_quiet_stretch_but_not_a_short_dip():
    from backend.cinematic import pieces_bed_args, pieces_length, song_pieces
    loud, quiet = [0.3], [0.03]
    curve = quiet * 10 + loud * 160 + quiet * 60 + loud * 120 + quiet * 6 + loud * 100 + quiet * 20   # 0.5 s steps
    ps = song_pieces(curve)
    # intro and outro gone; the 30 s quiet stretch at 85-115 s cut (1.5 s kept each side); the 3 s dip kept
    assert len(ps) == 2
    (a1, b1), (a2, b2) = ps
    assert 3.0 <= a1 <= 5.5 and 84.0 <= b1 <= 88.0 and 112.0 <= a2 <= 116.0 and 227.0 <= b2 <= 230.0
    assert pieces_length([(0.0, 50.0), (60.0, 100.0)]) == 87.0
    args = pieces_bed_args(Path("ff"), [(Path("a.mp3"), 4.0, 86.5), (Path("a.mp3"), 113.5, 200.0), (Path("b.mp3"), 0.0, 90.0)], Path("o.m4a"))
    assert args[:8] == ["ff", "-y", "-ss", "4.000", "-t", "82.500", "-i", "a.mp3"]
    assert args[args.index("-filter_complex") + 1] == "[0:a][1:a]acrossfade=d=3:c1=tri:c2=tri[x1];[x1][2:a]acrossfade=d=3:c1=tri:c2=tri[x2]"


def test_song_montage_ends_with_the_gg_card(tmp_path, monkeypatch):
    import backend.cinematic as cine
    import backend.record as rec
    monkeypatch.setattr(rec, "VIDEOS_DIR", tmp_path)
    monkeypatch.setattr(rec, "load_config", lambda: {"render_parallel": 1, "montage_outro": True})
    monkeypatch.setattr(rec, "load_team", lambda: {"team": "Your Team", "intro": True})
    monkeypatch.setattr(rec, "_squad_job", {"status": "running", "total": 2})
    (tmp_path / "d").mkdir()

    def fake(demo_id, v, ffmpeg, ffprobe, vdir, overrides=None, progress=None, montage_piece=False, **kw):
        (tmp_path / "d" / f"m{v['file']}").write_bytes(b"x")
        return {**v, "cine": f"m{v['file']}", "cine_info": {"seconds": 4.0, "name": v["file"]}}
    monkeypatch.setattr(cine, "cinematic_for_video", fake)
    cards = []

    def card(ffmpeg, ffprobe, piece, at, dst, team, sub, stats, crf=20):
        cards.append((piece.name, at, team, sub, stats))
        dst.write_bytes(b"c")
        return 3.5

    def outro(ffmpeg, ffprobe, piece, at, dst, team, stats, crf=20, players=None):
        cards.append((piece.name, at, "GG", team, stats))
        dst.write_bytes(b"c")
        return 4.5
    monkeypatch.setattr(cine, "make_intro", card)
    monkeypatch.setattr(cine, "make_outro", outro)
    joined = []
    monkeypatch.setattr(rec, "_join", lambda files, infos, out, lst: (joined.extend(f.name for f in files), out.write_bytes(b"j")))
    monkeypatch.setattr(cine, "pick_music", lambda opts, exclude=None: None)
    made = rec._render_song_montage([("d", {"file": "0.mp4"}), ("d", {"file": "1.mp4"})], None, tmp_path, tmp_path / "o.mp4",
                                    intro={"team": "Your Team", "sub": "Squad montage", "stats": "2 plays"})
    assert joined[0].startswith("montage_intro_") and joined[-1].startswith("montage_outro_")
    assert cards[-1] == ("m1.mp4", 2.5, "GG", "Your Team", "2 plays")       # 1.5 s before the end, not a faded frame
    assert made["seconds"] == 3.5 + 4.0 + 4.0 + 4.5


def test_snap_to_beat_cuts_the_tail_back_to_a_beat():
    from backend.record import snap_to_beat
    v = {"slow": False, "lead": None, "seconds": 10.0, "hit": None, "tail": 3.0}
    got = snap_to_beat(v, 5.0, [13.0, 14.6, 15.4])           # ends at 15.0: the beat at 14.6 is 0.4 s back
    assert got["seconds"] == 9.6 and got["trim"] == 0.4
    assert snap_to_beat(v, 5.0, [14.0, 15.5]) == v              # 1 s back is more than SNAP_MAX
    assert snap_to_beat({**v, "tail": 1.2}, 5.0, [14.6]) == {**v, "tail": 1.2}   # would leave < 1 s after the kill
    assert snap_to_beat(v, 5.0, []) == v


def test_plan_song_order_ends_plays_on_beats():
    from backend.record import plan_song_order
    same = lambda: [{"slow": True, "lead": None, "seconds": 14.0, "hit": 6.0, "tail": 4.0},  # noqa: E731
                    {"slow": False, "lead": None, "seconds": 10.0, "hit": None, "tail": 4.0}]
    beats = [round(0.5 * k, 3) for k in range(1, 200)]       # 120 BPM
    beats = [b for b in beats if abs(b % 1.0 - 0.5) < 1e-6]   # only the off-beats: x.5
    order, chosen, _ = plan_song_order([same(), same(), same()], [1.0, 0.6, 0.6], 0.0, lambda hit: 0.0,
                                       lambda off: [(6.0, 1.0)], beats_for=lambda off: beats)
    t = 0.0
    for i in order:
        t += chosen[i]["seconds"]
        assert abs(t % 1.0 - 0.5) < 1e-6                    # every cut lands on a beat


def test_montage_plan_puts_a_transition_between_every_two_plays(monkeypatch):
    import backend.record as rec
    monkeypatch.setattr(rec, "_plan_montage", lambda plays, ov, intro, reorder, follow: {
        "per_play": [{} for _ in plays], "order": [2, 0, 1], "song": None, "offset": None, "follow": None, "note": None})
    monkeypatch.setattr(rec, "load_config", lambda: {"montage_transition": "mixed", "montage_outro": False})
    pp = rec.plan_montage([("d", {}), ("d", {}), ("d", {})], None, False)["per_play"]
    assert pp[2] == {"t_out": "whip"} and pp[0] == {"t_in": "whip", "t_out": "zoom"} and pp[1] == {"t_in": "zoom"}
    monkeypatch.setattr(rec, "load_config", lambda: {"montage_transition": "mixed", "montage_outro": True})
    pp = rec.plan_montage([("d", {}), ("d", {}), ("d", {})], None, False)["per_play"]
    assert pp[1] == {"t_in": "zoom", "t_out": "flash"}                   # the last play flashes into the end card
    monkeypatch.setattr(rec, "load_config", lambda: {"montage_transition": "none"})
    assert rec.plan_montage([("d", {}), ("d", {})], None, False)["per_play"] == [{}, {}]


def test_a_recording_cut_off_early_is_marked_and_recorded_again(tmp_path, monkeypatch):
    import backend.record as r
    monkeypatch.setattr(r, "clip_seconds", lambda p: 37.2)
    e = r.check_length({"file": "x.mp4", "seconds": 67.2}, tmp_path / "x.mp4")
    assert e["short"] and e["seconds"] == 37.2 and e["expected"] == 67.2 and not r.is_recorded(e)
    monkeypatch.setattr(r, "clip_seconds", lambda p: 66.4)          # a frame or two short is fine
    assert r.is_recorded(r.check_length({"file": "x.mp4", "seconds": 67.2}, tmp_path / "x.mp4"))
    monkeypatch.setattr(r, "clip_seconds", lambda p: 0.0)           # could not measure: trust it
    assert r.is_recorded(r.check_length({"file": "x.mp4", "seconds": 67.2}, tmp_path / "x.mp4"))


def test_lineup_adds_up_each_players_kills():
    from backend.cinematic import lineup_players
    a = {"name": "invis", "photo": None, "steamid": "1"}
    b = {"name": "Ldr", "photo": "ldr.png", "steamid": "2"}
    got = lineup_players([(a, 3), (b, 4), (a, 2), (None, 5)])
    assert [(p["name"], p["kills"]) for p in got] == [("invis", 5), ("Ldr", 4)] and got[1]["photo"] == "ldr.png"
    many = [({"name": f"p{i}", "steamid": str(i)}, i) for i in range(10)]
    assert len(lineup_players(many)) == 8 and lineup_players(many)[0]["name"] == "p9"


def test_swoop_lands_on_the_players_eyes():
    from backend.record import campath_xml, plan_swoop, swoop_points
    eye = {"x": 100.0, "y": 200.0, "z": 64.0, "pitch": 5.0, "yaw": 90.0}     # looking along +y
    pts = swoop_points(eye, 1.8)
    assert pts[-1] == {"t": 1.8, "x": 100.0, "y": 200.0, "z": 64.0, "pitch": 5.0, "yaw": 90.0}   # exactly his view
    assert pts[0]["t"] == 0.0 and pts[0]["y"] < 200.0 - 100 and pts[0]["z"] > 64.0 + 100 and pts[0]["pitch"] > 20  # behind, above
    xml = campath_xml(pts)
    assert xml.startswith('<?xml version="1.0"?><campath><points><p t="0.0"') and 'ry="5.0" rz="90.0"' in xml
    h = {"kills": [10000, 10300]}
    assert plan_swoop(h, 9000, 64) == {"from": 9756, "to": 9872}               # 1.8 s = 115.2 ticks before landing
    assert plan_swoop(h, 9800, 64) is None                                   # no room inside the recording
    assert plan_swoop(h, 10000 - 256, 64) == {"from": 9756, "to": 9872}       # a clip starting 4 s before the kill fits
    assert plan_swoop({"kills": []}, 0, 64) is None


def test_build_cfg_schedules_the_swoop():
    sw = {0: {"from": 4700, "to": 4872, "xml": "C:/x/raw/swoop_0.xml"}}
    text, seqs = build_cfg([hl(0, 4600, 5600)], Path("C:/x/raw"), {}, {"record_swoop_spec": "spec_mode 5"}, 64, swoops=sw)
    assert 'alias hl_sw1 "spec_mode 5; mirv_campath clear; mirv_campath load C:/x/raw/swoop_0.xml; mirv_campath edit start; mirv_campath enabled 1"' in text
    assert "mirv_cmd addAtTick 4700 hl_sw1" in text and "mirv_cmd addAtTick 4872 hl_swe1" in text
    assert 'alias hl_swe1 "mirv_campath enabled 0; spec_mode 1;' in text
    assert seqs[0]["swoop"] == {"from": 1.562, "to": 4.25}
    text2, seqs2 = build_cfg([hl(0, 4800, 5600)], Path("C:/x/raw"), {}, {}, 64, swoops=sw)   # starts too late: no swoop
    assert "mirv_campath" not in text2 and "swoop" not in seqs2[0]


def test_montage_spaces_the_swoops_out():
    from backend.record import SWOOP_GAP, plan_song_order
    def play(sw):
        vs = [{"slow": True, "lead": None, "swoop": False, "seconds": 14.0, "hit": 6.0},
              {"slow": False, "lead": None, "swoop": False, "seconds": 10.0, "hit": None}]
        if sw:
            vs += [{"slow": True, "lead": None, "swoop": True, "seconds": 15.8, "hit": 7.8},
                   {"slow": False, "lead": None, "swoop": True, "seconds": 11.8, "hit": None}]
        return vs
    order, chosen, _ = plan_song_order([play(True) for _ in range(12)], [0.6] * 12, 0.0, lambda hit: 0.0, lambda off: [])
    flags = [bool(chosen[i].get("swoop")) for i in order]
    pos = [k for k, f in enumerate(flags) if f]
    assert len(pos) >= 2                                                    # some plays swoop...
    assert all(b - a > SWOOP_GAP for a, b in zip(pos, pos[1:]))             # ...never two close together
