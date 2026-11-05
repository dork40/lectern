import os
from bisect import bisect_right
from dataclasses import dataclass, field


@dataclass
class Track:
    path: str
    duration: float
    offset: float = 0.0
    title: str = ""


@dataclass
class Chapter:
    title: str
    start: float
    end: float

    @property
    def duration(self):
        return self.end - self.start


@dataclass
class Book:
    id: int | None
    path: str
    kind: str                      # "file" or "folder"
    title: str
    author: str = ""
    narrator: str = ""
    series: str = ""
    series_index: str = ""
    year: str = ""
    duration: float = 0.0
    cover_path: str | None = None
    tracks: list = field(default_factory=list)
    chapters: list = field(default_factory=list)
    added: float = 0.0
    position: float = 0.0
    last_played: float = 0.0
    finished: bool = False
    audio: dict | None = None      # per-book effect settings, None = use global
    overrides: dict = field(default_factory=dict)
    rate: float | None = None      # per-book speed, None = use the default speed

    @property
    def progress(self):
        if self.finished:
            return 1.0
        return min(1.0, self.position / self.duration) if self.duration else 0.0

    @property
    def status(self):
        if self.finished:
            return "finished"
        return "in-progress" if self.position > 1 else "not-started"

    def chapter_index(self, pos):
        if not self.chapters:
            return -1
        starts = [c.start for c in self.chapters]
        return max(0, bisect_right(starts, pos + 1e-6) - 1)

    def locate(self, pos):
        """Global position -> (track index, position inside that track)."""
        if not self.tracks:
            return 0, 0.0
        for i, t in enumerate(self.tracks):
            if pos < t.offset + t.duration or i == len(self.tracks) - 1:
                return i, max(0.0, pos - t.offset)
        return 0, 0.0

    @property
    def folder(self):
        """Directory holding the book's files."""
        if self.kind == "folder":
            return self.path.split("::")[0]
        return os.path.dirname(self.path)

    @property
    def search_text(self):
        return " ".join(filter(None, (self.title, self.author, self.narrator, self.series))).casefold()
