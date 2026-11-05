import urllib.request

import pytest

from lectern.cast import MediaServer, content_type, parse_range


@pytest.mark.parametrize("header,expected", [
    ("bytes=0-99", (0, 99)), ("bytes=100-", (100, 999)), ("bytes=-100", (900, 999)),
    ("bytes=990-5000", (990, 999)), ("bytes=1000-", None), ("bytes=5-2", None), ("", None), ("junk", None),
])
def test_parse_range(header, expected):
    assert parse_range(header, 1000) == expected


def test_content_types():
    assert content_type("/a/b.m4b") == "audio/mp4"
    assert content_type("/a/b.MP3") == "audio/mpeg"
    assert content_type("/a/b.opus") == "audio/ogg"
    assert content_type("/a/cover.jpg") == "image/jpeg"


def test_server_serves_only_registered_files_with_ranges(tmp_path):
    f = tmp_path / "book.mp3"
    f.write_bytes(bytes(range(256)) * 4)          # 1024 bytes
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    server = MediaServer(port=0)
    try:
        url = server.url(str(f), "127.0.0.1")
        assert url.endswith(".mp3") and "/media/" in url
        with urllib.request.urlopen(url) as r:
            assert r.status == 200 and len(r.read()) == 1024
            assert r.headers["Content-Type"] == "audio/mpeg"
        req = urllib.request.Request(url, headers={"Range": "bytes=256-511"})
        with urllib.request.urlopen(req) as r:
            assert r.status == 206
            assert r.headers["Content-Range"] == "bytes 256-511/1024"
            assert r.read() == bytes(range(256))
        base = url.rsplit("/", 1)[0]
        for bad in ("secret.txt", "..%2Fsecret.txt", "book.mp3"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(f"{base}/{bad}")
            assert e.value.code == 404
        assert server.register(str(f)) == url.rsplit("/", 1)[1]   # same token for the same file
        server.clear()
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(url)                            # revoked after clear()
    finally:
        server.shutdown()
