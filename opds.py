"""OPDS catalog support.

An OPDS (Open Publication Distribution System) feed is an Atom 1.0 (or
OPDS 2.0 JSON) document that lists books: navigation links point at
sub-feeds, acquisition links point at the files themselves (EPUB, PDF,
text, …). Browsing one turns any OPDS server — Standard Ebooks, Project
Gutenberg, a library's catalog, a personal shelf — into a source the app
can read from.

This module only *parses* feeds into a plain dict; fetching, saving and
opening the files is done by the app's existing per-format flows.
"""
from __future__ import annotations

import json
import posixpath
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = {"User-Agent": "mutuo/1.0 (+opds)"}
ATOM = "{http://www.w3.org/2005/Atom}"
OPDS = "{http://opds-spec.org/2010/catalog}"


def _text(el, tag: str) -> str:
    """First child's text, '' if absent. Falls back to itertext() for
    XHTML-wrapped titles (e.g. <title type="text/xhtml"><div>..</div></title>)."""
    c = el.find(tag)
    if c is None:
        return ""
    t = (c.text or "").strip()
    if not t:
        t = "".join(c.itertext()).strip()
    return t


def _resolve(base: str, url: str) -> str:
    """Resolve a (possibly relative) feed URL against the feed's own URL."""
    return urllib.parse.urljoin(base, url.strip())


NAV_RELS = {"section", "http://opds-spec.org/section"}
ACQ_RELS = {"http://opds-spec.org/acquisition", "acquisition"}


def _is_feed_type(media_type: str) -> bool:
    return media_type.endswith(("atom+xml", "json"))


def _is_image_type(media_type: str) -> bool:
    # covers/thumbnails are not readable files — never an acquisition
    return media_type.startswith("image/")


def _classify(links):
    """Split an entry's links into (navigation, acquisition).

    Navigation entries point at a sub-feed (rel="section"); acquisition
    entries point at the file itself. Non-standard feeds often put the file
    link on rel="alternate" with a document media type, so fall back to the
    first link whose type is a document (not a feed). Some feeds (e.g.
    ManyBooks) mark navigation entries with rel="alternate" + a feed media
    type instead of rel="section", so a feed-typed link is navigation too."""
    nav = next((l for l in links if l["rel"] in NAV_RELS), None)
    if nav is None:
        nav = next((l for l in links
                    if l["type"] and _is_feed_type(l["type"])), None)
    acq = next((l for l in links if l["rel"] in ACQ_RELS), None)
    if not acq:
        acq = next((l for l in links
                    if l is not nav and l["type"]
                    and not _is_feed_type(l["type"])
                    and not _is_image_type(l["type"])), None)
    return nav, acq


def _kind(media_type: str, url: str) -> str:
    """Map an OPDS media type (or file extension) to the app's reader kinds."""
    mt = (media_type or "").lower()
    if "epub" in mt:
        return "epub"
    if "pdf" in mt:
        return "pdf"
    if "html" in mt or "xml" in mt:
        return "html"
    if "plain" in mt or "text" in mt:
        return "text"
    ext = posixpath.splitext(urllib.parse.urlparse(url).path)[1].lower()
    return {".epub": "epub", ".pdf": "pdf", ".txt": "text",
            ".html": "html", ".htm": "html"}.get(ext, "file")


def _parse_atom(root, base: str) -> dict:
    title = _text(root, ATOM + "title")
    entries = []
    for e in root.findall(ATOM + "entry"):
        links = []
        for l in e.findall(ATOM + "link"):
            href = l.get("href", "")
            if not href:
                continue
            rel = l.get("rel", "alternate")
            links.append({
                "rel": rel,
                "title": l.get("title", ""),
                "type": l.get("type", ""),
                "href": _resolve(base, href),
            })
        nav, acq = _classify(links)
        # cover: an <opds:image> element, else a thumbnail link (ManyBooks)
        cover = ""
        img = e.find(OPDS + "image")
        if img is not None:
            cover = _resolve(base, img.get("href", ""))
        else:
            thumb = next((l for l in links
                          if l["rel"] == "http://opds-spec.org/thumbnail"
                          or l["type"].startswith("image/")), None)
            if thumb:
                cover = thumb["href"]
        if nav and not acq:
            # a navigation entry: a link into a sub-feed (may carry a cover,
            # e.g. ManyBooks lists each book as a link to its detail page)
            entries.append({
                "title": _text(e, ATOM + "title") or nav.get("title") or "Section",
                "author": _text(e, ATOM + "author/" + ATOM + "name"),
                "summary": _text(e, ATOM + "summary") or _text(e, ATOM + "content"),
                "url": nav["href"], "nav": True, "cover": cover,
            })
            continue
        if not acq:
            continue
        author = _text(e, ATOM + "author/" + ATOM + "name")
        summary = _text(e, ATOM + "summary")
        entries.append({
            "title": _text(e, ATOM + "title") or acq.get("title") or "Untitled",
            "author": author,
            "summary": summary,
            "cover": cover,
            "url": acq["href"],
            "kind": _kind(acq["type"], acq["href"]),
            "media_type": acq["type"],
            "links": links,
            "nav": False,
        })
    return {"title": title, "entries": entries}


def _parse_json(data, base: str) -> dict:
    entries = []
    for e in data.get("entries", []):
        links = []
        for l in e.get("links", []):
            href = l.get("url", "")
            if not href:
                continue
            links.append({
                "rel": l.get("rel", "alternate"),
                "title": l.get("title", ""),
                "type": l.get("type", ""),
                "href": _resolve(base, href),
            })
        nav, acq = _classify(links)
        if nav and not acq:
            entries.append({"title": e.get("title", "") or nav.get("title") or "Section",
                            "url": nav["href"], "nav": True})
            continue
        if not acq:
            continue
        entries.append({
            "title": e.get("title", "") or acq.get("title") or "Untitled",
            "author": e.get("author", ""),
            "summary": e.get("summary", ""),
            "cover": e.get("image", ""),
            "url": acq["href"],
            "kind": _kind(acq["type"], acq["href"]),
            "media_type": acq["type"],
            "links": links,
            "nav": False,
        })
    return {"title": data.get("metadata", {}).get("title", ""), "entries": entries}


def parse_feed(data: bytes, base_url: str) -> dict:
    """Parse an OPDS feed (Atom XML or OPDS 2.0 JSON) into
    {title, entries, next}. Each entry: {title, author, summary, cover,
    url, kind, media_type, links}. ``next`` is the rel="next" URL for
    paginated feeds ('' when there is none)."""
    data = data.lstrip(b"\xef\xbb\xbf")  # some feeds ship a UTF-8 BOM
    if data[:1] == b"{":
        doc = json.loads(data)
        parsed = _parse_json(doc, base_url)
        nxt = ""
        search = ""
        for l in doc.get("metadata", {}).get("links", []):
            if l.get("rel") == "next" and l.get("url"):
                nxt = _resolve(base_url, l.get("url"))
            elif l.get("rel") == "search" and l.get("url"):
                search = _resolve(base_url, l.get("url"))
        parsed["next"] = nxt
        parsed["search"] = search
        return parsed
    root = ET.fromstring(data)
    parsed = _parse_atom(root, base_url)
    parsed["next"] = ""
    parsed["search"] = ""
    for l in root.findall(ATOM + "link"):
        if l.get("rel") == "next" and l.get("href"):
            parsed["next"] = _resolve(base_url, l.get("href"))
        elif l.get("rel") == "search" and l.get("href"):
            # a rel="search" link carries a {searchTerms} placeholder the UI
            # fills in — this is what makes a real catalog search possible
            parsed["search"] = _resolve(base_url, l.get("href"))
    return parsed


def fetch_feed(url: str, timeout: int = 30) -> dict:
    """Fetch and parse an OPDS feed. Raises ValueError with a readable
    message on any failure (bad URL, HTTP error, unparseable body)."""
    if not re.match(r"^https?://", url):
        raise ValueError("paste an http(s) OPDS feed URL")
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            final = resp.geturl()
    except urllib.error.HTTPError as exc:
        raise ValueError(f"feed returned HTTP {exc.code}") from exc
    except Exception as exc:
        raise ValueError(f"could not fetch feed: {exc}") from exc
    try:
        return parse_feed(data, final)
    except Exception as exc:
        raise ValueError(f"not a readable OPDS feed: {exc}") from exc
