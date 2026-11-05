"""Online cover search (iTunes -> Open Library -> Google Books), ranking and caching.

No API keys are needed. All network functions are blocking and must be run
off the main thread (see CoverFetcher / util.run_in_thread).
"""
import hashlib
import json
import logging
import queue
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from gi.repository import GLib, GObject

from .const import COVER_CACHE, USER_AGENT

log = logging.getLogger(__name__)

TIMEOUT = 10
SOURCE_BONUS = {"iTunes": 0.06, "Open Library": 0.02, "Google Books": 0.0}
AUTO_PICK_THRESHOLD = 0.62


class NetworkError(Exception):
    pass


@dataclass
class CoverResult:
    title: str
    author: str
    source: str
    thumb_url: str
    full_urls: list = field(default_factory=list)   # best first
    square: bool = False
    score: float = 0.0


# ------------------------------------------------------------------ ranking

_noise = re.compile(r"\b(?:unabridged|abridged|audiobook|audio book|a novel|the novel|edition|book \d+|"
                    r"volume \d+|vol \d+|narrated by .*)\b")


def normalize(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = text.casefold()
    text = re.sub(r"[\(\[].*?[\)\]]", " ", text)
    text = _noise.sub(" ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return " ".join(text.split())


def similarity(a, b):
    """0..1, tolerant to word order, subtitles and extra words."""
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))
    # "Dune" vs "Dune: Book One" - one title fully contained in the other
    head = 0.9 if (b.startswith(a) or a.startswith(b)) else 0.0
    return max(ratio, 0.85 * overlap, head)


def author_similarity(a, b):
    if not a or not b:
        return 0.5  # unknown: neither reward nor punish much
    # Results often list several authors; match against the best one.
    parts = [p for p in re.split(r",|&| and |;", b) if p.strip()] or [b]
    best = max(similarity(a, p) for p in parts)
    # "J. R. R. Tolkien" vs "Tolkien"
    last_a, last_b = normalize(a).split()[-1:], [normalize(p).split()[-1:] for p in parts]
    if last_a and last_a in last_b:
        best = max(best, 0.8)
    return best


def score_result(result, title, author):
    t = similarity(title, result.title)
    a = author_similarity(author, result.author)
    score = 0.65 * t + 0.30 * a + SOURCE_BONUS.get(result.source, 0.0)
    if result.square:
        score += 0.03
    if re.search(r"(?<!un)abridged", result.title, re.I) and not re.search(r"(?<!un)abridged", title or "", re.I):
        score -= 0.08
    return round(min(score, 1.0), 4)


def rank(results, title, author):
    for r in results:
        r.score = score_result(r, title, author)
    # stable sort keeps provider order for ties (iTunes first)
    return sorted(results, key=lambda r: r.score, reverse=True)


def best_match(results, title, author, threshold=AUTO_PICK_THRESHOLD):
    ranked = rank(list(results), title, author)
    return ranked[0] if ranked and ranked[0].score >= threshold else None


# ----------------------------------------------------------------- network

def http_get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise NetworkError(str(e)) from e


def _json(url):
    data, _ = http_get(url)
    return json.loads(data.decode("utf-8", "replace"))


def search_itunes(title, author):
    term = " ".join(filter(None, (title, author)))
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "media": "audiobook", "entity": "audiobook", "limit": 12})
    out = []
    for item in _json(url).get("results", []):
        art = item.get("artworkUrl100") or item.get("artworkUrl60")
        if not art:
            continue
        sizes = ["3000x3000bb", "1400x1400bb", "600x600bb"]
        full = [re.sub(r"\d+x\d+bb", s, art) for s in sizes] + [art]
        out.append(CoverResult(title=item.get("collectionName", ""), author=item.get("artistName", ""),
                               source="iTunes", thumb_url=re.sub(r"\d+x\d+bb", "300x300bb", art),
                               full_urls=full, square=True))
    return out


def search_openlibrary(title, author):
    params = {"title": title, "limit": 12, "fields": "title,author_name,cover_i"}
    if author:
        params["author"] = author
    url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(params)
    out = []
    for doc in _json(url).get("docs", []):
        cid = doc.get("cover_i")
        if not cid:
            continue
        base = f"https://covers.openlibrary.org/b/id/{cid}"
        out.append(CoverResult(title=doc.get("title", ""), author=", ".join(doc.get("author_name", [])[:3]),
                               source="Open Library", thumb_url=f"{base}-M.jpg?default=false",
                               full_urls=[f"{base}-L.jpg?default=false"]))
    return out


def search_google(title, author):
    q = f'intitle:"{title}"' + (f' inauthor:"{author}"' if author else "")
    url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode(
        {"q": q, "maxResults": 12, "printType": "books"})
    out = []
    for item in _json(url).get("items", []):
        info = item.get("volumeInfo", {})
        links = info.get("imageLinks") or {}
        thumb = links.get("thumbnail") or links.get("smallThumbnail")
        if not thumb:
            continue
        fix = lambda u: u.replace("http://", "https://").replace("&edge=curl", "")  # noqa: E731
        full = [fix(links[k]) for k in ("extraLarge", "large", "medium") if links.get(k)]
        full += [fix(thumb) + "&fife=w1200-h1200", fix(thumb).replace("zoom=1", "zoom=0"), fix(thumb)]
        out.append(CoverResult(title=info.get("title", ""), author=", ".join(info.get("authors", [])[:3]),
                               source="Google Books", thumb_url=fix(thumb), full_urls=full))
    return out


PROVIDERS = (search_itunes, search_openlibrary, search_google)


def _run_provider(provider, title, author):
    """-> (results, reachable)"""
    try:
        return provider(title, author), True
    except NetworkError as e:
        log.info("%s unreachable: %s", provider.__name__, e)
        return [], False
    except (urllib.error.HTTPError, ValueError, KeyError) as e:
        log.info("%s failed: %s", provider.__name__, e)
        return [], True


def search(title, author, limit=12, stop_when_good=False):
    """Query the providers (iTunes, Open Library, Google Books).

    Interactive searches query all of them in parallel; background auto-picks
    (``stop_when_good``) go one by one and stop at the first confident match,
    to be gentle with the free APIs. Raises NetworkError only if no provider
    could be reached at all.
    """
    if stop_when_good:
        outcomes = []
        for provider in PROVIDERS:
            outcomes.append(_run_provider(provider, title, author))
            if best_match(outcomes[-1][0], title, author, threshold=0.8):
                break
    else:
        with ThreadPoolExecutor(max_workers=len(PROVIDERS)) as pool:
            outcomes = list(pool.map(lambda p: _run_provider(p, title, author), PROVIDERS))
    if not any(reachable for _, reachable in outcomes):
        raise NetworkError("no cover source could be reached")
    results = [r for found, _ in outcomes for r in found]  # provider order kept for ties
    return rank(results, title, author)[:limit]


def sniff_mime(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def fetch_image(urls, min_bytes=2048):
    """Download the first URL that yields a real image. Returns (bytes, mime)."""
    last = None
    for url in urls:
        try:
            data, _ = http_get(url, timeout=20)
        except (urllib.error.HTTPError, NetworkError) as e:
            last = e
            continue
        mime = sniff_mime(data)
        if mime and len(data) >= min_bytes:
            return data, mime
    if isinstance(last, NetworkError):
        raise last
    raise ValueError("no usable image found")


def save_to_cache(book_path, data, mime):
    COVER_CACHE.mkdir(parents=True, exist_ok=True)
    ext = {"image/png": ".png", "image/webp": ".webp"}.get(mime, ".jpg")
    digest = hashlib.sha1(book_path.encode()).hexdigest()
    for old in COVER_CACHE.glob(f"{digest}-*"):
        old.unlink(missing_ok=True)
    dest = COVER_CACHE / f"{digest}-{int(time.time())}{ext}"
    dest.write_bytes(data)
    return str(dest)


class CoverFetcher(GObject.Object):
    """Background queue that auto-picks covers for books without one."""

    __gsignals__ = {"cover-found": (GObject.SignalFlags.RUN_FIRST, None, (int, str)),
                    "cover-missed": (GObject.SignalFlags.RUN_FIRST, None, (int,))}

    def __init__(self):
        super().__init__()
        self._queue = queue.Queue()
        self._pending = set()
        self._offline_until = 0.0
        threading.Thread(target=self._worker, daemon=True).start()

    def enqueue(self, book_id, book_path, title, author):
        if book_id in self._pending:
            return
        self._pending.add(book_id)
        self._queue.put((book_id, book_path, title, author))

    def _worker(self):
        while True:
            book_id, path, title, author = self._queue.get()
            try:
                if time.time() < self._offline_until:
                    continue
                match = best_match(search(title, author, stop_when_good=True), title, author)
                if match is None:
                    log.info("no confident cover match for %r", title)
                    GLib.idle_add(lambda b=book_id: (self.emit("cover-missed", b), False)[1])
                    continue
                data, mime = fetch_image(match.full_urls)
                dest = save_to_cache(path, data, mime)
                log.info("cover for %r from %s (score %.2f)", title, match.source, match.score)
                GLib.idle_add(lambda b=book_id, d=dest: (self.emit("cover-found", b, d), False)[1])
            except NetworkError:
                log.info("offline; pausing automatic cover search")
                self._offline_until = time.time() + 300
            except Exception:  # noqa: BLE001
                log.exception("cover fetch failed for %r", title)
            finally:
                self._pending.discard(book_id)
                time.sleep(0.5)  # be polite to the free APIs
