"""Internet Archive plugin.

A *live* provider, loaded in two stages so the result list appears fast:

  1. search(query, config) — one advancedsearch API call. Returns the matching
     items immediately, each tagged with a "resolve_id" (its archive.org
     identifier) and no URL yet.
  2. resolve(ids) — the app calls this with the pending identifiers and it
     fetches each item's metadata *in parallel* (a thread pool), picking the
     best non-DRM PDF. The app merges the returned URLs back into the list.

Good for public-domain comics, books, and other scanned documents.

search() items are {"name", "kind": "pdf", "resolve_id", "meta"?}.
resolve() returns {identifier: {"url", "meta"?} | None} — None means the item
has no openable PDF and should be dropped from the list.

CONFIG exposes settings the UI renders and passes back as the `config` dict.
"""
import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

NAME = "Internet Archive"
DESCRIPTION = "Search the Internet Archive for PDFs (public-domain comics, books, and other scanned documents)."

SEARCH_URL = "https://archive.org/advancedsearch.php"
META_URL = "https://archive.org/metadata"
DL_URL = "https://archive.org/download"
UA = {"User-Agent": "pdf-torrent-reader/1.0"}

# how many search results to list. Each needs one metadata call to find its
# PDF, but those run in parallel in resolve(), so the list stays snappy.
ROWS = 10


def _get_json(url, attempts=5, timeout=15):
    """GET a URL and parse JSON, retrying on transient failures.

    archive.org occasionally hangs or 502s; a few retries with a short backoff
    ride out those windows instead of surfacing an error in the UI. A healthy
    response comes back in well under a second, so the per-attempt timeout only
    costs anything during a bad window.
    """
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(2 * (i + 1))
    raise last


# Settings the UI renders and passes to search(). These map onto the
# advancedsearch query (sort, year range, language).
CONFIG = [
    {"key": "sort", "label": "Sort by", "type": "select",
     "default": "relevance",
     "options": [
         {"value": "relevance", "label": "Relevance"},
         {"value": "year-asc", "label": "Year (oldest first)"},
         {"value": "year-desc", "label": "Year (newest first)"},
         {"value": "downloads-desc", "label": "Most downloaded"},
     ]},
    {"key": "year_from", "label": "Year from", "type": "text",
     "default": "", "placeholder": "e.g. 1900"},
    {"key": "year_to", "label": "Year to", "type": "text",
     "default": "", "placeholder": "e.g. 1927 (pre-1928 = public domain)"},
    {"key": "language", "label": "Language", "type": "select",
     "default": "",
     "options": [
         {"value": "", "label": "Any"},
         {"value": "en", "label": "English"},
         {"value": "fr", "label": "French"},
         {"value": "de", "label": "German"},
         {"value": "es", "label": "Spanish"},
     ]},
]


def _item_name(doc: dict) -> str:
    title = (doc.get("title") or "").strip()
    creator = doc.get("creator")
    if isinstance(creator, list):
        creator = ", ".join(str(c) for c in creator if c)
    creator = (creator or "").strip()
    return f"{title} — {creator}" if creator else title


def _item_meta(doc: dict, pdf: dict | None = None) -> str:
    """Subtitle line. The year comes from the search doc (available up front);
    the size only exists once the PDF is resolved, so it's added then."""
    parts = []
    year = doc.get("year")
    if year:
        parts.append(str(year))
    if pdf:
        size = pdf.get("size")
        if isinstance(size, (int, float)) and size > 0:
            parts.append(f"{size / (1024 * 1024):.0f} MB")
    return " · ".join(parts)


def _is_encrypted(fmt: str) -> bool:
    # DRM-protected variants can't be opened in the reader
    return any(x in fmt for x in ("Encrypted", "LCP", "ACS"))


def _best_pdf(files: list, identifier: str):
    """Pick the item's openable PDF: prefer the main <identifier>.pdf, then the
    standard 'Text PDF', then any non-encrypted PDF. Returns None if the item
    has no PDF (e.g. borrow-only lending items)."""
    pdfs = [f for f in files if (f.get("name") or "").lower().endswith(".pdf")]
    if not pdfs:
        return None
    open_pdfs = [f for f in pdfs if not _is_encrypted(f.get("format") or "")] or pdfs
    for f in open_pdfs:
        if f.get("name") == f"{identifier}.pdf":
            return f
    for f in open_pdfs:
        if f.get("format") == "Text PDF":
            return f
    return open_pdfs[0]


def search(query: str, config: dict) -> list[dict]:
    """Search the Internet Archive for text items with a PDF and return each
    as an openable PDF (its best non-DRM edition)."""
    cfg = config or {}
    parts = []
    q = (query or "").strip()
    if q:
        parts.append(q)
    parts.append("mediatype:(texts)")
    parts.append("format:(PDF)")
    lang = (cfg.get("language") or "").strip()
    if lang:
        parts.append(f"language:({lang})")
    yf = (cfg.get("year_from") or "").strip()
    yt = (cfg.get("year_to") or "").strip()
    if yf or yt:
        parts.append(f"year:[{yf or '*'} TO {yt or '*'}]")
    qstr = " AND ".join(parts)

    params = {
        "q": qstr,
        "fl[]": ["identifier", "title", "creator", "year", "downloads"],
        "rows": str(ROWS),
        "page": "1",
        "output": "json",
    }
    sort = cfg.get("sort") or "relevance"
    if sort == "year-asc":
        params["sort[]"] = "year+asc"
    elif sort == "year-desc":
        params["sort[]"] = "year+desc"
    elif sort == "downloads-desc":
        params["sort[]"] = "downloads+desc"
    # "relevance" is the default (no sort param)

    data = _get_json(f"{SEARCH_URL}?{urllib.parse.urlencode(params, doseq=True)}")
    docs = (data.get("response") or {}).get("docs") or []

    # Stage 1: return the list immediately. No metadata calls here — each item
    # carries its identifier as "resolve_id" so the app can resolve the PDFs
    # in parallel afterwards (see resolve()).
    items = []
    for doc in docs:
        ident = doc.get("identifier")
        if not ident:
            continue
        name = _item_name(doc)
        if not name:
            continue
        item = {"name": name, "kind": "pdf", "resolve_id": ident}
        m = _item_meta(doc)
        if m:
            item["meta"] = m
        items.append(item)
    return items


def resolve(ids: list) -> dict:
    """Stage 2: resolve a batch of identifiers to their openable PDFs.

    Each metadata call is independent, so they run in a thread pool (archive.org
    rate-limits per-IP, but a handful of parallel requests is well within it and
    turns ~10 sequential calls into one round-trip). Returns
    {identifier: {"url", "meta"?} | None} — None means no openable PDF.
    """
    def _one(ident: str):
        try:
            meta = _get_json(f"{META_URL}/{ident}", attempts=3)
        except Exception:
            return ident, None  # transient failure / no metadata
        pdf = _best_pdf(meta.get("files") or [], ident)
        if not pdf:
            return ident, None
        return ident, {"url": f"{DL_URL}/{ident}/{urllib.parse.quote(pdf['name'])}",
                       "size": pdf.get("size")}

    out = {}
    ids = [i for i in (ids or []) if i]
    if not ids:
        return out
    with ThreadPoolExecutor(max_workers=min(4, len(ids))) as ex:
        for ident, res in ex.map(_one, ids):
            out[ident] = res
    return out
