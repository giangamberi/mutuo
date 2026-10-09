"""PDF torrent reader — FastAPI app.

Architecture notes
- One long-running libtorrent session in a worker thread (torrent_engine.py).
- Range requests are the contract with PDF.js: the browser fetches
  bytes=0-65535 first, then the final bytes for the xref, then whole
  pages. So the server only *verifies* the pieces covering each range
  before responding — no full-file download required for first render.
- Single user, no auth, binds to 127.0.0.1 by default.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from catalog import Catalog
from torrent_engine import TorrentEngine, TorrentError

BASE = Path(__file__).parent
app = FastAPI(title="pdf-torrent-reader")

engine = TorrentEngine(download_dir=str(BASE / "downloads"))
catalog = Catalog(str(BASE / "catalog.json"))
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


class AddRequest(BaseModel):
    name: str
    magnet: str
    files: list[str] | None = None


@app.on_event("startup")
def _startup():
    engine.start()
    feed = BASE / "feed.json"
    catalog.sync(str(feed) if feed.exists() else None)
    # enqueue torrents that are catalogued but not yet in the session
    for item in catalog.items():
        engine.add_torrent_async(item.magnet)


@app.get("/")
def index() -> HTMLResponse:
    return HTMLResponse((BASE / "static" / "index.html").read_text())


@app.get("/api/catalog")
def get_catalog() -> JSONResponse:
    return JSONResponse([
        {"name": it.name, "magnet": it.magnet, "files": it.files}
        for it in catalog.items()
    ])


@app.post("/api/catalog")
def post_catalog(req: AddRequest) -> JSONResponse:
    if not req.magnet.startswith("magnet:") and not req.magnet.startswith(("http://", "https://")):
        raise HTTPException(400, "magnet or .torrent URL required")
    if catalog.has(req.magnet):
        raise HTTPException(409, "already in catalog")
    catalog.add(req.name, req.magnet, req.files)
    catalog.save()
    engine.add_torrent_async(req.magnet)
    return JSONResponse({"ok": True})


@app.get("/api/torrents")
def list_torrents() -> JSONResponse:
    return JSONResponse(engine.status(include_files=True))


@app.get("/api/file")
def file_meta(ih: str, f: int) -> JSONResponse:
    """Resolve (info_hash, file_index) -> name/size so the UI can offer readers."""
    for t in engine.status(include_files=True):
        if t["info_hash"] == ih:
            for entry in t["files"]:
                if entry["index"] == f:
                    return JSONResponse({"name": entry["name"], "size": entry["size"]})
    raise HTTPException(404, "file not found")


@app.get("/api/pdf")
def stream_pdf(ih: str, f: int, request: Request) -> Response:
    """Serve file `f` of torrent `ih` with full HTTP range support."""
    try:
        size = engine.file_size(ih, f)
    except (TorrentError, KeyError):
        raise HTTPException(404, "torrent/file not found or metadata not ready")

    rng = request.headers.get("range", "")
    m = re.match(r"bytes=(\d+)-(\d*)", rng)
    if m:
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
        end = min(end, size - 1)
        if start >= size or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        length = end - start + 1
        status_code = 206
        headers = {
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
            "Accept-Ranges": "bytes",
        }
    else:
        start, length, status_code = 0, size, 200
        headers = {"Content-Length": str(length), "Accept-Ranges": "bytes"}

    try:
        engine.wait_for_range(ih, f, start, length, timeout=600)
        data = engine.fetch_range(ih, f, start, length)
    except TimeoutError:
        raise HTTPException(503, "pieces still not available from swarm")
    except KeyError:
        raise HTTPException(404, "torrent left session")
    return Response(data, status_code=status_code, media_type="application/pdf", headers=headers)


@app.post("/api/sync")
def sync_feed() -> JSONResponse:
    """Re-pull feed.json and enqueue any new torrents."""
    feed = BASE / "feed.json"
    added = catalog.sync(str(feed)) if feed.exists() else 0
    known = {t["info_hash"] for t in engine.status(include_files=False)}
    for item in catalog.items():
        if not any(k == _ih_from_magnet(item.magnet) for k in known):
            engine.add_torrent_async(item.magnet)
    return JSONResponse({"added": added})


def _ih_from_magnet(magnet: str) -> str | None:
    for part in magnet.split("&"):
        if part.startswith("xt=urn:btih:"):
            return part[len("xt=urn:btih:"):]
    return None
