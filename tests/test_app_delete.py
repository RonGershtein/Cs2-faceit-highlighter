"""Deleting several matches at once."""
import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def test_delete_several_matches_at_once(monkeypatch):
    from backend import app as appmod
    have = {"a": {"id": "a"}, "b": {"id": "b"}, "c": {"id": "c"}}
    gone = []
    monkeypatch.setattr(appmod.db, "get_demo", lambda i: have.get(i))
    monkeypatch.setattr(appmod.ingest, "delete", lambda i: (gone.append(i), have.pop(i)))
    monkeypatch.setattr(appmod, "rec", None)          # no clip stamping in this test
    client = TestClient(appmod.app)
    r = client.post("/api/demos/delete", json={"ids": ["a", "c", "a", "zzz"]})
    assert r.status_code == 200 and r.json() == {"deleted": 2, "missing": ["zzz"]}
    assert gone == ["a", "c"] and list(have) == ["b"]
