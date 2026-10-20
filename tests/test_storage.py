from pathlib import Path

from backend import storage


def test_export_files_are_only_what_the_app_copied(tmp_path):
    base = tmp_path / "exports"
    m = base / "de_dust2_2026-10-17_1ba97380"
    mont = base / "montages"
    mine = base / "My stuff"
    for d in (m, mont, mine):
        d.mkdir(parents=True)
    for f in ("01_clutch_r8_qwq.mp4", "01_clutch_r8_qwq_cine.mp4", "long_3clips_cinematic_20261020_101010.mp4", "notes.txt"):
        (m / f).write_bytes(b"x")
    for f in ("squad_6matches_20261020_174426.mp4", "recap_20plays_x.mp4", "holiday.mp4"):
        (mont / f).write_bytes(b"x")
    (mine / "01_clutch_r8_qwq.mp4").write_bytes(b"x")       # an app-like name in a folder the app did not make
    (base / "squad_1.mp4").write_bytes(b"x")                 # loose in the base folder: not ours
    got = sorted(p.relative_to(base).as_posix() for p in storage.export_files(base))
    assert got == ["de_dust2_2026-10-17_1ba97380/01_clutch_r8_qwq.mp4", "de_dust2_2026-10-17_1ba97380/01_clutch_r8_qwq_cine.mp4",
                   "de_dust2_2026-10-17_1ba97380/long_3clips_cinematic_20261020_101010.mp4",
                   "montages/recap_20plays_x.mp4", "montages/squad_6matches_20261020_174426.mp4"]
    assert storage.export_files(tmp_path / "missing") == []


def test_clear_exports_leaves_your_files_and_refuses_while_busy(tmp_path, monkeypatch):
    base = tmp_path / "exports"
    m = base / "de_nuke_2026-10-20_6f88e856"
    m.mkdir(parents=True)
    (m / "01_clutch_r4_x.mp4").write_bytes(b"x" * 10)
    (m / "notes.txt").write_text("keep")
    monkeypatch.setattr(storage, "export_base", lambda cfg=None: base)
    monkeypatch.setattr(storage, "busy", lambda: "CS2 is open")
    try:
        storage.clear("exports")
        raise AssertionError("cleared while busy")
    except RuntimeError as e:
        assert "CS2 is open" in str(e)
    monkeypatch.setattr(storage, "busy", lambda: None)
    assert storage.clear("exports") == {"freed": 10, "files": 1}
    assert (m / "notes.txt").exists() and not (m / "01_clutch_r4_x.mp4").exists()
    try:
        storage.clear("videos")
        raise AssertionError("cleared the clips")
    except RuntimeError:
        pass
