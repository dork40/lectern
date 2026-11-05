import pytest

from lectern.metadata import clean_track_title, guess_from_path, parse_name, split_series_suffix
from lectern.scanner import group_files, looks_like_parts
from lectern.util import natural_key


@pytest.mark.parametrize("name,expected", [
    ("Author Name - The Title", {"author": "Author Name", "title": "The Title"}),
    ("Book 3 - The Title", {"series_index": "3", "title": "The Title"}),
    ("03 The Lantern Keeper", {"series_index": "3", "title": "The Lantern Keeper"}),
    ("Brandon Sanderson - Stormlight 2 - Words of Radiance",
     {"author": "Brandon Sanderson", "series": "Stormlight", "series_index": "2", "title": "Words of Radiance"}),
    ("The Hobbit (Narrated by Andy Serkis) (2020)", {"year": "2020", "narrator": "Andy Serkis", "title": "The Hobbit"}),
    ("Dune [Unabridged]", {"title": "Dune"}),
    ("1984", {"title": "1984"}),
])
def test_parse_name(name, expected):
    assert parse_name(name) == expected


def test_guess_from_hierarchy(tmp_path):
    book = tmp_path / "Jonah Pike" / "Harbor Lights" / "Book 2 - The Lantern Keeper"
    book.mkdir(parents=True)
    assert guess_from_path(book, tmp_path) == {
        "author": "Jonah Pike", "series": "Harbor Lights", "series_index": "2", "title": "The Lantern Keeper"}
    single = tmp_path / "Mara Ellison" / "The Quiet Orbit.m4b"
    assert guess_from_path(single, tmp_path) == {"author": "Mara Ellison", "title": "The Quiet Orbit"}


def test_natural_sort():
    assert sorted(["Part 10", "Part 2", "part 1"], key=natural_key) == ["part 1", "Part 2", "Part 10"]


def test_clean_track_title():
    assert clean_track_title("01 - Intro") == "Intro"
    assert clean_track_title("Part 3") == "Part 3"


def test_looks_like_parts():
    assert looks_like_parts(["/b/01 Intro.mp3", "/b/02 The Road.mp3"])
    assert looks_like_parts(["/b/Part 1.mp3", "/b/Part 2.mp3"])
    assert looks_like_parts(["/b/Dune 01.mp3", "/b/Dune 02.mp3"])
    assert not looks_like_parts(["/a/The Hobbit.mp3", "/a/The Silmarillion.mp3"])


def test_group_files():
    albums = {"/a/x.mp3": "dune", "/a/y.mp3": "dune", "/a/z.mp3": "emma", "/a/w.mp3": ""}
    album = albums.get
    # m4b files always stand alone; numbered parts form one book
    assert group_files(["/a/one.m4b", "/a/01.mp3", "/a/02.mp3"], album) == [["/a/one.m4b"], ["/a/01.mp3", "/a/02.mp3"]]
    # unnumbered files: grouped by album tag, the rest are separate books
    assert group_files(["/a/x.mp3", "/a/y.mp3", "/a/z.mp3", "/a/w.mp3"], album) == [
        ["/a/x.mp3", "/a/y.mp3"], ["/a/z.mp3"], ["/a/w.mp3"]]
    assert group_files(["/a/w.mp3"], album) == [["/a/w.mp3"]]


@pytest.mark.parametrize("title,expected", [
    ("The Glass Lantern 9: A Harbor Mystery (The Glass Lantern Saga, Book 9)",
     ("The Glass Lantern 9: A Harbor Mystery", "The Glass Lantern Saga", "9")),
    ("Words of Radiance (The Stormlight Archive, Book 2)", ("Words of Radiance", "The Stormlight Archive", "2")),
    ("Leviathan Wakes [Expanse #1]", ("Leviathan Wakes", "Expanse", "1")),
    ("Dune (Unabridged)", ("Dune (Unabridged)", None, None)),
    ("Plain Title", ("Plain Title", None, None)),
])
def test_split_series_suffix(title, expected):
    assert split_series_suffix(title) == expected
