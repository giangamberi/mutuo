"""Digital Comic Museum plugin.

A *live* provider: search(query, config) searches The Digital Comic Museum
(digitalcomicmuseum.com) â€” an archive of public-domain Golden Age comic books â€”
and returns each matching comic's first issue as a PDF to open in the reader.

Define search(query, config) -> list[dict]. Each item is one of:
  {"name": str, "url": str}      a PDF to open (downloaded on click)
  {"name": str, "magnet": str}   a torrent to track
  {"name": str, "path": str}     a local PDF to read in place
An item may also carry a "meta" string shown as a subtitle line.

NOTE: digitalcomicmuseum.com sits behind Cloudflare bot protection. From a
network where Cloudflare lets plain HTTP requests through, this works; from a
network where it doesn't, every request returns 403 and the search surfaces a
clear error. The HTML parsing below is best-effort (regex, no external deps)
and may need a tweak if the site's markup changes.
"""
import html
import re
import time
import urllib.error
import urllib.parse
import urllib.request

NAME = "Digital Comic Museum"
DESCRIPTION = "Search The Digital Comic Museum for public-domain Golden Age comic books (first issue of each title)."

BASE = "https://digitalcomicmuseum.com"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# how many search results to resolve into openable PDFs (each needs one page
# fetch, so keep this modest to keep the search snappy)
ROWS = 8


def _get(url, attempts=3, timeout=20):
    """GET a URL and return its text, retrying on transient failures.

    Raises a clear error if the site answers 403 (Cloudflare bot protection)
    so the UI can show something useful instead of a generic failure.
    """
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 403:
                raise RuntimeError(
                    "digitalcomicmuseum.com is blocking this network "
                    "(Cloudflare 403). Try from a different connection.")
            last = e
        except Exception as e:
            last = e
        if i < attempts - 1:
            time.sleep(2 * (i + 1))
    raise last


# Settings the UI renders and passes to search().
CONFIG = [
    {"key": "limit", "label": "Max results", "type": "select",
     "default": "8",
     "options": [
         {"value": "4", "label": "4"},
         {"value": "8", "label": "8"},
         {"value": "12", "label": "12"},
     ]},
]


def _abs(href: str) -> str:
    """Make a (possibly relative) href absolute against the DCM base."""
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return BASE + href
    return BASE + "/" + href


def _strip_tags(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _search_titles(query: str, limit: int) -> list[dict]:
    """Run the site's search and return [{name, url}] for each comic title."""
    url = f"{BASE}/?s={urllib.parse.quote_plus(query)}"
    page = _get(url)
    # comic title links live under /comics/<slug>/
    seen = {}
    for m in re.finditer(r'href="([^"]*?/comics/[a-z0-9-]+/)"[^>]*>(.*?)</a>',
                         page, re.I | re.S):
        href, label = m.group(1), _strip_tags(m.group(2))
        if not label or label.lower() in ("comics", "home"):
            continue
        key = href.split("?")[0].rstrip("/")
        if key not in seen:
            seen[key] = {"name": label, "url": _abs(href)}
        if len(seen) >= limit:
            break
    return list(seen.values())


def _first_pdf(comic_url: str):
    """Fetch a comic title page and return the first issue's PDF URL, or None.

    DCM lists a title's issues as links; the issue page (or a direct link)
    points at a .pdf. We take the first PDF we can find on the page."""
    page = _get(comic_url)
    # direct PDF links first
    for m in re.finditer(r'href="([^"]+\.pdf)"', page, re.I):
        return _abs(m.group(1))
    # otherwise the first issue link (issue pages host the PDF)
    for m in re.finditer(r'href="([^"]*?/comics/[a-z0-9-]+/[a-z0-9-]+/)"', page, re.I):
        return _abs(m.group(1))
    return None


def search(query: str, config: dict) -> list[dict]:
    """Search the Digital Comic Museum and return each matching comic's first
    issue as an openable PDF."""
    q = (query or "").strip()
    if not q:
        return []
    limit = int((config or {}).get("limit") or ROWS)
    titles = _search_titles(q, limit)
    items = []
    for t in titles:
        try:
            pdf = _first_pdf(t["url"])
        except Exception:
            continue  # can't resolve a PDF for this title; skip it
        if not pdf:
            continue
        items.append({"name": t["name"], "url": pdf, "kind": "pdf",
                      "meta": "first issue"})
    return items

