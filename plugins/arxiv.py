"""arXiv plugin.

A *live* provider: search(query, config) queries the official arXiv API
(export.arxiv.org) and returns matching papers. Each result links to the
paper's PDF (arxiv.org/pdf/<id>), which the reader downloads and opens.

Define search(query, config) -> list[dict]. Each item is:
  {"name": str, "url": str, "kind": "pdf", "meta": str}
where `url` is the direct PDF link.

arXiv's API is key-free and stable, but can be slow to respond (a broad
search may take 20-30s), so the request is bounded by a hard deadline.
"""
import base64
import io
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw, ImageFont

NAME = "arXiv"
DESCRIPTION = "Search arXiv for academic papers and open them as PDFs."

API = "https://export.arxiv.org/api/query"
UA = {"User-Agent": "pdf-torrent-reader/1.0"}

# Atom + arXiv extension namespaces
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

# field select value -> arXiv search field prefix
_FIELDS = {"all": "all", "ti": "ti", "au": "au", "abs": "abs"}
# sort select value -> (sortBy, sortOrder); None sortOrder = arXiv default
_SORTS = {
    "relevance": ("relevance", None),
    "submitted": ("submittedDate", "descending"),
    "updated": ("lastUpdatedDate", "descending"),
}

# Settings the UI renders and passes to search() as the `config` dict.
CONFIG = [
    {"key": "field", "label": "Search in", "type": "select", "default": "all",
     "options": [
         {"value": "all", "label": "Everything"},
         {"value": "ti", "label": "Title"},
         {"value": "au", "label": "Author"},
         {"value": "abs", "label": "Abstract"},
     ]},
    {"key": "sort", "label": "Sort by", "type": "select", "default": "relevance",
     "options": [
         {"value": "relevance", "label": "Relevance"},
         {"value": "submitted", "label": "Newest"},
         {"value": "updated", "label": "Recently updated"},
     ]},
    {"key": "limit", "label": "Results", "type": "select", "default": "10",
     "options": [
         {"value": "5", "label": "5"},
         {"value": "10", "label": "10"},
         {"value": "20", "label": "20"},
         {"value": "50", "label": "50"},
     ]},
    {"key": "category", "label": "Category", "type": "text", "default": "",
     "placeholder": "e.g. cs.CV, math.CO (optional)"},
]


def _get(url: str, timeout: int = 45) -> str:
    """GET a URL and return the body as text, bounded to ~`timeout` seconds.

    arXiv's API can be slow to start responding (a broad search may take
    20-30s), so the body is read in chunks against a hard deadline — the same
    pattern as the Gutenberg plugin — so a stuck connection can't hang the
    search. Failures get a clear message; the front end keeps the previous
    results on screen so the user can just re-run the search.
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
            return b"".join(chunks).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"arXiv returned HTTP {e.code}") from e
    except Exception as e:
        msg = str(e).lower()
        if isinstance(e, TimeoutError) or "timed out" in msg or "timeout" in msg:
            raise RuntimeError(
                "arXiv is not responding (timed out) — it can be slow under "
                "load; try again or narrow the search") from e
        raise


def _clean(s: str) -> str:
    # arXiv titles/abstracts are wrapped with newlines; collapse to one line
    return re.sub(r"\s+", " ", s or "").strip()


def _build_query(query: str, config: dict) -> str | None:
    """Build the arXiv search_query. Returns None for an empty query."""
    cfg = config or {}
    q = (query or "").strip()
    if not q:
        return None
    field = _FIELDS.get(cfg.get("field") or "all", "all")
    parts = [f'{field}:"{q}"']  # phrase-search the user's terms
    cat = (cfg.get("category") or "").strip()
    if cat:
        parts.append(f"cat:{cat}")
    return " AND ".join(parts)


def _parse(text: str) -> list[dict]:
    """Parse the Atom feed into result items (direct PDF links)."""
    root = ET.fromstring(text)
    items = []
    for entry in root.findall("a:entry", NS):
        title = _clean(entry.findtext("a:title", "", NS))
        if not title:
            continue
        pdf_url = ""
        for link in entry.findall("a:link", NS):
            if link.get("type") == "application/pdf":
                pdf_url = link.get("href", "")
        if not pdf_url:
            continue
        authors = [_clean(a.findtext("a:name", "", NS))
                   for a in entry.findall("a:author", NS)]
        authors = [a for a in authors if a]
        pc = entry.find("arxiv:primary_category", NS)
        cat = pc.get("term", "") if pc is not None else ""
        published = entry.findtext("a:published", "", NS)
        year = published[:4] if published else ""
        meta = " · ".join(x for x in [", ".join(authors[:3]), cat, year] if x)
        items.append({"name": title, "url": pdf_url, "kind": "pdf", "meta": meta})
    return items


def search(query: str, config: dict) -> list[dict]:
    """Search arXiv and return matching papers as direct-PDF items."""
    sq = _build_query(query, config)
    if sq is None:
        return []
    cfg = config or {}
    params = {"search_query": sq, "start": 0}
    try:
        params["max_results"] = int(cfg.get("limit") or 10)
    except ValueError:
        params["max_results"] = 10
    sort_by, sort_order = _SORTS.get(cfg.get("sort") or "relevance",
                                     ("relevance", None))
    params["sortBy"] = sort_by
    if sort_order:
        params["sortOrder"] = sort_order
    url = f"{API}?{urllib.parse.urlencode(params)}"
    return _parse(_get(url))


def _font(size: int) -> ImageFont.ImageFont:
    # Pillow >=10.1 accepts a size for the built-in font; fall back for older.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _arxiv_id(item: dict) -> str:
    # https://arxiv.org/pdf/2201.00978v1 -> 2201.00978v1
    url = (item or {}).get("url", "")
    return url.rsplit("/", 1)[-1] if url else ""


def _cover_data_uri(title: str, arxiv_id: str) -> str:
    """A simple paper-style cover (arXiv has no cover art)."""
    W, H = 300, 400
    im = Image.new("RGB", (W, H), "#f7f7f5")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 52], fill="#b31b1b")  # arXiv red
    d.text((16, 16), "arXiv", fill="white", font=_font(20))
    if arxiv_id:
        d.text((16, 64), arxiv_id, fill="#888888", font=_font(13))
    # wrap the title into a few lines
    words = (title or "Paper").split()
    line, lines, y = "", [], 100
    for w in words:
        if d.textlength(line + " " + w, font=_font(20)) > W - 40:
            lines.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    if line:
        lines.append(line)
    for ln in lines[:8]:
        d.text((20, y), ln, fill="#222222", font=_font(20))
        y += 28
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def covers(items: list) -> list:
    """A generated paper-style cover per result, aligned by index with `items`."""
    if not items:
        return []
    return [_cover_data_uri((it or {}).get("name", ""), _arxiv_id(it or {}))
            for it in items]
