"""Google Books plugin.

A *live* provider: search(query, config) queries the Google Books API and
returns matching books. Books with a free PDF (public-domain / open books —
Google Books' full view) link to the PDF, which the reader downloads and
opens. Books without a free PDF are skipped by default (they only have a
limited preview); enable "Include non-PDF" to list them as HTML previews.

Define search(query, config) -> list[dict]. Each item is:
  {"name": str, "url": str, "kind": "pdf"|"html", "meta": str}

API key: the API works without a key, but keyless calls share one global
daily quota (1000 queries/day across ALL keyless users), which is frequently
exhausted — the search then fails with a clear "quota" message. Adding your
own free key (console.cloud.google.com -> enable "Books API") gives your own
quota; paste it in the "API key" setting.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

NAME = "Google Books"
DESCRIPTION = "Search Google Books and open the free PDFs (public-domain / open books)."

API = "https://www.googleapis.com/books/v1/volumes"
UA = {"User-Agent": "pdf-torrent-reader/1.0"}

# sort select value -> (orderBy, sortOrder); None = API default (relevance)
_SORTS = {
    "relevance": (None, None),
    "newest": ("newest", None),
    "title": ("title", "asc"),
}

# Settings the UI renders and passes to search() as the `config` dict.
CONFIG = [
    {"key": "api_key", "label": "API key", "type": "text", "default": "",
     "placeholder": "optional — avoids the shared keyless quota"},
    {"key": "sort", "label": "Sort by", "type": "select", "default": "relevance",
     "options": [
         {"value": "relevance", "label": "Relevance"},
         {"value": "newest", "label": "Newest"},
         {"value": "title", "label": "Title A-Z"},
     ]},
    {"key": "language", "label": "Language", "type": "select", "default": "",
     "options": [
         {"value": "", "label": "Any"},
         {"value": "en", "label": "English"},
         {"value": "fr", "label": "French"},
         {"value": "de", "label": "German"},
         {"value": "es", "label": "Spanish"},
         {"value": "it", "label": "Italian"},
     ]},
    {"key": "limit", "label": "Results", "type": "select", "default": "10",
     "options": [
         {"value": "5", "label": "5"},
         {"value": "10", "label": "10"},
         {"value": "20", "label": "20"},
         {"value": "40", "label": "40"},
     ]},
    {"key": "include_non_pdf", "label": "Include non-PDF (HTML preview)", "type": "select",
     "default": "no",
     "options": [
         {"value": "no", "label": "No — free PDFs only"},
         {"value": "yes", "label": "Yes — previews too"},
     ]},
]


def _get_json(url: str, timeout: int = 30) -> dict:
    """GET a URL and parse JSON, bounded to ~`timeout` seconds (chunked read
    against a hard deadline, like the other plugins). A 429 gets a clear
    message: keyless calls share one global daily quota that is often
    exhausted, and the fix is to add a personal API key."""
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
        if e.code == 429:
            raise RuntimeError(
                "Google Books quota exceeded — keyless searches share one "
                "global daily quota. Add your own free API key in the plugin "
                "settings, or try again tomorrow") from e
        raise RuntimeError(f"Google Books returned HTTP {e.code}") from e
    except Exception as e:
        msg = str(e).lower()
        if isinstance(e, TimeoutError) or "timed out" in msg or "timeout" in msg:
            raise RuntimeError(
                "Google Books is not responding (timed out); try again") from e
        raise


def _book_name(v: dict) -> str:
    title = (v.get("title") or "").strip()
    authors = ", ".join(a for a in (v.get("authors") or []) if a)
    return f"{title} — {authors}" if authors else title


def _book_meta(v: dict, kind: str) -> str:
    parts = []
    year = (v.get("publishedDate") or "")[:4]
    if year:
        parts.append(year)
    pages = v.get("pageCount")
    if isinstance(pages, int) and pages > 0:
        parts.append(f"{pages} p")
    if kind != "pdf":
        parts.append("preview")
    return " · ".join(parts)


def _cover(v: dict) -> str:
    # the book's cover thumbnail (front cover), if the API provides one
    imgs = v.get("imageLinks") or {}
    return imgs.get("thumbnail") or imgs.get("smallThumbnail") or ""


def _item_from_volume(it: dict, include_non_pdf: bool) -> dict | None:
    """One API volume -> a result item, or None if it has nothing openable."""
    v = it.get("volumeInfo") or {}
    name = _book_name(v)
    if not name:
        return None
    cover = _cover(v)
    sale = it.get("saleInfo") or {}
    pdf = (sale.get("pdf") or {})
    if pdf.get("isAvailable") and pdf.get("downloadLink"):
        return {"name": name, "url": pdf["downloadLink"], "kind": "pdf",
                "meta": _book_meta(v, "pdf"), "cover": cover}
    if include_non_pdf and sale.get("accessViewStatus") in ("FULL_PUBLIC_DOMAIN", "PARTIAL") \
            and (v.get("previewLink") or ""):
        return {"name": name, "url": v["previewLink"], "kind": "html",
                "meta": _book_meta(v, "html"), "cover": cover}
    return None


def search(query: str, config: dict) -> list[dict]:
    """Search Google Books and return openable results (free PDFs, and HTML
    previews too if enabled)."""
    q = (query or "").strip()
    if not q:
        return []
    cfg = config or {}
    params = {"q": q, "printType": "books"}
    try:
        params["maxResults"] = int(cfg.get("limit") or 10)
    except ValueError:
        params["maxResults"] = 10
    order_by, order = _SORTS.get(cfg.get("sort") or "relevance", (None, None))
    if order_by:
        params["orderBy"] = order_by
        if order:
            params["sortOrder"] = order
    lang = (cfg.get("language") or "").strip()
    if lang:
        params["langRestrict"] = lang
    key = (cfg.get("api_key") or "").strip()
    if key:
        params["key"] = key
    url = f"{API}?{urllib.parse.urlencode(params)}"
    data = _get_json(url)
    include_non_pdf = (cfg.get("include_non_pdf") or "no") == "yes"
    items = []
    for it in data.get("items", []):
        item = _item_from_volume(it, include_non_pdf)
        if item:
            items.append(item)
    return items


def covers(items: list) -> list:
    """Cover image per result, aligned by index with `items`.

    The cover (the API's imageLinks thumbnail) is already on each item from
    search(), so this just hands it back — no network call, which keeps the
    cover query instant. (The front end overwrites it.cover with this
    response, so the value must be returned here, not only in search().)
    """
    return [(it or {}).get("cover", "") for it in (items or [])]
