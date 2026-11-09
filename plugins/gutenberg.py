"""Project Gutenberg plugin.

A *live* provider: search(query, config) queries Gutendex (the official JSON
API for Project Gutenberg) and returns matching books. Most books have a PDF
edition (opened in the reader); books without one fall back to their
plain-text / HTML / EPUB edition, which the UI opens in a new tab.

Define search(query, config) -> list[dict]. Each item is one of:
  {"name": str, "url": str}      a PDF to open (downloaded on click)
  {"name": str, "magnet": str}   a torrent to track
  {"name": str, "path": str}     a local PDF to read in place
An item may also carry a "meta" string shown as a subtitle line.

CONFIG exposes settings the UI renders and passes back as the `config` dict.
"""
import base64
import io
import json
import posixpath
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

NAME = "Project Gutenberg"
DESCRIPTION = "Search the Project Gutenberg book archive (via Gutendex) and read the matching books as PDFs."

SEARCH_URL = "https://gutendex.com/books"
UA = {"User-Agent": "pdf-torrent-reader/1.0"}


def _get_json(url, timeout=20):
    """GET a URL and parse JSON.

    Gutendex (behind Cloudflare) is fast when healthy (~0.1s) but goes through
    bad windows that last minutes, during which it holds the connection open
    with no response. That held-open connection defeats urllib's per-socket
    timeout — each recv() stays under it, so a single request can otherwise run
    for minutes (a 120s-timeout request was observed running 229s). The body is
    therefore read in chunks against a hard total deadline, so the request is
    bounded to ~`timeout` seconds no matter how the server behaves.

    There is deliberately no retry: during a bad window a retry just fails the
    same way, so a single bounded attempt that fails fast with a clear message
    is better than hanging. The front end keeps the previous results on screen,
    so the user can just re-run the search once Gutendex recovers.

    HTTP errors (503/502/429/…) are the same "Gutendex is unhealthy" situation
    as a timeout — Cloudflare returns them during bad windows — so they get the
    same friendly message instead of a raw "HTTP Error 503".
    """
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            deadline = time.time() + timeout
            chunks = []
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if time.time() > deadline:
                    raise TimeoutError("response body exceeded the deadline")
            return json.loads(b"".join(chunks))
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"Gutendex returned HTTP {e.code} — it is likely rate-limiting or "
            "down; try again in a moment") from e
    except Exception as e:
        msg = str(e).lower()
        if isinstance(e, TimeoutError) or "timed out" in msg or "timeout" in msg:
            raise RuntimeError(
                "Gutendex is not responding (timed out) — it is likely "
                "rate-limiting or down; try again in a moment") from e
        raise

# Settings the UI renders and passes to search(). These map onto Gutendex's
# own query parameters (sort, languages, topic, copyright, author years).
CONFIG = [
    {"key": "sort", "label": "Sort by", "type": "select",
     "default": "popular",
     "options": [
         {"value": "popular", "label": "Most popular (downloads)"},
         {"value": "ascending", "label": "ID ascending"},
         {"value": "descending", "label": "ID descending"},
         {"value": "title-asc", "label": "Title A\u2013Z"},
         {"value": "title-desc", "label": "Title Z\u2013A"},
         {"value": "author-asc", "label": "Author A\u2013Z"},
         {"value": "author-desc", "label": "Author Z\u2013A"},
     ]},
    {"key": "format", "label": "Format", "type": "select",
     "default": "any",
     "options": [
         {"value": "any", "label": "Any (PDF preferred)"},
         {"value": "pdf", "label": "PDF"},
         {"value": "text", "label": "Plain text"},
         {"value": "html", "label": "HTML"},
         {"value": "epub", "label": "EPUB"},
     ]},
    {"key": "language", "label": "Language", "type": "select",
     "default": "",
     "options": [
         {"value": "", "label": "Any"},
         {"value": "en", "label": "English"},
         {"value": "fr", "label": "French"},
         {"value": "de", "label": "German"},
         {"value": "es", "label": "Spanish"},
         {"value": "it", "label": "Italian"},
         {"value": "pt", "label": "Portuguese"},
     ]},
    {"key": "topic", "label": "Topic / bookshelf", "type": "text",
     "default": "", "placeholder": "e.g. children, science"},
    {"key": "copyright", "label": "Copyright", "type": "select",
     "default": "",
     "options": [
         {"value": "", "label": "Any"},
         {"value": "false", "label": "Public domain (USA)"},
         {"value": "true", "label": "Copyrighted"},
         {"value": "null", "label": "Unknown"},
     ]},
    {"key": "author_era", "label": "Author era", "type": "select",
     "default": "",
     "options": [
         {"value": "", "label": "Any era"},
         {"value": "ancient", "label": "Ancient (before 500 CE)"},
         {"value": "medieval", "label": "Medieval (500\u20131400)"},
         {"value": "early-modern", "label": "Early modern (1400\u20131800)"},
         {"value": "19th", "label": "19th century (1800\u20131899)"},
         {"value": "20th", "label": "20th century (1900\u20131999)"},
         {"value": "modern", "label": "Modern (1900+)"},
     ]},
]

# author_era select value -> (author_year_start, author_year_end); None = omit.
AUTHOR_ERAS = {
    "ancient": (None, 499),
    "medieval": (500, 1400),
    "early-modern": (1400, 1800),
    "19th": (1800, 1899),
    "20th": (1900, 1999),
    "modern": (1900, None),
}


def _book_name(b: dict) -> str:
    title = (b.get("title") or "").strip()
    authors = ", ".join(a.get("name", "") for a in b.get("authors", []) if a.get("name"))
    return f"{title} — {authors}" if authors else title


def _author_key(b: dict) -> str:
    # sort by the first listed author's name (case-insensitive)
    for a in b.get("authors", []):
        if a.get("name"):
            return a["name"].lower()
    return ""


def _book_meta(b: dict) -> str:
    parts = []
    langs = b.get("languages") or []
    if langs:
        parts.append("/".join(langs))
    dl = b.get("download_count")
    if isinstance(dl, (int, float)):
        parts.append(f"{dl:,} downloads")
    return " · ".join(parts)


# format select value -> the kinds to return when the user picks a specific
# format (instead of "any").
FORMATS = {
    "pdf": [("application/pdf", "pdf")],
    "text": [("text/plain; charset=utf-8", "text"), ("text/plain", "text")],
    "html": [("text/html", "html")],
    "epub": [("application/epub+zip", "epub")],
}


# mime -> kind label for the editions the UI can offer. Everything else a
# book carries (RDF metadata, cover JPEGs) is not a readable edition.
MIME_KINDS = {
    "application/pdf": "pdf",
    "text/plain": "text",
    "text/html": "html",
    "application/epub+zip": "epub",
    "application/x-mobipocket-ebook": "mobi",
    "application/octet-stream": "file",  # Gutenberg's pgNN-h.zip (HTML bundle)
}

# listing order when a book has several editions of the same kind
KIND_ORDER = {"pdf": 0, "text": 1, "html": 2, "epub": 3, "mobi": 4, "file": 5}


def _edition_name(url: str) -> str:
    # the file's basename (e.g. "84.txt.utf-8") — distinguishes editions that
    # share a kind, like the two plain-text variants of one book
    return urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] or url


def _book_editions(formats: dict, fmt: str = "any") -> list[dict]:
    """Every readable edition a book offers, each as {"url", "kind", "name"},
    so the UI can list them all in one result box. Repeated kinds (e.g. two
    text files) are all kept, told apart by filename. With a specific fmt only
    that kind is returned."""
    wanted = None
    if fmt in FORMATS:
        wanted = {kind for _, kind in FORMATS[fmt]}
    out = []
    for mime, url in formats.items():
        base = mime.split(";")[0].strip()  # "text/plain; charset=utf-8" -> "text/plain"
        kind = MIME_KINDS.get(base)
        if not kind or (wanted and kind not in wanted):
            continue
        out.append({"url": url, "kind": kind, "name": _edition_name(url)})
    out.sort(key=lambda e: KIND_ORDER.get(e["kind"], 99))
    return out


def search(query: str, config: dict) -> list[dict]:
    """Search Gutendex and return matching books in the requested format
    (default "any" = PDF, else text/HTML/EPUB)."""
    params = {}
    q = (query or "").strip()
    if q:
        params["search"] = q
    sort = (config or {}).get("sort") or "popular"
    # Gutendex only sorts server-side by popularity or ID; title/author sorts
    # are applied to the returned page below.
    params["sort"] = sort if sort in ("ascending", "descending", "popular") else "popular"
    lang = (config or {}).get("language") or ""
    if lang:
        params["languages"] = lang
    topic = (config or {}).get("topic") or ""
    if topic.strip():
        params["topic"] = topic.strip()
    copyright = (config or {}).get("copyright") or ""
    if copyright in ("true", "false", "null"):
        params["copyright"] = copyright
    era = (config or {}).get("author_era") or ""
    era_range = AUTHOR_ERAS.get(era)
    if era_range:
        start, end = era_range
        if start is not None:
            params["author_year_start"] = start
        if end is not None:
            params["author_year_end"] = end
    url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
    data = _get_json(url)
    results = data.get("results", [])
    # client-side sorts Gutendex can't do server-side (reorder the returned page)
    if sort == "title-asc":
        results.sort(key=lambda b: (b.get("title") or "").lower())
    elif sort == "title-desc":
        results.sort(key=lambda b: (b.get("title") or "").lower(), reverse=True)
    elif sort == "author-asc":
        results.sort(key=_author_key)
    elif sort == "author-desc":
        results.sort(key=_author_key, reverse=True)
    fmt = (config or {}).get("format") or "any"
    items = []
    for b in results:
        name = _book_name(b)
        if not name:
            continue
        editions = _book_editions(b.get("formats") or {}, fmt)
        if not editions:
            continue  # no usable edition at all
        url, kind = editions[0]["url"], editions[0]["kind"]
        item = {"name": name, "url": url, "kind": kind}
        if len(editions) > 1:
            # the UI lists every edition in an expandable list in the same box
            item["editions"] = editions
        meta = _book_meta(b)
        if kind != "pdf":
            meta = f"{meta} · {kind}" if meta else kind
        if meta:
            item["meta"] = meta
        items.append(item)
    return items


# The cover is the book's real cover art, embedded in its EPUB (the edition
# whose URL ends in ".epub3.images") — richer than Gutenberg's auto-generated
# text cover. Extracting it means downloading the whole EPUB, so oversized
# EPUBs (and any download failure) fall back to the generated cover.
EPUB_COVER_MAX_BYTES = 15 * 1024 * 1024


def _epub_url(item: dict) -> str:
    """The book's EPUB edition URL (kind "epub"), from the primary url or the
    editions list."""
    if item.get("kind") == "epub":
        return item.get("url", "")
    for ed in item.get("editions") or []:
        if ed.get("kind") == "epub":
            return ed.get("url", "")
    return ""


def _generated_cover(url: str) -> str:
    """Gutenberg's auto-generated cover, derived from the book id in any of its
    URLs (/ebooks/<id>..., /files/<id>/..., /cache/epub/<id>/...)."""
    m = re.search(r"/(?:ebooks|files|cache/epub)/(\d+)", url)
    return (f"https://www.gutenberg.org/cache/epub/{m.group(1)}/pg{m.group(1)}.cover.medium.jpg"
            if m else "")


def _opf_cover_href(opf_text: str) -> str:
    """The cover image's href from an EPUB's OPF. EPUB3 marks it with
    properties="cover-image"; EPUB2 uses <meta name="cover" content="ID"/>."""
    for tag in re.findall(r"<item\b[^>]*>", opf_text):
        if "cover-image" in tag:
            m = re.search(r'href="([^"]+)"', tag)
            if m:
                return m.group(1)
    mc = re.search(r'<meta\b[^>]*name="cover"[^>]*content="([^"]+)"', opf_text)
    if mc:
        cid = mc.group(1)
        for tag in re.findall(r"<item\b[^>]*>", opf_text):
            if f'id="{cid}"' in tag:
                m = re.search(r'href="([^"]+)"', tag)
                if m:
                    return m.group(1)
    return ""


def _epub_cover_data_uri(epub_url: str) -> str:
    """Download an EPUB and return its embedded cover image as a data URI, or
    "" if the EPUB has no cover or is too large to be worth downloading."""
    req = urllib.request.Request(epub_url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        if int(r.headers.get("Content-Length") or 0) > EPUB_COVER_MAX_BYTES:
            return ""
        data = r.read(EPUB_COVER_MAX_BYTES + 1)
    if len(data) > EPUB_COVER_MAX_BYTES:
        return ""
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    opf = next((n for n in names if n.lower().endswith(".opf")), None)
    if not opf:
        return ""
    href = _opf_cover_href(zf.read(opf).decode("utf-8", "replace"))
    if not href:
        return ""
    opf_dir = opf.rsplit("/", 1)[0] + "/" if "/" in opf else ""
    cover_path = posixpath.normpath(
        (opf_dir + href) if not href.startswith("/") else href).lstrip("/")
    if cover_path not in names:
        return ""
    img = zf.read(cover_path)
    mime = "image/png" if cover_path.lower().endswith(".png") else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(img).decode('ascii')}"


def covers(items: list) -> list:
    """Cover image for each search result, aligned by index with `items`.

    This is the second, parallel query of a live search: the list + metadata
    come from search(), and the app calls covers() separately right after the
    list loads so a cover lookup never delays the results.

    Each cover is the book's real cover art, extracted from its EPUB (the
    edition whose URL ends in ".epub3.images") — richer than Gutenberg's
    auto-generated text cover. The lookups run in parallel (each downloads an
    EPUB) and fall back to the generated cover (pg<id>.cover.medium.jpg) when a
    book has no EPUB, its EPUB is oversized, or the download fails.
    """
    def one(it: dict) -> str:
        it = it or {}
        epub = _epub_url(it)
        if epub:
            try:
                du = _epub_cover_data_uri(epub)
                if du:
                    return du
            except Exception:
                pass  # fall through to the generated cover
        return _generated_cover(epub or it.get("url", ""))

    if not items:
        return []
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(one, items))
