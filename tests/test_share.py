import json

import pytest

from backend import share


def test_normalize_youtube_accepts_video_links_only():
    assert share.normalize_youtube(" https://youtu.be/dQw4w9WgXcQ ") == "https://youtu.be/dQw4w9WgXcQ"
    assert share.normalize_youtube("youtube.com/watch?v=dQw4w9WgXcQ&t=3") == "https://youtube.com/watch?v=dQw4w9WgXcQ&t=3"
    assert share.normalize_youtube("https://www.youtube.com/shorts/abcDEF12345").endswith("abcDEF12345")
    for bad in ("", "https://evil.example/watch?v=x", "https://youtube.com/channel/UC123", "javascript:alert(1)"):
        with pytest.raises(ValueError):
            share.normalize_youtube(bad)


def test_video_path_stays_inside_the_videos_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(share, "VIDEOS_DIR", tmp_path)
    (tmp_path / "montages").mkdir()
    (tmp_path / "montages" / "m.mp4").write_bytes(b"x")
    assert share.video_path("montages", "m.mp4") == tmp_path / "montages" / "m.mp4"
    for d, f in (("montages", "../secret.txt"), ("..", "m.mp4"), ("montages", "missing.mp4"), ("a/b", "m.mp4")):
        with pytest.raises(ValueError):
            share.video_path(d, f)


def test_link_is_saved_and_the_montage_described(tmp_path, monkeypatch):
    monkeypatch.setattr(share, "VIDEOS_DIR", tmp_path)
    monkeypatch.setattr(share, "DATA_DIR", tmp_path)
    m = tmp_path / "montages"
    m.mkdir()
    (m / "squad_1.mp4").write_bytes(b"x")
    (m / "montages.json").write_text(json.dumps([{"file": "squad_1.mp4", "description": "Squad montage · 2 plays",
                                                  "clips": [{"description": "3K by A"}, {"description": "ace by B"}]}]))
    (tmp_path / "squad.json").write_text(json.dumps({"team": "Your Team"}))
    info = share.video_info("montages", "squad_1.mp4")
    assert info["title"] == "Your Team · Squad montage · 2 plays" and info["link"] is None
    assert "• 3K by A" in info["description"] and "• ace by B" in info["description"]
    assert share.save_link("montages", "squad_1.mp4", "youtu.be/abcdef123") == "https://youtu.be/abcdef123"
    assert share.video_info("montages", "squad_1.mp4")["link"] == "https://youtu.be/abcdef123"


def test_email_message_carries_the_link():
    m = share.email_message("me@gmail.com", "Squad montage", "https://youtu.be/x123456", "2 plays")
    assert m["To"] == m["From"] == "me@gmail.com" and m["Subject"] == "Squad montage"
    assert "https://youtu.be/x123456" in m.get_content()
