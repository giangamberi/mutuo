"""OpenAlex plugin (scholarly search).

A *live* provider: search(query, config) queries the OpenAlex API — a free,
key-free index of 250M+ scholarly works (the closest open equivalent to
Google Scholar) — and returns matching papers. Open-access papers link to
their PDF (opened in the reader); papers whose open copy is a landing page
open in the in-site HTML viewer.

Define search(query, config) -> list[dict]. Each item is:
  {"name": str, "url": str, "kind": "pdf"|"html", "meta": str}

Why not Google Scholar directly: it has no official API — only HTML that is
aggressively bot-protected (CAPTCHAs, IP bans) and off-limits to scrape.
OpenAlex is the same idea (search the scholarly literature) via a proper API.
"""
import base64
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw, ImageFont

NAME = "OpenAlex"
DESCRIPTION = "Search the OpenAlex scholarly index (250M+ papers) and open the open-access PDFs."

API = "https://api.openalex.org/works"
# keep responses small: only the fields we render
SELECT = "id,display_name,publication_year,authorships,primary_location,open_access,doi"
UA = {"User-Agent": "pdf-torrent-reader/1.0"}

# sort select value -> OpenAlex sort param; None = API default (relevance)
_SORTS = {
    "relevance": None,
    "newest": "publication_date:desc",
    "oldest": "publication_date:asc",
}

# Settings the UI renders and passes to search() as the `config` dict.
CONFIG = [
    {"key": "sort", "label": "Sort by", "type": "select", "default": "relevance",
     "options": [
         {"value": "relevance", "label": "Relevance"},
         {"value": "newest", "label": "Newest"},
         {"value": "oldest", "label": "Oldest"},
     ]},
    {"key": "limit", "label": "Results", "type": "select", "default": "10",
     "options": [
         {"value": "5", "label": "5"},
         {"value": "10", "label": "10"},
         {"value": "20", "label": "20"},
         {"value": "50", "label": "50"},
     ]},
    {"key": "year_from", "label": "Year from", "type": "text",
     "default": "", "placeholder": "e.g. 2015"},
    {"key": "year_to", "label": "Year to", "type": "text",
     "default": "", "placeholder": "e.g. 2024"},
    {"key": "open_access", "label": "Access", "type": "select", "default": "oa",
     "options": [
         {"value": "oa", "label": "Open access only"},
         {"value": "all", "label": "All (paywalled open as page)"},
     ]},
]


def _get_json(url: str, timeout: int = 30, attempts: int = 2) -> dict:
    """GET a URL and parse JSON, bounded to ~`timeout` seconds per attempt.

    The body is read in chunks against a hard deadline (like the other
    plugins) so a stuck connection can't hang the search. OpenAlex rate-limits
    per IP (10 req/s); a single search is one request, but a shared IP can
    still 429, so one short retry rides out a transient limit.
    """
    last = None
    for i in range(attempts):
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
            last = e
            if e.code in (429, 500, 502, 503) and i < attempts - 1:
                time.sleep(2)
                continue
            if e.code == 429:
                raise RuntimeError(
                    "OpenAlex rate limit hit — wait a moment and try again") from e
            raise RuntimeError(f"OpenAlex returned HTTP {e.code}") from e
        except Exception as e:
            last = e
            msg = str(e).lower()
            if isinstance(e, TimeoutError) or "timed out" in msg or "timeout" in msg:
                if i < attempts - 1:
                    time.sleep(2)
                    continue
                raise RuntimeError(
                    "OpenAlex is not responding (timed out); try again") from e
            raise
    raise last


def _is_direct_pdf(url: str) -> bool:
    """Heuristic: does this URL point straight at a PDF file (so the reader's
    downloader will get a %PDF- header)? OpenAlex's oa_url is sometimes a
    landing page (e.g. a DOI) rather than the file, so only trust the obvious
    direct-PDF shapes; everything else opens as a page."""
    p = urllib.parse.urlparse(url)
    path = p.path.lower()
    if path.endswith(".pdf"):
        return True
    if p.netloc.endswith("arxiv.org") and path.startswith("/pdf/"):
        return True
    return False


def _item_from_work(w: dict, oa_only: bool) -> dict | None:
    """One OpenAlex work -> a result item, or None if it has nothing openable."""
    title = (w.get("display_name") or "").strip()
    if not title:
        return None
    oa = w.get("open_access") or {}
    pdf_url = oa.get("pdf_url") or ""
    oa_url = oa.get("oa_url") or ""
    if pdf_url:
        url, kind = pdf_url, "pdf"
    elif oa_url:
        url = oa_url
        kind = "pdf" if _is_direct_pdf(oa_url) else "html"
    else:
        if oa_only:
            return None  # paywalled and we only want open access
        loc = w.get("primary_location") or {}
        url = (loc.get("landing_page_url") or "") or (w.get("doi") or "")
        if not url:
            return None
        kind = "html"
    authors = [a.get("author", {}).get("display_name")
               for a in (w.get("authorships") or [])]
    authors = [a for a in authors if a]
    year = w.get("publication_year")
    loc = w.get("primary_location") or {}
    source = (loc.get("source") or {}).get("display_name") or ""
    meta = " · ".join(x for x in [", ".join(authors[:3]),
                                  str(year) if year else "", source] if x)
    item = {"name": title, "url": url, "kind": kind}
    if meta:
        item["meta"] = meta
    return item


def search(query: str, config: dict) -> list[dict]:
    """Search OpenAlex and return matching papers (open-access PDFs, and
    landing pages too if "all" access is selected)."""
    q = (query or "").strip()
    if not q:
        return []
    cfg = config or {}
    params = {"search": q, "select": SELECT}
    try:
        params["per-page"] = int(cfg.get("limit") or 10)
    except ValueError:
        params["per-page"] = 10
    sort = _SORTS.get(cfg.get("sort") or "relevance")
    if sort:
        params["sort"] = sort
    filters = []
    yf = (cfg.get("year_from") or "").strip()
    yt = (cfg.get("year_to") or "").strip()
    if yf:
        filters.append(f"from_publication_date:{yf}-01-01")
    if yt:
        filters.append(f"to_publication_date:{yt}-12-31")
    if filters:
        params["filter"] = ",".join(filters)
    url = f"{API}?{urllib.parse.urlencode(params)}"
    data = _get_json(url)
    oa_only = (cfg.get("open_access") or "oa") == "oa"
    items = []
    for w in data.get("results", []):
        item = _item_from_work(w, oa_only)
        if item:
            items.append(item)
    return items


def _font(size: int) -> ImageFont.ImageFont:
    # Pillow >=10.1 accepts a size for the built-in font; fall back for older.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _cover_data_uri(title: str, meta: str) -> str:
    """A simple paper-style cover (OpenAlex has no cover art)."""
    W, H = 300, 400
    im = Image.new("RGB", (W, H), "#1f2430")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 48], fill="#3b6ea5")
    d.text((16, 14), "OpenAlex", fill="white", font=_font(18))
    words = (title or "Paper").split()
    line, lines, y = "", [], 90
    for w in words:
        if d.textlength(line + " " + w, font=_font(20)) > W - 40:
            lines.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    if line:
        lines.append(line)
    for ln in lines[:7]:
        d.text((20, y), ln, fill="white", font=_font(20))
        y += 28
    if meta:
        d.text((20, H - 40), meta[:44], fill="#aab4c4", font=_font(12))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def covers(items: list) -> list:
    """A generated paper-style cover per result, aligned by index with `items`."""
    if not items:
        return []
    return [_cover_data_uri((it or {}).get("name", ""), (it or {}).get("meta", ""))
            for it in items]
