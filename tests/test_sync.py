"""Music sync: the song analysis (beats, bars, drops), kills put on the beat, and the effects around them."""
import math
from pathlib import Path

import pytest

from backend import cinematic as c
from backend import songs

np = pytest.importorskip("numpy")


def made_up_song(sr=songs.SR, bpm=120.0, seconds=40.0, drop=16.0, offset=0.0):
    """A kick on every beat (quiet before `drop`, loud with a bass line from it on)."""
    n = int(sr * seconds)
    y = np.zeros(n)
    beat = 60.0 / bpm
    k = 0
    while offset + k * beat < seconds - 0.3:
        at = offset + k * beat
        i0, ln = int(at * sr), int(0.25 * sr)
        tt = np.arange(ln) / sr
        ph = 2 * np.pi * np.cumsum(50 + 70 * np.exp(-tt / 0.03)) / sr
        y[i0:i0 + ln] += (0.9 if at >= drop else 0.12) * np.sin(ph) * np.exp(-tt / 0.12)
        k += 1
    t = np.arange(n) / sr
    y += np.where(t >= drop, 0.35, 0.0) * np.sin(2 * np.pi * 55 * t) + np.where(t < drop, 0.05, 0.0) * np.sin(2 * np.pi * 440 * t)
    return (y / np.abs(y).max() * 0.9).astype(np.float32)


def test_analysis_finds_the_beat_the_bars_and_the_drop_exactly():
    a = songs.analyze_signal(made_up_song())
    assert abs(a["bpm"] - 120) < 1.5
    b = np.array(a["beats"])
    err = b - np.round(b * 2) / 2                    # the beats are on the half seconds
    assert np.median(np.abs(err)) < 0.012
    assert any(abs(d - 16.0) < 0.02 for d in a["auto_drops"])     # the drop, on its beat (not one early)
    assert all(abs(((d + 0.01) % 2.0) - 0.01) < 0.03 for d in a["downbeats"][:6])   # bars every 2 s from 0
    assert -9 <= a["gain_db"] <= 6 and a["wave"] and max(a["wave"]) <= 1.0


def test_refine_beats_moves_a_rough_beat_onto_the_hit():
    sr = songs.SR
    x = np.zeros(sr * 3, dtype=np.float32)
    n = int(0.1 * sr)
    x[int(1.5 * sr):int(1.5 * sr) + n] = 0.8 * np.sin(np.arange(n) * 0.3)
    got = songs.refine_beats(x, [1.43, 2.5], sr)
    assert abs(got[0] - 1.5) < 0.012
    assert got[1] == 2.5                             # nothing to hit near it: left alone


def test_drops_are_snapped_onto_bars_and_meet_once():
    beats = [i * 0.5 for i in range(40)]
    downs = beats[0::4]
    got = songs.snap_drops([(7.6, 0.9), (8.1, 0.5), (13.0, 0.4)], beats, downs)
    assert got == [(8.0, 0.9), (13.0, 0.4)]          # 7.6 and 8.1 meet on the bar at 8; 13 is two beats from a bar


def test_downbeat_phase_follows_the_drops():
    beats = [i * 0.5 for i in range(32)]
    assert songs.downbeat_phase(beats, [(4.5, 1.0), (12.5, 0.5)], [1.0] * 32) == 1      # 4.5 s is beat 9 -> 9 % 4
    assert songs.downbeat_phase(beats, [], [1.0 if i % 4 == 2 else 0.1 for i in range(32)]) == 2


def test_pieces_start_on_a_drop_or_a_bar():
    downs = [i * 2.0 for i in range(60)]
    # a piece starting near a drop comes in on it (the crossfade ends there); others on a bar; ends on a bar
    assert songs.snap_pieces([(0.0, 41.3), (46.5, 100.0)], downs, 120.0, drops=[48.0]) == [(0.0, 42.0), (45.0, 100.0)]
    assert songs.snap_pieces([(0.0, 40.0), (50.7, 100.0)], downs, 120.0) == [(0.0, 40.0), (51.0, 100.0)]


def test_beat_grid_adds_half_beats_to_a_slow_beat():
    assert songs.beat_grid([0.0, 0.5, 1.0]) == [0.0, 0.5, 1.0]
    assert songs.beat_grid([0.0, 0.7, 1.4]) == [0.0, 0.35, 0.7, 1.05, 1.4]


def test_song_gain_evens_out_loudness():
    quiet, loud = [0.03] * 100, [0.6] * 100
    assert songs.song_gain(quiet) == songs.GAIN_MAX and songs.song_gain(loud) < 0
    assert songs.song_gain([10 ** (songs.TARGET_DB / 20)] * 50) == 0.0


def test_main_drop_is_the_strongest_that_fits():
    info = {"duration": 100.0, "drops": [10.0, 30.0, 90.0], "strengths": [0.5, 1.0, 0.9]}
    assert songs.main_drop(info, hit=5.0, clip_len=20.0) == 30.0
    assert songs.main_drop(info, hit=35.0, clip_len=20.0) is None or songs.main_drop(info, hit=35.0, clip_len=20.0) == 90.0
    assert songs.main_drop({"duration": 10, "drops": [], "strengths": []}, 1.0, 5.0) is None


def test_hand_placed_drops_replace_the_detected_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(songs, "MARKS_JSON", tmp_path / "marks.json")
    assert songs.marks_for("a.mp3") is None
    songs.set_marks("a.mp3", [30.0, 12.5])
    assert songs.marks_for("a.mp3") == [12.5, 30.0]
    info = {"auto_drops": [12.4], "auto_strengths": [0.8]}
    assert songs.user_strengths([12.5, 30.0], info) == [0.8, songs.BIG_DROP]
    songs.forget("a.mp3")
    assert songs.marks_for("a.mp3") is None


def test_retime_lands_each_kill_where_it_is_asked():
    segs = [(0.0, 4.0, 1.0), (4.0, 5.0, 0.35), (5.0, 9.0, 1.0)]
    got = c.retime(segs, [(4.5, 4.8), (8.0, 9.9)])
    assert abs(c.out_time(got, 4.5) - 4.8) < 1e-6 and abs(c.out_time(got, 8.0) - 9.9) < 1e-6
    # out of reach: as close as the limits allow, never past them
    far = c.retime(segs, [(4.5, 1.0)])
    assert all(s / o >= c.RETIME_REAL[0] - 1e-9 for (_, _, s), (_, _, o) in zip(far, c.split_at_src(segs, 4.5)))


def test_retime_takes_it_from_the_fast_forward_first():
    segs = [(0.0, 6.0, 6.0), (6.0, 8.0, 1.0)]          # 1 s of fast-forward, then 2 s real
    got = c.retime(segs, [(7.0, 2.6)])                   # 0.6 s more before the kill
    assert abs(c.out_time(got, 7.0) - 2.6) < 1e-6
    assert got[0][2] < 6.0 and abs(got[1][2] - 1.0) < 1e-9


def test_absorb_slivers_keeps_the_length_and_leaves_no_empty_segment():
    segs = [(0.0, 2.0, 1.0), (2.0, 2.02, 6.0), (5.0, 5.01, 1.0), (5.01, 7.0, 0.5)]
    got = c.absorb_slivers(segs)
    assert abs(c.total_duration(got) - c.total_duration(segs)) < 1e-9
    assert all((b - a) / s >= c.SLIVER for a, b, s in got)


def test_beat_anchors_put_kills_on_the_beat_or_the_drop():
    grid = [i * 0.5 for i in range(60)]
    # play starts at 10 s; its kills play at 2.3, 3.7 and 5.0 s of it
    got = c.beat_anchors([5.0, 6.4, 7.7], [2.3, 3.7, 5.0], [False, False, True], 10.0, grid, drop_at=15.3)
    # 12.5 for the first; 13.5 would squeeze 1.4 s into 1.0 (beyond the limit): 14.0; the slow kill on the drop
    assert got == [(5.0, 2.5), (6.4, 4.0), (7.7, 5.3)]
    # a kill too close to the one before for any beat: a half beat, or its own time
    got = c.beat_anchors([5.0, 5.6], [2.0, 2.6], [False, False], 0.0, grid)
    assert got[0] == (5.0, 2.0) and (len(got) == 1 or got[1][1] in (2.5, 2.75))
    # a single clip: the first slow kill stays (the song was started for it)
    assert c.beat_anchors([5.0], [3.21], [True], 0.0, grid, first_slow_fixed=True) == [(5.0, 3.21)]


def test_clip_timing_follows_the_retime():
    opts = {"cine_slow": 0.35, "cine_kills": "last", "cine_trim": "fast", "cine_trim_lead": 2.0, "cine_fast": 6.0,
            "cine_ramp": True}
    plain = c.clip_timing([5.0, 6.2], 12.0, opts)
    want = [(5.0, plain["outs"][0] + 0.2), (6.2, plain["outs"][1] + 0.3)]
    got = c.clip_timing([5.0, 6.2], 12.0, {**opts, "_retime": want})
    assert [round(x, 4) for x in got["outs"]] == [round(w, 4) for _, w in want]
    assert got["slows"] == [False, True]


def test_sfx_events_whoosh_into_slow_motion_and_hit_on_the_kill():
    segs = [(0.0, 3.0, 1.0), (3.0, 3.3, 0.6), (3.3, 4.5, 0.35), (4.5, 6.0, 1.0)]
    ev = c.sfx_events(segs, [4.0])
    kinds = {k: (at, skip) for k, at, skip in ev}
    assert kinds["impact"][0] == round(c.out_time(segs, 4.0), 3)
    from backend.sfx import WHOOSH_SECONDS
    assert abs(kinds["whoosh"][0] - (3.0 - WHOOSH_SECONDS)) < 1e-3     # peaks where the ramp into slow motion starts


def test_drop_fx_builds_up_into_the_drop():
    filt, risers = c.drop_fx([10.0], 0.5, 30.0)
    assert "lowpass@dfx0" in filt and "asendcmd" in filt and "aeval" in filt
    assert "10.000 lowpass@dfx0 mix 0" in filt          # opens on the drop
    (at, skip), = risers
    assert abs(at + (2.4 - skip) - 10.0) < 1e-6          # the riser ends on the drop
    assert c.drop_fx([0.5], 0.5, 30.0) == ("", [])        # no room before it


def test_buildup_drops_keeps_them_rare():
    landed = [(5.0, 0.4), (12.0, 0.9), (25.0, 1.0), (31.0, 0.5), (45.0, 0.7)]
    assert c.buildup_drops(landed) == [5.0, 25.0, 45.0]


def test_pulse_filter_flashes_on_the_bars():
    assert c.pulse_filter([]) == ""
    f = c.pulse_filter([1.0, 3.0])
    assert f.startswith("eq=") and "between(t\\,1.000\\,1.160)" in f


def test_montage_music_ducks_and_lays_risers(tmp_path):
    args = c.montage_music_args(Path("ff"), Path("v.mp4"), Path("m.mp3"), Path("o.mp4"), 60.0, 12.0, 0.7,
                                gain_db=2.5, music_fx="anull", risers=[(8.0, 0.4), (30.0, 0.4)],
                                riser=Path("r.wav"), duck_shots=True)
    g = args[args.index("-filter_complex") + 1]
    assert "volume=2.50dB" in g and "sidechaincompress" in g and "asplit=2" in g
    assert "[2:a]aformat=channel_layouts=stereo,asplit=2" in g and "adelay=8000" in g and "amix=inputs=4" in g
    assert args.count("-i") == 3


def test_pieces_bed_args_evens_the_songs_out():
    args = c.pieces_bed_args(Path("ff"), [(Path("a.mp3"), 0.0, 50.0), (Path("b.mp3"), 10.0, 60.0)], Path("o.m4a"),
                             gains=[3.0, 0.0])
    g = args[args.index("-filter_complex") + 1]
    assert g.startswith("[0:a]volume=3.00dB[s0];[s0][1:a]acrossfade")


def test_built_in_effects_are_made_once(tmp_path, monkeypatch):
    from backend import sfx
    monkeypatch.setattr(sfx, "SFX_DIR", tmp_path)
    for slot in sfx.SLOTS:
        p = sfx.file_for(slot)
        assert p.is_file() and p.stat().st_size > 10000
    import wave
    with wave.open(str(sfx.file_for("whoosh"))) as w:
        assert abs(w.getnframes() / w.getframerate() - sfx.WHOOSH_SECONDS) < 0.01
    sfx.save("impact", "boom.mp3", b"ID3fake")
    assert sfx.file_for("impact").name == "impact.mp3" and sfx.listing()["slots"][0]["file"] == "impact.mp3"
    sfx.reset("impact")
    assert sfx.file_for("impact").parent.name == "builtin"
    with pytest.raises(ValueError):
        sfx.save("impact", "x.txt", b"no")


def test_tune_to_music_lands_the_drop_kill_on_its_frame():
    from backend.record import tune_to_music
    opts = {"cine_slow": 0.35, "cine_kills": "last", "cine_trim": "fast", "cine_trim_lead": 2.0, "cine_fast": 6.0,
            "cine_ramp": True}

    def var(kills, dur, slow=True):
        o = opts if slow else {**opts, "_no_slow": True}
        return {"slow": slow, "lead": None, **c.clip_timing(kills, dur, o), "_opts": o, "_kills": kills, "_dur": dur}
    v0, v1 = var([5.0, 6.0], 12.0), var([4.0], 10.0, slow=False)
    hit = v0["hit"]
    drop = 3.5 + hit + 0.2                       # 0.2 s off: the plan's tolerance
    grid = [3.5 + i * 0.5 for i in range(80)]
    tuned, landed = tune_to_music([0, 1], [v0, v1], 3.5, [(drop, 1.0)], grid, grid, fps=60.0)
    assert landed == [(round(drop, 3), 1.0)]
    assert abs(3.5 + tuned[0]["hit"] - drop) < 1e-4
    t1 = 3.5 + tuned[0]["seconds"]
    assert abs(round(tuned[0]["seconds"] * 60) - tuned[0]["seconds"] * 60) < 1e-6     # whole frames
    assert min(abs(t1 + tuned[1]["outs"][0] - g) for g in grid) < 1e-4                 # the next play's kill on a beat


def test_first_pieces_start_where_the_plan_starts_the_song():
    from backend.record import first_pieces, song_drops_in_montage
    assert first_pieces([(15.0, 42.0), (47.0, 94.0)], 9.0) == [(9.0, 42.0), (47.0, 94.0)]
    assert first_pieces([(15.0, 42.0), (47.0, 94.0)], 44.0) == [(44.0, 94.0)]
    # the drop at 16 s plays 7 s in when the song starts at 9 s (not 1 s in, as if the build-up were cut)
    assert song_drops_in_montage([([(15.0, 42.0)], [16.0], [1.0])], 9.0) == [(7.0, 1.0)]


def test_concat_exact_lays_the_sound_end_to_end(tmp_path):
    from backend.record import concat_exact_args
    args = concat_exact_args([tmp_path / "a.mp4", tmp_path / "b.mp4"], [3.5, 10.016667], tmp_path / "l.txt", tmp_path / "o.mp4")
    g = args[args.index("-filter_complex") + 1]
    assert "[1:a]atrim=end=3.500000" in g and "[2:a]atrim=end=10.016667" in g and "concat=n=2:v=0:a=1[a]" in g
    assert args[args.index("-map") + 1] == "0:v" and "copy" in args


def test_punch_rates_montage_songs_above_a_fan_chant():
    assert songs.punch(2.67, 0.75) > 0.66 > songs.punch(2.13, 0.60) > songs.punch(1.87, 0.57)
    assert 0.0 <= songs.punch(0.0, 0.0) < 0.01 and songs.punch(5.0, 3.0) > 0.99


def test_random_order_lets_strong_songs_take_turns():
    punches = {"hard": 0.9, "mid": 0.6, "soft": 0.15}
    assert songs.rank_for_random(["soft", "mid", "hard"], punches, []) == ["hard", "mid", "soft"]
    assert songs.rank_for_random(["soft", "mid", "hard"], punches, ["mid", "hard"])[0] == "mid"   # hard just played
    assert songs.rank_for_random(["soft", "mid", "hard"], punches, ["mid", "hard"])[-1] == "soft"


def test_a_song_can_leave_random_picks(tmp_path, monkeypatch):
    monkeypatch.setattr(songs, "MARKS_JSON", tmp_path / "marks.json")
    songs.set_marks("a.mp3", [10.0])
    songs.set_in_random("a.mp3", False)
    assert not songs.in_random("a.mp3") and songs.marks_for("a.mp3") == [10.0]   # one keeps the other
    songs.set_marks("a.mp3", None)
    assert not songs.in_random("a.mp3")
    songs.set_in_random("a.mp3", True)
    assert songs.in_random("a.mp3") and not (tmp_path / "marks.json").read_text().strip("{}\n ")
