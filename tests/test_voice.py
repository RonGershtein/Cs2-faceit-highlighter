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


def test_pick_respects_pins(vdir):
    put(vdir, "final", "", "a.mp3")
    put(vdir, "final", "", "b.mp3")
    put(vdir, "final", "clutch", "c.mp3")
    assert voice.pick("final", ["clutch"], pinned=["a.mp3"]).name == "a.mp3"          # pinned beats the type folder
    assert voice.pick("final", ["clutch"], pinned=["a.mp3", "clutch/c.mp3"]).name == "c.mp3"
    assert voice.pick("final", [], pinned=["missing.mp3"]) is None
    assert voice.parse_pins('{"final": ["a.mp3"], "bogus": 1}') == {"start": [], "kill": [], "final": ["a.mp3"]}
    assert voice.parse_pins("not json") == {"start": [], "kill": [], "final": []}
    assert voice.parse_pins({"kill": ["2/x.mp3", 5]}) == {"start": [], "kill": ["2/x.mp3"], "final": []}


def test_caster_fx_args(tmp_path):
    bed = tmp_path / "bed.mp3"
    bed.write_bytes(b"x")
    a = voice.caster_fx_args(Path("ffmpeg"), tmp_path / "in.mp3", tmp_path / "out.mp3", bed)
    assert a[:3] == ["ffmpeg", "-y", "-i"] and "-stream_loop" in a and str(bed) in a
    g = a[a.index("-filter_complex") + 1]
    assert "acompressor" in g and "aecho" in g and "amix=inputs=2" in g and "loudnorm" in g and "[1:a]aresample=44100,volume=0.22[c]" in g
    a = voice.caster_fx_args(Path("ffmpeg"), tmp_path / "in.wav", tmp_path / "out.mp3", None)
    assert "anoisesrc=color=pink" in " ".join(a) and "tremolo" in a[a.index("-filter_complex") + 1]


def test_other_voice_differs_from_the_first():
    assert voice.other_voice("en-US-GuyNeural") == "en-US-ChristopherNeural"
    assert voice.other_voice("en-US-ChristopherNeural") == "en-US-GuyNeural"
    assert voice.other_voice("windows") == "windows"
    assert voice.voice_label("en-US-GuyNeural") == "Guy" and voice.voice_label("x") == "x"
