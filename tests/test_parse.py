"""Integration test against a real demo. Set CS2_SAMPLE_DEMO=/path/to/match.dem(.zst); skipped otherwise."""
import os
from pathlib import Path

import pytest

SAMPLE = os.environ.get("CS2_SAMPLE_DEMO")
pytestmark = pytest.mark.skipif(not SAMPLE or not Path(SAMPLE).exists(), reason="CS2_SAMPLE_DEMO not set")


@pytest.fixture(scope="module")
def parsed(tmp_path_factory):
    from backend.ingest import decompress_zst
    from backend.parser import parse_demo

    src = Path(SAMPLE)
    if src.name.endswith(".zst"):
        dem = tmp_path_factory.mktemp("demo") / "sample.dem"
        decompress_zst(src, dem)
    else:
        dem = src
    return parse_demo(str(dem), step=8)


def test_meta(parsed):
    meta = parsed["meta"]
    assert meta["map"].startswith(("de_", "cs_"))
    assert len(meta["players"]) == 10
    assert len(meta["rounds"]) >= 16
    assert meta["score"]["a"] + meta["score"]["b"] == len(meta["rounds"])
    assert all(r["start"] < r["freeze_end"] <= r["end"] <= r["official_end"] for r in meta["rounds"])


def test_kills_inside_rounds(parsed):
    kills = parsed["events"]["kills"]
    rounds = {r["num"]: r for r in parsed["meta"]["rounds"]}
    assert len(kills) > 0
    for k in kills:
        r = rounds[k["round"]]
        assert r["start"] <= k["tick"] <= r["official_end"]
        assert k["vpos"] is not None


def test_frames(parsed):
    frames = parsed["frames"]
    assert len(frames) > 1000
    assert all(frames[i]["t"] < frames[i + 1]["t"] for i in range(len(frames) - 1))
    f = frames[len(frames) // 2]
    assert 8 <= len(f["p"]) <= 10
    for p in f["p"]:
        assert p[8] in ("CT", "T")


def test_stats_consistent(parsed):
    stats = parsed["stats"]
    kills = [k for k in parsed["events"]["kills"] if k["attacker"] and k["attacker_team"] != k["victim_team"]]
    assert sum(s["kills"] for s in stats) == len(kills)
    assert sum(s["deaths"] for s in stats) == len(parsed["events"]["kills"])
    for s in stats:
        assert 0 <= s["kast"] <= 100
