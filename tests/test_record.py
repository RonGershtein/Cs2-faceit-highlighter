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
    assert "mirv_cmd addAtTick 100 demo_gototick 4951" in text          # first jump from tick 100 to setup-1
    assert "mirv_cmd addAtTick 4952 hl_s1" in text
    assert "mirv_cmd addAtTick 5000 mirv_streams record start" in text
    assert "mirv_cmd addAtTick 5600 mirv_streams record end" in text
    assert "mirv_cmd addAtTick 5616 demo_gototick 8951" in text         # next jump 16 ticks after previous end
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
                      {"highlight_id": 2, "file": "b.mp4", "start_tick": 2}], "reel": {"file": "reel.mp4"},
           "long": [{"file": "long_x.mp4"}]}
    (d / "videos.json").write_text(json.dumps(idx))
    r = rec.delete_clips("demo1", [1, 99], downloads=False)
    assert r["deleted"] == [1] and r["files"] == 2
    assert not (d / "a.mp4").exists() and not (d / "a_cine.mp4").exists() and (d / "b.mp4").exists()
    assert (tmp_path / "dl" / "a.mp4").exists()  # Downloads copy kept
    after = rec.videos_index("demo1")
    assert [v["highlight_id"] for v in after["videos"]] == [2] and after["reel"] and after["long"]
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
