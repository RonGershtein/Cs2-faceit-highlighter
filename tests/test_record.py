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
