from lectern.covers import CoverResult, author_similarity, best_match, normalize, rank, similarity


def R(title, author, source="iTunes", square=True):
    return CoverResult(title=title, author=author, source=source, thumb_url="", full_urls=[], square=square)


def test_normalize():
    assert normalize("Dune (Unabridged)") == "dune"
    assert normalize("  The Hobbit: Or There and Back Again ") == "the hobbit or there and back again"
    assert normalize("Café Society") == "cafe society"


def test_similarity_tolerates_subtitles_and_noise():
    assert similarity("Dune", "Dune: Book One") >= 0.9
    assert similarity("Project Hail Mary", "Project Hail Mary (Unabridged)") == 1.0
    assert similarity("Dune", "Children of Time") < 0.4


def test_author_similarity():
    assert author_similarity("J. R. R. Tolkien", "J.R.R. Tolkien") > 0.8
    assert author_similarity("Andy Weir", "Ray Porter, Andy Weir") > 0.9
    assert author_similarity("Tolkien", "J. R. R. Tolkien") >= 0.8
    assert author_similarity("", "Anyone") == 0.5


def test_rank_prefers_matching_title_and_author():
    results = [
        R("The Martian", "Andy Weir"),
        R("Project Hail Mary", "Somebody Else"),
        R("Project Hail Mary", "Andy Weir", source="Open Library", square=False),
        R("Project Hail Mary (Unabridged)", "Andy Weir"),
    ]
    ranked = rank(results, "Project Hail Mary", "Andy Weir")
    assert ranked[0].title.startswith("Project Hail Mary") and ranked[0].author == "Andy Weir"
    assert ranked[0].source == "iTunes"               # square audiobook art wins ties
    assert ranked[-1].title == "The Martian"
    assert [r.score for r in ranked] == sorted((r.score for r in ranked), reverse=True)


def test_abridged_penalised():
    ranked = rank([R("The Hobbit (Abridged)", "J. R. R. Tolkien"), R("The Hobbit", "J. R. R. Tolkien")],
                  "The Hobbit", "J. R. R. Tolkien")
    assert ranked[0].title == "The Hobbit"


def test_best_match_threshold():
    assert best_match([R("Completely Different", "Nobody")], "Dune", "Frank Herbert") is None
    assert best_match([R("Dune", "Frank Herbert")], "Dune", "Frank Herbert").title == "Dune"
    assert best_match([], "Dune", "Frank Herbert") is None
