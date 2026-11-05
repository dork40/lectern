"""Player in cast mode, driven by a simulated Chromecast session."""
import gi

gi.require_version("Gst", "1.0")

import pytest  # noqa: E402

from lectern.models import Book, Chapter, Track  # noqa: E402
from lectern.player import Player  # noqa: E402


class Status:
    def __init__(self, state, reason=None):
        self.player_state, self.idle_reason = state, reason


class FakeSession:
    def __init__(self):
        self.calls = []
        self.on_status = None
        self.pos = None

    def load(self, book, track, local, autoplay, rate, effects=None):
        self.calls.append(("load", track.path, round(local, 2), autoplay, rate))

    def play(self):
        self.calls.append(("play",))

    def pause(self):
        self.calls.append(("pause",))

    def seek(self, local):
        self.calls.append(("seek", round(local, 2)))

    def set_rate(self, r):
        self.calls.append(("rate", r))

    def set_fade(self, v):
        self.calls.append(("fade", v))

    def stop(self, quit_app=True):
        self.calls.append(("stop",))

    def position(self):
        return self.pos

    def status(self, state, reason=None):
        self.on_status(Status(state, reason))


@pytest.fixture
def player():
    p = Player()
    tracks = [Track("/b/1.mp3", 100, 0), Track("/b/2.mp3", 50, 100)]
    book = Book(1, "/b", "folder", "Book", duration=150, tracks=tracks,
                chapters=[Chapter("1", 0, 100), Chapter("2", 100, 150)])
    p.book = book            # no local pipeline needed for these tests
    p.track_index = 0
    p._last_pos = 40.0
    yield p
    p.remote = None
    p.shutdown()


def test_cast_starts_at_current_position(player):
    s = FakeSession()
    player._want_playing = True
    player.set_rate(1.5)
    player.start_casting(s)
    assert s.calls[-1] == ("load", "/b/1.mp3", 40.0, True, 1.5)
    assert player.casting and player.position == 40.0      # until the device reports


def test_position_follows_device_and_seeks_across_files(player):
    s = FakeSession()
    player.start_casting(s)
    s.status("PLAYING")
    s.pos = 63.0
    assert player.position == 63.0
    player.seek(70)                    # same file: a remote seek
    assert s.calls[-1] == ("seek", 70.0)
    player.seek(120)                   # other file: load it at the right offset
    assert s.calls[-1][:3] == ("load", "/b/2.mp3", 20.0)
    assert player.position == 120.0


def test_next_file_and_book_end(player):
    s = FakeSession()
    player.start_casting(s)
    s.status("PLAYING")
    s.status("IDLE", "FINISHED")
    assert s.calls[-1] == ("load", "/b/2.mp3", 0.0, True, 1.0)
    finished = []
    player.connect("book-finished", lambda *_: finished.append(True))
    s.status("PLAYING")
    s.status("IDLE", "FINISHED")
    assert finished and player.position == 150


def test_follows_pause_from_tv_remote(player):
    s = FakeSession()
    player._want_playing = True
    player.start_casting(s)
    s.status("PLAYING")
    s.status("PAUSED")
    assert not player.playing


def test_controls_go_to_device_and_fade(player):
    s = FakeSession()
    player.start_casting(s)
    s.status("PAUSED")
    player.play()
    player.pause()
    player.set_fade(0.5)
    assert ("play",) in s.calls and ("pause",) in s.calls and ("fade", 0.5) in s.calls


def test_stop_casting_resumes_locally_at_same_place(player):
    s = FakeSession()
    player.start_casting(s)
    s.status("PLAYING")
    s.pos = 88.0
    opened = []
    player._open_track = lambda idx, local: opened.append((idx, round(local, 1)))
    player.stop_casting()
    assert ("stop",) in s.calls and not player.casting
    assert opened == [(0, 88.0)]
