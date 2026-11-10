"""Any round of any player as a clip with an adjustable start/end."""
import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def test_round_clip_add_edit_delete(tmp_path, monkeypatch):
    from backend import app as appmod, db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_conn", None)
    (tmp_path / "d1").mkdir()
    meta = {"players": [{"steamid": "7656", "name": "Ron"}],
            "rounds": [{"num": 3, "freeze_end": 1000, "official_end": 1000 + 64 * 60}]}
    (tmp_path / "d1" / "meta.json").write_text(json.dumps(meta))
    monkeypatch.setattr(appmod, "PARSED_DIR", tmp_path)
    monkeypatch.setattr(db, "get_demo", lambda i: {"id": i, "tickrate": 64})
    c = TestClient(appmod.app)

    h = c.post("/api/demos/d1/highlights/round", json={"player": "7656", "round": 3}).json()
    assert (h["type"], h["start_tick"], h["end_tick"], h["id"]) == ("round", 1000, 1000 + 64 * 60, 1)
    assert c.post("/api/demos/d1/highlights/round", json={"player": "7656", "round": 9}).status_code == 400

    h = c.put(f"/api/demos/d1/highlights/{h['id']}/round", json={"player": "7656", "round": 3, "start": -2, "end": 10}).json()
    assert (h["start_tick"], h["end_tick"]) == (1000 - 128, 1000 + 640)
    assert [x["start_tick"] for x in db.get_highlights("d1")] == [872]
    assert c.delete("/api/demos/d1/highlights/1").status_code == 200 and db.get_highlights("d1") == []
