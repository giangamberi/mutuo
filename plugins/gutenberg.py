"""Project Gutenberg plugin.

A *live* provider: search(query, config) queries Gutendex (the official JSON
API for Project Gutenberg) and returns matching books as PDFs. Gutenberg's own
site asks you not to scrape it, so we use Gutendex instead. Not every book has
a PDF, so books without one are skipped.

Define search(query, config) -> list[dict]. Each item is one of:
  {"name": str, "url": str}      a PDF to open (downloaded on click)
  {"name": str, "magnet": str}   a torrent to track
  {"name": str, "path": str}     a local PDF to read in place
An item may also carry a "meta" string shown as a subtitle line.

CONFIG exposes settings the UI renders and passes back as the `config` dict.
"""
import json
import urllib.parse
import urllib.request

NAME = "Project Gutenberg"
DESCRIPTION = "Search the Project Gutenberg book archive (via Gutendex) and read the matching books as PDFs."

SEARCH_URL = "https://gutendex.com/books"
UA = {"User-Agent": "pdf-torrent-reader/1.0"}

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


def search(query: str, config: dict) -> list[dict]:
    """Search Gutendex and return matching books that have a PDF edition."""
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
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.load(resp)
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
    items = []
    for b in results:
        pdf_url = b.get("formats", {}).get("application/pdf")
        if not pdf_url:
            continue  # this book has no PDF edition
        name = _book_name(b)
        if not name:
            continue
        item = {"name": name, "url": pdf_url}
        meta = _book_meta(b)
        if meta:
            item["meta"] = meta
        items.append(item)
    return items
