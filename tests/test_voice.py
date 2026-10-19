from pathlib import Path

import pytest

from backend import cinematic as c
from backend import voice


@pytest.fixture
def vdir(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "VOICE_DIR", tmp_path)
    return tmp_path


def put(vdir, slot, sub, name):
    d = vdir / slot / sub if sub else vdir / slot
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"x")


def test_pick_prefers_matching_subfolder_then_generic(vdir):
    put(vdir, "final", "", "generic.mp3")
    put(vdir, "final", "clutch", "clutch.mp3")
    assert voice.pick("final", ["ace", "clutch"]).name == "clutch.mp3"   # ace folder empty -> clutch
    assert voice.pick("final", ["ace"]).name == "generic.mp3"
    assert voice.pick("final").name == "generic.mp3"
    assert voice.pick("start") is None


def test_listing_names_are_slot_relative(vdir):
    put(vdir, "kill", "", "a.mp3")
    put(vdir, "kill", "3", "b.wav")
    put(vdir, "kill", "", "notes.txt")
    assert voice.listing()["slots"]["kill"] == ["a.mp3", "3/b.wav"]


def test_split_name_and_subfolder_validation():
    assert voice.split_name("ace/x.mp3") == ("ace", "x.mp3")
    assert voice.split_name("x.mp3") == ("", "x.mp3")
    for bad in ("../x.mp3", "a/b/c.mp3", "", "ace/../x.mp3"):
        with pytest.raises(ValueError):
            voice.split_name(bad)
    with pytest.raises(ValueError):
        voice.slot_dir("final", "Bad Name")


def test_voice_prefs_follow_the_highlight():
    assert c.voice_prefs({"type": "clutch", "n": 3}) == ["clutch"]
    assert c.voice_prefs({"type": "multikill", "n": 5}) == ["ace", "multikill"]
    assert c.voice_prefs({"type": "multikill", "n": 3}) == ["multikill"]
    assert c.voice_prefs({"type": "kill"}) == []
    assert c.voice_prefs(None) == []


def test_plan_voices_uses_type_and_kill_number(vdir):
    put(vdir, "start", "ace", "s.mp3")
    put(vdir, "kill", "", "k.mp3")
    put(vdir, "kill", "2", "k2.mp3")
    put(vdir, "final", "ace", "f.mp3")
    segs = [(0.0, 20.0, 1.0)]
    out = c.plan_voices(segs, [2.0, 5.0, 9.0, 14.0], 20.0, {"type": "ace", "n": 5})
    names = [p.name for p, _ in out]
    assert names == ["s.mp3", "k.mp3", "k2.mp3", "k.mp3", "f.mp3"]   # 1st kill generic, 2nd kill its folder, 3rd generic
    assert [round(t, 2) for _, t in out] == [0.3, 2.15, 5.15, 9.15, 14.4]
