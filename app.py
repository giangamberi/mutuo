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

import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote

import libtorrent as lt
from contextlib import asynccontextmanager
from typing import List
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from catalog import Catalog
from torrent_engine import TorrentEngine, TorrentError, _ih_hex

BASE = Path(__file__).parent
UPLOAD_DIR = BASE / ".torrents"
@asynccontextmanager
async def _lifespan(app: FastAPI):
    # startup
    engine.start()
    feed = BASE / "feed.json"
    catalog.sync(str(feed) if feed.exists() else None)
    # enqueue torrents that are catalogued but not yet in the session
    for item in catalog.items():
        engine.add_torrent_async(item.magnet)
    try:
        yield
    finally:
        # shutdown
        engine.stop()


app = FastAPI(title="pdf-torrent-reader", lifespan=_lifespan)

engine = TorrentEngine(download_dir=str(BASE / "downloads"))
catalog = Catalog(str(BASE / "catalog.json"))
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


class AddRequest(BaseModel):
    name: str
    magnet: str
    files: list[str] | None = None


@app.get("/")
def index() -> HTMLResponse:
    resp = HTMLResponse((BASE / "static" / "index.html").read_text(encoding="utf-8"))
    # The UI changes frequently and is a single file with no cache-busting version
    # in its name; never let the browser serve a stale copy.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


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


@app.post("/api/torrent")
async def upload_torrent(files: List[UploadFile] = File(...)) -> JSONResponse:
    """Accept one or more dropped .torrent files, register each in the
    catalog (as a magnet), and add each to the session. Because the files
    already carry full metadata, PDF lists appear immediately — no DHT wait."""
    if not files:
        raise HTTPException(400, "no files provided")
    added, skipped, errors = [], [], []
    for uf in files:
        data = await uf.read()
        fname = _as_str(uf.filename) if uf.filename else "unknown"
        if not data:
            errors.append({"file": fname, "error": "empty file"})
            continue
        tmp = tempfile.NamedTemporaryFile(suffix=".torrent", delete=False)
        try:
            tmp.write(data)
            tmp.close()
            try:
                ti = lt.torrent_info(tmp.name)
            except Exception as exc:
                errors.append({"file": fname, "error": f"not a valid .torrent: {exc}"})
                continue
            name = _as_str(ti.name())
            ih_hex = _ih_hex(ti.info_hash)
            magnet = _build_magnet(ti)
            UPLOAD_DIR.mkdir(exist_ok=True)
            dest = str(UPLOAD_DIR / f"{ih_hex}.torrent")
            shutil.copyfile(tmp.name, dest)
            already = catalog.has(magnet)
            if not already:
                catalog.add(name, magnet, [])
                catalog.save()
            engine.add_torrent(dest, magnet=magnet)
            entry = {"name": name, "info_hash": ih_hex}
            (skipped if already else added).append(entry)
        finally:
            os.unlink(tmp.name)
    if errors and not added and not skipped:
        raise HTTPException(400, "; ".join(f"{e['file']}: {e['error']}" for e in errors))
    return JSONResponse({"ok": True, "added": added, "skipped": skipped, "errors": errors})


@app.get("/api/torrents")
def list_torrents() -> JSONResponse:
    return JSONResponse(engine.status(include_files=True))


@app.get("/api/torrent")
def get_torrent(ih: str) -> JSONResponse:
    """Detail snapshot of one torrent (stats + file list) for the loading page."""
    entry = engine.get(ih)
    if entry is None:
        raise HTTPException(404, "torrent not in session")
    return JSONResponse(entry)


@app.post("/api/torrent/{ih}/{action}")
def torrent_action(ih: str, action: str) -> JSONResponse:
    """Per-torrent actions: pause, resume, recheck."""
    if engine.get(ih, include_files=False) is None:
        raise HTTPException(404, "torrent not in session")
    if action == "pause":
        engine.pause(ih)
    elif action == "resume":
        engine.resume(ih)
    elif action == "recheck":
        engine.force_recheck(ih)
    else:
        raise HTTPException(400, f"unknown action: {action}")
    return JSONResponse({"ok": True})


class RemoveRequest(BaseModel):
    delete_files: bool = False


@app.delete("/api/torrent/{ih}")
def remove_torrent(ih: str, req: RemoveRequest | None = None) -> JSONResponse:
    """Remove a torrent from the session (optionally deleting downloaded data)."""
    if engine.get(ih, include_files=False) is None:
        raise HTTPException(404, "torrent not in session")
    delete = bool(req.delete_files) if req else False
    engine.remove_torrent(ih, delete_files=delete)
    return JSONResponse({"ok": True})


class FilePriorityRequest(BaseModel):
    index: int
    priority: int  # 0=exclude, 4=normal, 7=max


@app.post("/api/torrent/{ih}/file-priority")
def file_priority(ih: str, req: FilePriorityRequest) -> JSONResponse:
    """Set download priority for one file (0 to exclude it from the download)."""
    if engine.get(ih, include_files=False) is None:
        raise HTTPException(404, "torrent not in session")
    engine.set_file_priority(ih, req.index, req.priority)
    return JSONResponse({"ok": True})


@app.delete("/api/torrent/{ih}/file/{index}")
def delete_file(ih: str, index: int) -> JSONResponse:
    """Delete one file from disk and exclude it, keeping the torrent in the session."""
    if engine.get(ih, include_files=False) is None:
        raise HTTPException(404, "torrent not in session")
    try:
        engine.delete_file(ih, index)
    except TorrentError as exc:
        raise HTTPException(404, str(exc))
    return JSONResponse({"ok": True})


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

    # Opt-in downloads: opening a PDF is the user's choice to fetch it. Make
    # sure the file is actually included in the download, otherwise its pieces
    # are never requested and the read below would block until timeout.
    try:
        engine.set_file_priority(ih, f, 4)
    except (KeyError, TorrentError):
        pass
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


def _as_str(x) -> str:
    """libtorrent returns name()/trackers() as bytes on some builds; coerce."""
    if isinstance(x, bytes):
        return x.decode("utf-8", "replace")
    return str(x)


def _build_magnet(ti) -> str:
    """Build a magnet URI from parsed metadata so a dropped .torrent can be
    persisted in the catalog (the file itself can't) and re-added after a
    restart. Trackers from the file are included to help reconnect."""
    parts = [f"magnet:?xt=urn:btih:{_ih_hex(ti.info_hash)}", f"dn={quote(_as_str(ti.name()))}"]
    try:
        raw = list(ti.trackers())  # deprecated; returns announce_entry objects
    except Exception:
        raw = []
    for entry in raw:
        # entries are announce_entry objects with a .url (str); guard both shapes
        url = getattr(entry, "url", None)
        url = _as_str(url if url is not None else entry)
        if url:
            parts.append(f"tr={quote(url)}")
    return "&".join(parts)
