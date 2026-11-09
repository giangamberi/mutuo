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

import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import quote

import libtorrent as lt
import anyio
from contextlib import asynccontextmanager
from typing import List
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import plugin_loader
import profiles
from catalog import Catalog, ProviderCatalog
from torrent_engine import TorrentEngine, TorrentError, _ih_hex

BASE = Path(__file__).parent
# All per-profile data lives under profiles/<name>/ (set by the desktop app
# via the PTR_PROFILE env var). Without it, defaults to "default".
PROFILE_NAME = os.environ.get("PTR_PROFILE", "default")
PROFILE_DIR = profiles.profile_dir(PROFILE_NAME)
PROFILE_DIR.mkdir(parents=True, exist_ok=True)
# One-time: fold legacy root-level data into the default profile. Must run
# BEFORE the singletons below load their files, or a first run would read an
# empty catalog and only then move the data in.
profiles.migrate_root_data()

UPLOAD_DIR = PROFILE_DIR / ".torrents"
PDF_DIR = PROFILE_DIR / ".pdfs"          # direct .pdf files dropped into the reader
PDFS_INDEX = PROFILE_DIR / "pdfs.json"   # registry of direct PDFs (id -> name/size/provider/cover)
TEXT_DIR = PROFILE_DIR / ".texts"        # plain-text documents saved locally for offline reading
TEXTS_INDEX = PROFILE_DIR / "texts.json"  # registry of saved texts (id -> name/size/url/provider)
EPUB_DIR = PROFILE_DIR / ".epubs"        # EPUB documents saved locally for offline reading
EPUBS_INDEX = PROFILE_DIR / "epubs.json"  # registry of saved EPUBs (id -> name/size/url/provider)
FILES_DIR = PROFILE_DIR / ".files"        # other saved documents (html/mobi/file) for offline reading
FILES_INDEX = PROFILE_DIR / "files.json"  # registry of saved files (id -> name/size/url/provider/kind)
FAVORITES_INDEX = PROFILE_DIR / "favorites.json"  # user's favorite documents
SHELF_INDEX = PROFILE_DIR / "shelf.json"  # user's personal shelf (superset of favorites)
RECENT_INDEX = PROFILE_DIR / "recent.json"  # recently opened books (most recent first)
@asynccontextmanager
async def _lifespan(app: FastAPI):
    # startup
    engine.start()
    feed = PROFILE_DIR / "feed.json"
    catalog.sync(str(feed) if feed.exists() else None)
    # each enabled provider owns its own torrent list (a feed); merge them in
    for p in providers.items():
        if p.enabled and p.feed:
            try:
                _sync_provider_feed(p.feed, p.id)
                _sync_folder_pdfs(p.feed, p.id)
            except Exception as exc:
                print(f"providers: sync failed for {p.id} ({exc})")
    # enqueue torrents that are catalogued but not yet in the session
    for item in catalog.items():
        engine.add_torrent_async(item.magnet)
    # one-time migration: when the shelf feature is first introduced, any
    # existing favorites are added to the shelf (favorites ⊆ shelf). Skipped
    # once shelf.json exists so a user can remove a favorited book from the
    # shelf without it reappearing on the next restart.
    if not SHELF_INDEX.exists():
        shelf = _load_shelf()
        shelf_keys = {s["key"] for s in shelf}
        for f in _load_favorites():
            if f["key"] not in shelf_keys:
                shelf.append(dict(f))
                shelf_keys.add(f["key"])
        if shelf:
            _save_shelf(shelf)
    try:
        yield
    finally:
        # shutdown
        engine.stop()


app = FastAPI(title="pdf-torrent-reader", lifespan=_lifespan)

engine = TorrentEngine(download_dir=str(PROFILE_DIR / "downloads"))
catalog = Catalog(str(PROFILE_DIR / "catalog.json"))
providers = ProviderCatalog(str(PROFILE_DIR / "providers.json"))
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    # The front end (index.html, the bundled PDF.js viewer, its .ftl locale
    # files, etc.) changes often and has no version in its URL. Without this,
    # the browser heuristically caches them and serves stale copies after an
    # edit. Local single-user app, so always revalidate.
    response = await call_next(request)
    if request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ---- profile info (the desktop app uses this to show the active profile) ----


@app.get("/api/profile")
def get_profile() -> JSONResponse:
    return JSONResponse({"ok": True, "name": PROFILE_NAME})


class AddRequest(BaseModel):
    name: str
    magnet: str
    files: list[str] | None = None
    provider: str = "local"


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
        {"name": it.name, "magnet": it.magnet, "files": it.files, "provider": it.provider}
        for it in catalog.items()
    ])


@app.post("/api/catalog")
def post_catalog(req: AddRequest) -> JSONResponse:
    if not req.magnet.startswith("magnet:") and not req.magnet.startswith(("http://", "https://")):
        raise HTTPException(400, "magnet or .torrent URL required")
    if catalog.has(req.magnet):
        raise HTTPException(409, "already in catalog")
    catalog.add(req.name, req.magnet, req.files, provider=req.provider)
    catalog.save()
    engine.add_torrent_async(req.magnet)
    return JSONResponse({"ok": True})


@app.post("/api/torrent")
async def upload_torrent(files: List[UploadFile] = File(...),
                         provider: str = Form("local")) -> JSONResponse:
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
                catalog.add(name, magnet, [], provider=provider)
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
def list_torrents(files: int = 1, skip: str = "") -> JSONResponse:
    # files=0 omits the per-file list (name/size/done/priority per file) — the
    # 4s poll uses it, since only the loading page and "Manage files" need it.
    # skip=ih1,ih2 omits settled torrents the client already caches, so a
    # finished download is never re-polled.
    items = engine.status(include_files=bool(files))
    if skip:
        skipset = {s.strip() for s in skip.split(",") if s.strip()}
        items = [t for t in items if t["info_hash"] not in skipset]
    return JSONResponse(items)


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
    # also drop it from the catalog, otherwise the next feed sync (or restart)
    # re-adds the torrent and it "comes back"
    catalog.remove(ih)
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


# ---------- local cache of torrent PDFs ----------
# Opening a torrent PDF streams it from the swarm; the bytes only live in the
# torrent's download dir. Once the whole file is available we copy it into
# .pdfs/ and register it as a direct PDF (source "cache"), so it keeps working
# after the torrent is removed and needs no peers. The copy runs in a
# background thread — the viewer must not wait on it.
_cached_torrents: set[str] = set()  # "ih:f" already cached (dedupe re-opens)


def _cache_torrent_pdf(ih: str, f: int) -> None:
    """Copy a fully-downloaded torrent PDF into .pdfs/ and register it.
    Runs in a background thread; every step is best-effort."""
    tag = f"{ih}:{f}"
    # the registry is the source of truth: if a cached copy already exists
    # (survives restarts, and a deleted one re-caches on the next open) there
    # is nothing to do. The in-memory set only dedupes concurrent re-opens.
    if any(m.get("source") == "cache" and m.get("cached_from", {}).get("ih") == ih
           and m.get("cached_from", {}).get("f") == f for m in _load_pdfs().values()):
        return
    if tag in _cached_torrents:
        return
    _cached_torrents.add(tag)
    try:
        size = engine.file_size(ih, f)
        # wait for the whole file (bounded — the viewer already has the head)
        engine.wait_for_range(ih, f, 0, size, timeout=600)
        data = engine.fetch_range(ih, f, 0, size)
        if not data.startswith(b"%PDF-"):
            return  # not a PDF after all; don't cache it
        name = engine.file_name(ih, f)
        PDF_DIR.mkdir(exist_ok=True)
        pid = uuid.uuid4().hex
        (PDF_DIR / f"{pid}.pdf").write_bytes(data)
        reg = _load_pdfs()
        reg[pid] = {"name": name, "size": len(data), "provider": "local",
                    "added_at": time.time(), "source": "cache",
                    "cached_from": {"ih": ih, "f": f},
                    **_pdf_meta(str(PDF_DIR / f"{pid}.pdf"))}
        _save_pdfs(reg)
    except Exception:
        _cached_torrents.discard(tag)  # allow a later re-open to retry


@app.post("/api/pdf/cache")
def cache_pdf(ih: str, f: int) -> JSONResponse:
    """Ask the server to keep a local copy of a torrent PDF. Fire-and-forget:
    the copy happens in a background thread, so this returns immediately."""
    threading.Thread(target=_cache_torrent_pdf, args=(ih, f), daemon=True).start()
    return JSONResponse({"ok": True})


@app.post("/api/sync")
def sync_feed() -> JSONResponse:
    """Re-pull feed.json and enqueue any new torrents."""
    feed = PROFILE_DIR / "feed.json"
    added = catalog.sync(str(feed)) if feed.exists() else 0
    _enqueue_new()
    return JSONResponse({"added": added})


# ---------- providers ----------
# A provider is a named torrent list (a feed in feed.json format). The
# "local" provider is the built-in catalog (catalog.json + feed.json) and is
# not stored in providers.json; extra providers are.

LOCAL_PROVIDER = {"id": "local", "name": "Local", "feed": "", "description": "Your own torrents (catalog.json + feed.json)", "enabled": True}


def _provider_view(p) -> dict:
    return {"id": p.id, "name": p.name, "feed": p.feed,
            "description": p.description, "enabled": p.enabled}


def _enqueue_new() -> None:
    """Add catalogued torrents that are not yet in the session."""
    known = {t["info_hash"] for t in engine.status(include_files=False)}
    for item in catalog.items():
        if not any(k == _ih_from_magnet(item.magnet) for k in known):
            engine.add_torrent_async(item.magnet)


def _sync_provider_feed(feed: str, provider: str) -> dict:
    """Sync one provider's feed into the catalog. A feed is either a folder
    (every .torrent in it is added, plus a feed.json if present) or a
    feed.json file/URL (merged as before). Returns a small summary."""
    if Path(feed).is_dir():
        return _sync_folder(feed, provider)
    added = catalog.sync(feed, provider=provider)
    return {"added": added, "torrents": 0, "feed": 0, "pdfs": 0}


def _sync_folder_pdfs(folder: str, provider: str) -> int:
    """Register the .pdf files in a provider folder so they can be read
    directly from disk (no copy). Re-syncing adds new files and drops
    entries whose file is gone. Returns the number of newly added PDFs."""
    folder = Path(folder)
    if not folder.is_dir():
        return 0
    reg = _load_pdfs()
    prefix = f"folder:{provider}:"
    seen, added = set(), 0
    for f in sorted(folder.glob("*.pdf")):
        if not f.is_file():
            continue
        pid = f"{prefix}{f.name}"
        seen.add(pid)
        if pid not in reg:
            reg[pid] = {"name": f.name, "size": f.stat().st_size,
                        "provider": provider, "added_at": time.time(),
                        "source": "folder", "path": str(f), **_pdf_meta(str(f))}
            added += 1
    # drop stale entries (file renamed/deleted, or provider no longer a folder)
    for pid in [k for k in reg
                if k.startswith(prefix) and k not in seen and reg[k].get("source") == "folder"]:
        del reg[pid]
    if added:
        _save_pdfs(reg)
    return added


def _drop_folder_pdfs(provider: str) -> None:
    """Unregister a provider's folder PDFs (e.g. when the provider is removed)."""
    reg = _load_pdfs()
    prefix = f"folder:{provider}:"
    stale = [k for k in reg if k.startswith(prefix) and reg[k].get("source") == "folder"]
    if stale:
        for k in stale:
            del reg[k]
        _save_pdfs(reg)


def _sync_folder(folder: str, provider: str) -> dict:
    """Add every .torrent in `folder` (registered as a magnet, like a drop)
    and merge a feed.json if one is present. Reuses the drop path so the
    metadata is in hand immediately — no DHT wait."""
    folder = Path(folder)
    added, skipped, errors = [], [], []
    for tfile in sorted(folder.glob("*.torrent")):
        try:
            ti = lt.torrent_info(str(tfile))
        except Exception as exc:
            errors.append({"file": tfile.name, "error": f"not a valid .torrent: {exc}"})
            continue
        name = _as_str(ti.name())
        ih_hex = _ih_hex(ti.info_hash)
        magnet = _build_magnet(ti)
        UPLOAD_DIR.mkdir(exist_ok=True)
        dest = str(UPLOAD_DIR / f"{ih_hex}.torrent")
        shutil.copyfile(str(tfile), dest)
        already = catalog.has(magnet)
        if not already:
            catalog.add(name, magnet, [], provider=provider)
            catalog.save()
        engine.add_torrent(dest, magnet=magnet)
        (skipped if already else added).append({"name": name, "info_hash": ih_hex})
    feed_added = 0
    feed_json = folder / "feed.json"
    if feed_json.exists():
        feed_added = catalog.sync(str(feed_json), provider=provider)
    pdfs = _sync_folder_pdfs(str(folder), provider)
    return {"added": len(added), "skipped": len(skipped), "errors": errors,
            "torrents": len(added), "feed": feed_added, "pdfs": pdfs}


def _plugin_providers() -> list[dict]:
    """A tab per *active* plugin. An active plugin is a live provider: its tab
    shows a search box (plus its config controls) and lists results on demand.
    Plugin results are tagged provider "plugin:<id>" in the catalog/PDF
    registry, so anything it has loaded also shows up under its own tab."""
    out = []
    for pl in plugin_loader.list_plugins():
        if not pl.get("active"):
            continue
        out.append({"id": f"plugin:{pl['id']}", "name": pl["name"],
                    "feed": "", "description": pl.get("description", ""),
                    "enabled": True, "plugin": True, "live": True,
                    "has_covers": pl.get("has_covers", False),
                    "config_schema": pl.get("config_schema"),
                    "config": pl.get("config", {})})
    return out


@app.get("/api/providers")
def list_providers() -> JSONResponse:
    return JSONResponse([LOCAL_PROVIDER]
                        + [_provider_view(p) for p in providers.items()]
                        + _plugin_providers())


class ProviderRequest(BaseModel):
    name: str
    feed: str = ""
    description: str = ""


@app.post("/api/providers")
def add_provider(req: ProviderRequest) -> JSONResponse:
    if not req.name.strip():
        raise HTTPException(400, "name required")
    if req.feed and not req.feed.startswith(("http://", "https://")) \
            and not Path(req.feed).exists():
        raise HTTPException(400, "feed must be a URL, an existing file, or a folder")
    p = providers.add(req.name.strip(), req.feed.strip(), req.description.strip())
    providers.save()
    if p.feed:
        try:
            summary = _sync_provider_feed(p.feed, p.id)
            summary["pdfs"] = _sync_folder_pdfs(p.feed, p.id)
            _enqueue_new()
        except Exception as exc:
            return JSONResponse({"ok": True, "id": p.id, "sync_error": str(exc)})
        return JSONResponse({"ok": True, "id": p.id, **summary})
    return JSONResponse({"ok": True, "id": p.id})


@app.delete("/api/providers/{pid}")
def delete_provider(pid: str) -> JSONResponse:
    if pid == "local":
        raise HTTPException(400, "the local provider cannot be removed")
    if not providers.remove(pid):
        raise HTTPException(404, "provider not found")
    providers.save()
    _drop_folder_pdfs(pid)  # its folder PDFs are no longer reachable
    return JSONResponse({"ok": True})


@app.post("/api/providers/{pid}/enabled")
def set_provider_enabled(pid: str, enabled: bool = True) -> JSONResponse:
    if pid == "local":
        raise HTTPException(400, "the local provider cannot be disabled")
    if not providers.set_enabled(pid, enabled):
        raise HTTPException(404, "provider not found")
    providers.save()
    return JSONResponse({"ok": True})


@app.post("/api/providers/{pid}/sync")
def sync_provider(pid: str) -> JSONResponse:
    """Re-pull one provider's feed and enqueue any new torrents."""
    if pid == "local":
        return sync_feed()
    p = providers.get(pid)
    if p is None:
        raise HTTPException(404, "provider not found")
    if not p.feed:
        raise HTTPException(400, "provider has no feed")
    try:
        summary = _sync_provider_feed(p.feed, p.id)
        summary["pdfs"] = _sync_folder_pdfs(p.feed, p.id)
    except Exception as exc:
        raise HTTPException(502, f"feed fetch failed: {exc}")
    _enqueue_new()
    return JSONResponse(summary)


@app.post("/api/providers/browse")
def browse_folder() -> JSONResponse:
    """Open a native folder picker on the server and return the chosen path.

    The browser can't read a server-side folder, so the dialog runs here. This
    is a sync (def) endpoint, so FastAPI runs it in the threadpool and the
    blocking dialog won't stall the event loop. Returns {path: ""} if the user
    cancels or no GUI is available."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        raise HTTPException(501, "folder picker unavailable on this system")
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)  # bring the dialog above the browser
    try:
        path = filedialog.askdirectory(title="Choose a folder to sync")
    finally:
        root.destroy()
    return JSONResponse({"path": path})


# ---------- plugins ----------
# A plugin is a behaviour (a .py file in plugins/) that, given a query, returns
# items to load: torrents (magnet) or PDFs (url/path). Running one tags its
# results with provider "plugin:<id>" so they appear under their own tab and
# reuse the existing catalog + direct-PDF + tab machinery.

class PluginRunRequest(BaseModel):
    query: str = ""


class PluginSearchRequest(BaseModel):
    query: str = ""
    config: dict | None = None


class PluginActiveRequest(BaseModel):
    active: bool


class PluginConfigRequest(BaseModel):
    config: dict


class PluginOpenRequest(BaseModel):
    url: str
    name: str = ""


class PluginAddRequest(BaseModel):
    name: str
    code: str
    needs_query: bool = True


class PluginPathRequest(BaseModel):
    path: str


def _plugin_config_defaults(schema) -> dict:
    """Build a config dict from a plugin's CONFIG schema defaults."""
    out = {}
    for c in (schema or []):
        if isinstance(c, dict) and c.get("key"):
            out[c["key"]] = c.get("default", "")
    return out


def _download_pdf(url: str, name: str, provider: str) -> dict:
    """Fetch a remote PDF and register it as a direct PDF (source "plugin").
    Returns the registry entry. Raises on a non-PDF or a fetch failure.
    Re-downloading the same URL is a no-op (returns the existing entry) so
    re-running a plugin query doesn't pile up duplicates."""
    reg = _load_pdfs()
    for pid, m in reg.items():
        if m.get("url") == url:
            return m
    req = urllib.request.Request(url, headers={"User-Agent": "pdf-torrent-reader/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    if not data.startswith(b"%PDF-"):
        raise ValueError("not a PDF (missing %PDF header)")
    PDF_DIR.mkdir(exist_ok=True)
    pid = uuid.uuid4().hex
    (PDF_DIR / f"{pid}.pdf").write_bytes(data)
    reg[pid] = {"name": name, "size": len(data), "provider": provider,
                "added_at": time.time(), "source": "plugin", "url": url,
                **_pdf_meta(str(PDF_DIR / f"{pid}.pdf"))}
    _save_pdfs(reg)
    return reg[pid]


@app.get("/api/plugins")
def list_plugins() -> JSONResponse:
    return JSONResponse(plugin_loader.list_plugins())


@app.post("/api/plugins/{pid}/active")
def set_plugin_active(pid: str, req: PluginActiveRequest) -> JSONResponse:
    """Toggle a plugin active. An active plugin becomes a live provider tab."""
    if not any(p["id"] == pid for p in plugin_loader.list_plugins()):
        raise HTTPException(404, "plugin not found")
    plugin_loader.set_active(pid, req.active)
    return JSONResponse({"ok": True, "active": req.active})


@app.post("/api/plugins/{pid}/config")
def set_plugin_config(pid: str, req: PluginConfigRequest) -> JSONResponse:
    """Save a plugin's config (its CONFIG schema values)."""
    if not any(p["id"] == pid for p in plugin_loader.list_plugins()):
        raise HTTPException(404, "plugin not found")
    plugin_loader.set_config(pid, req.config)
    return JSONResponse({"ok": True})


@app.get("/api/plugins/{pid}/search")
def search_plugin(pid: str, query: str = "", config: str = "") -> JSONResponse:
    """Run a plugin's live search(query, config) and return its items. The
    config is passed as a JSON string (query params can't carry objects)."""
    cfg = {}
    if config:
        try:
            cfg = json.loads(config)
        except json.JSONDecodeError:
            cfg = {}
    try:
        items = plugin_loader.search_plugin(pid, query, cfg)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"plugin search failed: {exc}")
    return JSONResponse({"ok": True, "items": items})


class PluginResolveRequest(BaseModel):
    ids: List[str] = []


@app.post("/api/plugins/{pid}/resolve")
def resolve_plugin(pid: str, req: PluginResolveRequest) -> JSONResponse:
    """Stage 2 of a two-stage provider: resolve a batch of pending result ids
    (e.g. archive.org identifiers) to their openable URLs. The plugin's
    resolve() fetches the metadata in parallel and returns {id: {...}|None}."""
    try:
        resolved = plugin_loader.resolve_plugin(pid, req.ids)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"plugin resolve failed: {exc}")
    return JSONResponse({"ok": True, "resolved": resolved})


class PluginCoversRequest(BaseModel):
    items: List[dict] = []


@app.post("/api/plugins/{pid}/covers")
def covers_plugin(pid: str, req: PluginCoversRequest) -> JSONResponse:
    """Stage 2 of a live search: fetch cover images for the listed results.
    The plugin's covers(items) runs in parallel (it may hit the network per
    item) and returns a list of cover URLs aligned by index with `items`.
    The front end fires this right after the list loads, so covers appear
    without delaying the list itself."""
    try:
        covers = plugin_loader.covers_plugin(pid, req.items)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"plugin covers failed: {exc}")
    return JSONResponse({"ok": True, "covers": covers})


@app.post("/api/plugins/{pid}/open")
def open_plugin_pdf(pid: str, req: PluginOpenRequest) -> JSONResponse:
    """Download a search-result PDF (by url) and return its direct-PDF id so
    the front end can open it in the viewer. Re-opening the same URL is a
    no-op (deduped by URL)."""
    provider = f"plugin:{pid}"
    try:
        _download_pdf(req.url, req.name, provider)
    except Exception as exc:
        raise HTTPException(502, f"could not open PDF: {exc}")
    reg = _load_pdfs()
    pdf_id = next((k for k, m in reg.items() if m.get("url") == req.url), None)
    if pdf_id is None:
        raise HTTPException(502, "could not open PDF")
    return JSONResponse({"ok": True, "id": pdf_id})


@app.post("/api/plugins/{pid}/run")
def run_plugin(pid: str, req: PluginRunRequest | None = None) -> JSONResponse:
    """Run a plugin's fetch(query) and load its results. Torrents go into the
    catalog + session; PDFs are downloaded and registered as direct PDFs."""
    query = (req.query if req else "") or ""
    provider = f"plugin:{pid}"
    try:
        items = plugin_loader.run_plugin(pid, query)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"plugin failed: {exc}")
    torrents, pdfs, errors = 0, 0, []
    for it in items:
        try:
            if it.get("magnet"):
                if not catalog.has(it["magnet"]):
                    catalog.add(it["name"], it["magnet"], [], provider=provider)
                    catalog.save()
                    engine.add_torrent_async(it["magnet"])
                    torrents += 1
            elif it.get("url"):
                _download_pdf(it["url"], it["name"], provider)
                pdfs += 1
            elif it.get("path"):
                _register_local_pdf(it["path"], it["name"], provider)
                pdfs += 1
        except Exception as exc:
            errors.append({"name": it.get("name", ""), "error": str(exc)})
    _enqueue_new()
    return JSONResponse({"ok": True, "torrents": torrents, "pdfs": pdfs,
                         "errors": errors, "provider": provider})


@app.get("/api/plugins/{pid}/source")
def plugin_source(pid: str) -> JSONResponse:
    """A plugin's current source + metadata, so the front end can prefill the
    edit modal."""
    path = plugin_loader.PLUGIN_DIR / f"{pid}.py"
    if not path.is_file():
        raise HTTPException(404, f"plugin not found: {pid}")
    meta = plugin_loader._meta_from_source(path)
    return JSONResponse({"ok": True, "name": meta["name"],
                         "needs_query": meta["needs_query"],
                         "code": path.read_text(encoding="utf-8", errors="replace")})


@app.post("/api/plugins/from-path")
def load_plugin_from_path(req: PluginPathRequest) -> JSONResponse:
    """Read a plugin .py file from a path and return its metadata + source so
    the front end can prefill the Add-plugin form (the user still clicks Add
    to register it)."""
    try:
        info = plugin_loader.load_plugin_from_path(req.path)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except (ValueError, SyntaxError) as exc:
        raise HTTPException(400, str(exc))
    return JSONResponse({"ok": True, **info})


@app.post("/api/plugins")
def add_plugin(req: PluginAddRequest) -> JSONResponse:
    try:
        info = plugin_loader.add_plugin(req.name, req.code, req.needs_query)
    except (ValueError, SyntaxError) as exc:
        raise HTTPException(400, str(exc))
    return JSONResponse({"ok": True, **info})


class PluginUpdateRequest(BaseModel):
    name: str
    code: str
    needs_query: bool = True


@app.put("/api/plugins/{pid}")
def update_plugin(pid: str, req: PluginUpdateRequest) -> JSONResponse:
    """Overwrite a plugin's source. The id (and its active flag + config) is
    unchanged — only the .py file is rewritten."""
    try:
        info = plugin_loader.update_plugin(pid, req.name, req.code, req.needs_query)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except (ValueError, SyntaxError) as exc:
        raise HTTPException(400, str(exc))
    return JSONResponse({"ok": True, **info})


@app.delete("/api/plugins/{pid}")
def remove_plugin(pid: str) -> JSONResponse:
    if not plugin_loader.remove_plugin(pid):
        raise HTTPException(404, "plugin not found")
    return JSONResponse({"ok": True})


@app.delete("/api/plugins/{pid}/items")
def clear_plugin_items(pid: str) -> JSONResponse:
    """Drop everything a plugin loaded (its catalog torrents + direct PDFs)."""
    provider = f"plugin:{pid}"
    removed_torrents = catalog.remove_provider(provider)
    reg = _load_pdfs()
    removed_pdfs = [k for k, m in reg.items()
                    if m.get("provider") == provider and m.get("source") == "plugin"]
    for k in removed_pdfs:
        del reg[k]
        (PDF_DIR / f"{k}.pdf").unlink(missing_ok=True)
    if removed_pdfs:
        _save_pdfs(reg)
    return JSONResponse({"ok": True, "torrents": removed_torrents, "pdfs": len(removed_pdfs)})


def _register_local_pdf(path: str, name: str, provider: str) -> None:
    """Register a local PDF (from a plugin) to read in place, like a folder PDF."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"file not found: {path}")
    reg = _load_pdfs()
    pid = f"pluginfile:{provider}:{p.name}"
    reg[pid] = {"name": name or p.name, "size": p.stat().st_size,
                "provider": provider, "added_at": time.time(),
                "source": "folder", "path": str(p), **_pdf_meta(str(p))}
    _save_pdfs(reg)


# ---------- direct PDFs ----------
# A dropped .pdf is stored on disk under .pdfs/ and served to PDF.js with full
# HTTP Range support (no torrent involved). The registry (pdfs.json) maps id ->
# metadata so the UI can list them under a provider tab.

def _load_pdfs() -> dict:
    try:
        return json.loads(PDFS_INDEX.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_pdfs(reg: dict) -> None:
    tmp = PDFS_INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    tmp.replace(PDFS_INDEX)


def _load_favorites() -> list:
    try:
        data = json.loads(FAVORITES_INDEX.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_favorites(favs: list) -> None:
    tmp = FAVORITES_INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(favs, indent=2), encoding="utf-8")
    tmp.replace(FAVORITES_INDEX)


def _load_shelf() -> list:
    try:
        data = json.loads(SHELF_INDEX.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_shelf(shelf: list) -> None:
    tmp = SHELF_INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(shelf, indent=2), encoding="utf-8")
    tmp.replace(SHELF_INDEX)


def _load_recent() -> list:
    try:
        data = json.loads(RECENT_INDEX.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_recent(items: list) -> None:
    tmp = RECENT_INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    tmp.replace(RECENT_INDEX)


def _load_texts() -> dict:
    try:
        return json.loads(TEXTS_INDEX.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_texts(reg: dict) -> None:
    tmp = TEXTS_INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    tmp.replace(TEXTS_INDEX)


def _load_epubs() -> dict:
    try:
        return json.loads(EPUBS_INDEX.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_epubs(reg: dict) -> None:
    tmp = EPUBS_INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    tmp.replace(EPUBS_INDEX)


def _load_files() -> dict:
    try:
        return json.loads(FILES_INDEX.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_files(reg: dict) -> None:
    tmp = FILES_INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    tmp.replace(FILES_INDEX)


def _pdf_meta(path: str) -> dict:
    """Extract filterable metadata from a PDF (title/author/subject/keywords/
    page count) plus the file's mtime. Returns {} on any error so a broken or
    password-protected PDF never breaks the list. Called at import time only
    (upload / download / folder sync) — never on the list path, so a slow
    extraction can't stall the UI."""
    out = {}
    try:
        from pypdf import PdfReader
        r = PdfReader(path)
        md = r.metadata
        if md:
            for key in ("title", "author", "subject", "keywords"):
                v = getattr(md, key, None)
                if v:
                    out[key] = str(v).strip()
        try:
            out["pages"] = len(r.pages)
        except Exception:
            pass
    except Exception:
        pass
    try:
        out["mtime"] = int(Path(path).stat().st_mtime)
    except OSError:
        pass
    return out


# ---------- local PDF covers (first page rendered to a data URI) ----------
# A book's cover is its first page, rendered server-side to a small JPEG and
# stored as a base64 data URI in the pdfs.json registry entry (one render per
# PDF — re-requests are served from the registry). pypdfium2 is a
# self-contained PDFium build, so no system poppler is needed. Rendering is
# best-effort: a scanned, encrypted, or otherwise odd PDF simply has no cover
# (the row shows nothing) and never breaks the list.
# PDFium is not thread-safe: two concurrent renders (even from two separate
# requests) can segfault the whole process. This lock serializes every render.
_cover_lock = threading.Lock()


def _render_pdf_cover_data(path: str) -> str | None:
    """Render a PDF's first page to a ~200px-wide JPEG data URI.
    Returns None when the PDF has no renderable first page."""
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        try:
            if len(pdf) == 0:
                return None
            page = pdf[0]
            width = page.get_width() or 612.0
            scale = 200.0 / width  # fixed thumbnail width, any page size
            img = page.render(scale=scale).to_pil()
            if img.mode != "RGB":
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=72)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        finally:
            pdf.close()
    except Exception:
        return None


class PdfCoversRequest(BaseModel):
    ids: List[str] = []


@app.post("/api/pdfs/covers")
def pdf_covers(req: PdfCoversRequest) -> JSONResponse:
    """Render first-page covers for local PDFs that don't have one yet and
    store them in the registry. Already-covered ids are skipped, so this is
    cheap to call repeatedly (the front end fires it after every list load).
    Renders run sequentially: PDFium is not thread-safe, so concurrent renders
    can crash the process — the list still loads first and covers appear a
    moment later because this endpoint runs off the main flow."""
    reg = _load_pdfs()
    missing = [pid for pid in req.ids
               if pid in reg and not reg[pid].get("cover")]
    if missing:
        for pid in missing:
            meta = reg[pid]
            path = Path(meta["path"]) if meta.get("source") == "folder" else PDF_DIR / f"{pid}.pdf"
            if not path.exists():
                continue
            with _cover_lock:
                data = _render_pdf_cover_data(str(path))
            if data:
                reg[pid]["cover"] = data
        _save_pdfs(reg)
    return JSONResponse({"ok": True,
                         "covers": {pid: reg[pid]["cover"] for pid in req.ids
                                    if pid in reg and reg[pid].get("cover")}})


@app.post("/api/pdf/upload")
async def upload_pdf(files: List[UploadFile] = File(...),
                     provider: str = Form("local")) -> JSONResponse:
    """Accept one or more dropped .pdf files, store them, and register each."""
    if not files:
        raise HTTPException(400, "no files provided")
    PDF_DIR.mkdir(exist_ok=True)
    reg = _load_pdfs()
    added, errors = [], []
    for uf in files:
        data = await uf.read()
        fname = _as_str(uf.filename) if uf.filename else "document.pdf"
        if not data:
            errors.append({"file": fname, "error": "empty file"})
            continue
        if not data.startswith(b"%PDF-"):
            errors.append({"file": fname, "error": "not a PDF (missing %PDF header)"})
            continue
        pid = uuid.uuid4().hex
        dest = PDF_DIR / f"{pid}.pdf"
        dest.write_bytes(data)
        # metadata is extracted at import time (off the event loop — this is an
        # async endpoint and pypdf can take seconds on a big file)
        reg[pid] = {"name": fname, "size": len(data),
                    "provider": provider, "added_at": time.time(),
                    **await anyio.to_thread.run_sync(_pdf_meta, str(dest))}
        added.append({"id": pid, "name": fname, "size": len(data)})
    if errors and not added:
        _save_pdfs(reg)
        raise HTTPException(400, "; ".join(f"{e['file']}: {e['error']}" for e in errors))
    _save_pdfs(reg)
    return JSONResponse({"ok": True, "added": added, "errors": errors})


class AddPdfUrlRequest(BaseModel):
    url: str
    name: str = ""


@app.post("/api/pdf/add-url")
def add_pdf_url(req: AddPdfUrlRequest) -> JSONResponse:
    """Download a remote PDF (e.g. an arXiv link) and register it as a direct
    PDF, so it can be opened in the viewer. Re-adding the same URL is a no-op
    (deduped by URL)."""
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "paste an http(s) PDF link")
    name = req.name.strip() or url.rsplit("/", 1)[-1].split("?")[0] or "document.pdf"
    # provider "local" so it lists under the Local provider, like uploads
    try:
        entry = _download_pdf(url, name, "local")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"could not download PDF: {exc}")
    pid = next(k for k, m in _load_pdfs().items() if m.get("url") == url)
    return JSONResponse({"ok": True, "id": pid, "name": entry["name"], "size": entry["size"]})


@app.get("/api/pdfs")
def list_pdfs() -> JSONResponse:
    reg = _load_pdfs()
    return JSONResponse([
        {"id": pid, "name": m["name"], "size": m["size"],
         "provider": m.get("provider", "local"),
         "source": m.get("source", "upload"),
         "url": m.get("url", ""),
         "cached_from": m.get("cached_from"),
         "title": m.get("title", ""), "author": m.get("author", ""),
         "subject": m.get("subject", ""), "keywords": m.get("keywords", ""),
         "pages": m.get("pages", 0), "mtime": m.get("mtime", 0),
         "cover": m.get("cover")}
        for pid, m in reg.items()
    ])


@app.post("/api/pdfs/{pid}/open-folder")
def open_pdf_folder(pid: str) -> JSONResponse:
    """Open the folder a PDF lives in, in the OS file manager. Folder-loaded
    PDFs open their source folder; downloaded/uploaded PDFs open .pdfs/."""
    reg = _load_pdfs()
    m = reg.get(pid)
    if not m:
        raise HTTPException(404, "pdf not found")
    path = m.get("path")
    if path and Path(path).is_file():
        folder = Path(path).parent
    else:
        # downloaded/uploaded PDFs are stored in PDF_DIR as <pid>.pdf
        candidate = PDF_DIR / f"{pid}.pdf"
        if not candidate.is_file():
            raise HTTPException(400, "file not found on disk")
        folder = PDF_DIR
    try:
        if os.name == "nt":
            os.startfile(str(folder))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception as exc:
        raise HTTPException(500, f"could not open folder: {exc}")
    return JSONResponse({"ok": True, "path": str(folder)})


class PdfMetaRequest(BaseModel):
    title: str = ""
    author: str = ""
    subject: str = ""
    keywords: str = ""


@app.put("/api/pdfs/{pid}/meta")
def update_pdf_meta(pid: str, req: PdfMetaRequest) -> JSONResponse:
    """Manually edit a PDF's metadata (overrides the auto-extracted values).
    Only the four text fields are editable; pages/size/mtime stay as-is."""
    reg = _load_pdfs()
    m = reg.get(pid)
    if not m:
        raise HTTPException(404, "pdf not found")
    for key in ("title", "author", "subject", "keywords"):
        m[key] = getattr(req, key).strip()
    _save_pdfs(reg)
    return JSONResponse({"ok": True})


@app.delete("/api/pdfs/{pid}")
def delete_pdf(pid: str) -> JSONResponse:
    reg = _load_pdfs()
    if pid not in reg:
        raise HTTPException(404, "pdf not found")
    is_folder = reg[pid].get("source") == "folder"
    del reg[pid]
    _save_pdfs(reg)
    # only uploaded PDFs live in .pdfs/; folder PDFs are read in place and
    # must not be deleted from the user's folder
    if not is_folder:
        (PDF_DIR / f"{pid}.pdf").unlink(missing_ok=True)
    return JSONResponse({"ok": True})


class FavoriteRequest(BaseModel):
    key: str
    name: str = ""
    type: str = "live"   # torrent | pdf | live
    ih: str = ""
    pdf: str = ""
    url: str = ""
    magnet: str = ""
    kind: str = ""
    provider: str = "local"
    status: str = "toread"   # toread | reading | complete
    tags: list = []          # custom user tags (free-form strings)
    group: str = ""          # entries sharing a group render as one expandable box
    cover: str = ""          # cover image (data URI or URL), saved with the entry


class FavoriteStatusRequest(BaseModel):
    key: str
    status: str   # toread | reading | complete


class FavoriteTagsRequest(BaseModel):
    key: str
    tags: list    # full replacement of the favorite's tag list


class ShelfPositionRequest(BaseModel):
    key: str
    page: int = 0        # 1-based PDF page (0 = not a PDF / unknown)
    scroll: float = 0.0  # 0..1 fraction through a plain-text document
    cfi: str = ""        # EPUB CFI (empty = not an EPUB)


class RecentRequest(BaseModel):
    key: str
    name: str = ""
    type: str = "live"   # torrent | pdf | live
    ih: str = ""
    f: int = 0           # torrent file index (type == torrent)
    pdf: str = ""        # direct pdf id (type == pdf)
    url: str = ""
    magnet: str = ""
    kind: str = ""
    provider: str = "local"
    shelf_key: str = ""  # original shelf key, for position resumption on re-open
    size: int = 0
    pages: int = 0


@app.get("/api/recent")
def list_recent() -> JSONResponse:
    return JSONResponse(_load_recent())


@app.post("/api/recent")
def add_recent(req: RecentRequest) -> JSONResponse:
    """Record a book open. Re-opening moves the entry to the front; the list
    is capped so it stays a small, recent-history list rather than a log."""
    items = [r for r in _load_recent() if r["key"] != req.key]
    items.insert(0, {"key": req.key, "name": req.name, "type": req.type,
                     "ih": req.ih, "f": req.f, "pdf": req.pdf, "url": req.url,
                     "magnet": req.magnet, "kind": req.kind,
                     "provider": req.provider, "shelf_key": req.shelf_key,
                     "size": req.size, "pages": req.pages,
                     "opened_at": time.time()})
    _save_recent(items[:50])
    return JSONResponse({"ok": True})


@app.delete("/api/recent")
def remove_recent(key: str) -> JSONResponse:
    items = _load_recent()
    kept = [r for r in items if r["key"] != key]
    if len(kept) == len(items):
        raise HTTPException(404, "not in recent list")
    _save_recent(kept)
    return JSONResponse({"ok": True})


@app.get("/api/favorites")
def list_favorites() -> JSONResponse:
    return JSONResponse(_load_favorites())


@app.get("/api/favorites/tags")
def list_favorite_tags() -> JSONResponse:
    """All distinct custom tags across favorites (sorted)."""
    tags = set()
    for f in _load_favorites():
        for t in f.get("tags", []):
            if t:
                tags.add(t)
    return JSONResponse(sorted(tags))


@app.post("/api/favorites")
def add_favorite(req: FavoriteRequest) -> JSONResponse:
    favs = _load_favorites()
    if any(f["key"] == req.key for f in favs):
        return JSONResponse({"ok": True, "added": False})
    fav = {"key": req.key, "name": req.name, "type": req.type,
           "ih": req.ih, "pdf": req.pdf, "url": req.url,
           "magnet": req.magnet, "kind": req.kind, "provider": req.provider,
           "status": req.status, "tags": req.tags, "group": req.group,
           "cover": req.cover, "added_at": time.time()}
    favs.append(fav)
    _save_favorites(favs)
    # a favorite is always on the shelf (favorites ⊆ shelf)
    shelf = _load_shelf()
    if not any(s["key"] == fav["key"] for s in shelf):
        shelf.append(dict(fav))
        _save_shelf(shelf)
    return JSONResponse({"ok": True, "added": True})


@app.get("/api/shelf")
def list_shelf() -> JSONResponse:
    return JSONResponse(_load_shelf())


@app.post("/api/shelf")
def add_to_shelf(req: FavoriteRequest) -> JSONResponse:
    shelf = _load_shelf()
    if any(s["key"] == req.key for s in shelf):
        return JSONResponse({"ok": True, "added": False})
    shelf.append({"key": req.key, "name": req.name, "type": req.type,
                  "ih": req.ih, "pdf": req.pdf, "url": req.url,
                  "magnet": req.magnet, "kind": req.kind, "provider": req.provider,
                  "status": req.status, "tags": req.tags, "group": req.group,
                  "cover": req.cover, "added_at": time.time()})
    _save_shelf(shelf)
    return JSONResponse({"ok": True, "added": True})


@app.delete("/api/shelf")
def remove_from_shelf(key: str) -> JSONResponse:
    shelf = _load_shelf()
    kept = [s for s in shelf if s["key"] != key]
    if len(kept) == len(shelf):
        raise HTTPException(404, "not on shelf")
    _save_shelf(kept)
    return JSONResponse({"ok": True})


@app.get("/api/shelf/tags")
def list_shelf_tags() -> JSONResponse:
    """All distinct custom tags across shelf entries (sorted)."""
    tags = set()
    for s in _load_shelf():
        for t in s.get("tags", []):
            if t:
                tags.add(t)
    return JSONResponse(sorted(tags))


@app.post("/api/shelf/status")
def set_shelf_status(req: FavoriteStatusRequest) -> JSONResponse:
    shelf = _load_shelf()
    for s in shelf:
        if s["key"] == req.key:
            s["status"] = req.status
            _save_shelf(shelf)
            return JSONResponse({"ok": True})
    raise HTTPException(404, "not on shelf")


@app.post("/api/shelf/tags")
def set_shelf_tags(req: FavoriteTagsRequest) -> JSONResponse:
    shelf = _load_shelf()
    for s in shelf:
        if s["key"] == req.key:
            s["tags"] = req.tags
            _save_shelf(shelf)
            return JSONResponse({"ok": True})
    raise HTTPException(404, "not on shelf")


@app.post("/api/shelf/position")
def set_shelf_position(req: ShelfPositionRequest) -> JSONResponse:
    """Remember where the reader stopped so she can pick up where she left
    off. Opening a book is what makes it "in progress", so the first saved
    position also promotes a toread book to reading."""
    shelf = _load_shelf()
    for s in shelf:
        if s["key"] == req.key:
            s["page"] = req.page
            s["scroll"] = req.scroll
            s["cfi"] = req.cfi
            # actually reading (past the first page — PDF or EPUB location — /
            # a bit into the text) promotes a toread book to in-progress
            if s.get("status", "toread") == "toread" and (req.page > 1 or req.scroll > 0.02):
                s["status"] = "reading"
            _save_shelf(shelf)
            return JSONResponse({"ok": True})
    raise HTTPException(404, "not on shelf")


@app.post("/api/favorites/status")
def set_favorite_status(req: FavoriteStatusRequest) -> JSONResponse:
    favs = _load_favorites()
    for f in favs:
        if f["key"] == req.key:
            f["status"] = req.status
            _save_favorites(favs)
            return JSONResponse({"ok": True})
    raise HTTPException(404, "favorite not found")


@app.post("/api/favorites/tags")
def set_favorite_tags(req: FavoriteTagsRequest) -> JSONResponse:
    favs = _load_favorites()
    for f in favs:
        if f["key"] == req.key:
            f["tags"] = req.tags
            _save_favorites(favs)
            return JSONResponse({"ok": True})
    raise HTTPException(404, "favorite not found")


@app.delete("/api/favorites")
def remove_favorite(key: str) -> JSONResponse:
    favs = _load_favorites()
    kept = [f for f in favs if f["key"] != key]
    if len(kept) == len(favs):
        raise HTTPException(404, "favorite not found")
    _save_favorites(kept)
    return JSONResponse({"ok": True})


@app.get("/api/pdf/direct")
def stream_direct_pdf(pid: str, request: Request) -> Response:
    """Serve a direct .pdf with full HTTP Range support (PDF.js contract).
    Uploaded PDFs come from .pdfs/; folder PDFs are streamed in place from
    the provider's folder (the registry holds their real path)."""
    meta = _load_pdfs().get(pid)
    if meta is None:
        raise HTTPException(404, "pdf not found")
    path = Path(meta["path"]) if meta.get("source") == "folder" else PDF_DIR / f"{pid}.pdf"
    if not path.exists():
        raise HTTPException(404, "pdf not found")
    size = path.stat().st_size
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
        headers = {"Content-Range": f"bytes {start}-{end}/{size}",
                   "Content-Length": str(length), "Accept-Ranges": "bytes"}
    else:
        start, length, status_code = 0, size, 200
        headers = {"Content-Length": str(length), "Accept-Ranges": "bytes"}
    with open(path, "rb") as fh:
        fh.seek(start)
        data = fh.read(length)
    return Response(data, status_code=status_code, media_type="application/pdf", headers=headers)


def _utf8(data: bytes) -> bytes:
    """Normalize a plain-text document to UTF-8.

    Older Gutenberg files are Windows-1252 (single-byte), not UTF-8. Serving
    those raw with a utf-8 charset makes the browser mangle the high bytes
    (prime marks, degree signs, math operators). Decode as UTF-8 when valid,
    else fall back to cp1252, and always re-encode to UTF-8."""
    try:
        return data.decode("utf-8").encode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252").encode("utf-8")


@app.get("/api/text")
def proxy_text(url: str) -> Response:
    """Fetch a remote plain-text document and return it so the in-site reader
    can show it (the browser can't fetch gutenberg.org directly — no CORS).
    Only http(s) URLs are allowed."""
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "only http(s) URLs are allowed")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pdf-torrent-reader/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
    except Exception as exc:
        raise HTTPException(502, f"could not fetch text: {exc}")
    return Response(_utf8(data), media_type="text/plain; charset=utf-8")


@app.get("/api/html")
def proxy_html(url: str) -> Response:
    """Fetch a remote HTML document and return it so the in-site viewer can
    show it (the browser can't fetch gutenberg.org directly — no CORS).
    Only http(s) URLs are allowed."""
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "only http(s) URLs are allowed")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pdf-torrent-reader/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "text/html")
    except Exception as exc:
        raise HTTPException(502, f"could not fetch html: {exc}")
    return Response(data, media_type=ctype)


@app.get("/api/epub")
def proxy_epub(url: str) -> Response:
    """Fetch a remote EPUB and return the raw bytes so the in-site reader
    (epub.js) can parse it (the browser can't fetch gutenberg.org directly —
    no CORS). Only http(s) URLs are allowed."""
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "only http(s) URLs are allowed")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pdf-torrent-reader/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
    except Exception as exc:
        raise HTTPException(502, f"could not fetch epub: {exc}")
    return Response(data, media_type="application/epub+zip")


# ---------- saved texts ----------
# A remote plain-text document can be saved to disk under .texts/ so it can be
# read offline (no re-fetch, no CORS). The registry (texts.json) maps id ->
# metadata so the UI can mark which live results are already saved locally.

class SaveTextRequest(BaseModel):
    url: str
    name: str = ""
    provider: str = "local"


@app.post("/api/text/save")
def save_text(req: SaveTextRequest) -> JSONResponse:
    """Fetch a remote plain-text document and store it locally for offline
    reading. Re-saving the same URL is a no-op (deduped by URL)."""
    if not re.match(r"^https?://", req.url):
        raise HTTPException(400, "only http(s) URLs are allowed")
    reg = _load_texts()
    for tid, m in reg.items():
        if m.get("url") == req.url:
            return JSONResponse({"ok": True, "id": tid, "added": False})
    try:
        r = urllib.request.Request(req.url, headers={"User-Agent": "pdf-torrent-reader/1.0"})
        with urllib.request.urlopen(r, timeout=60) as resp:
            data = resp.read()
    except Exception as exc:
        raise HTTPException(502, f"could not fetch text: {exc}")
    data = _utf8(data)  # store as UTF-8 so offline reads match the live view
    if not data.strip():
        raise HTTPException(400, "empty document")
    TEXT_DIR.mkdir(exist_ok=True)
    tid = uuid.uuid4().hex
    (TEXT_DIR / f"{tid}.txt").write_bytes(data)
    reg[tid] = {"name": req.name, "size": len(data), "url": req.url,
                "provider": req.provider, "added_at": time.time()}
    _save_texts(reg)
    return JSONResponse({"ok": True, "id": tid, "added": True})


@app.get("/api/texts")
def list_texts() -> JSONResponse:
    return JSONResponse([
        {"id": tid, "name": m["name"], "size": m["size"], "url": m.get("url", ""),
         "provider": m.get("provider", "local")}
        for tid, m in _load_texts().items()
    ])


@app.get("/api/texts/{tid}")
def get_text(tid: str) -> Response:
    """Serve a saved text so the in-site reader can show it offline."""
    reg = _load_texts()
    if tid not in reg:
        raise HTTPException(404, "text not found")
    path = TEXT_DIR / f"{tid}.txt"
    if not path.is_file():
        raise HTTPException(404, "text file missing")
    # _utf8 is a no-op for already-UTF-8 files; it rescues any saved before the
    # save path normalized encoding
    return Response(_utf8(path.read_bytes()), media_type="text/plain; charset=utf-8")


@app.delete("/api/texts/{tid}")
def delete_text(tid: str) -> JSONResponse:
    reg = _load_texts()
    if tid not in reg:
        raise HTTPException(404, "text not found")
    del reg[tid]
    _save_texts(reg)
    (TEXT_DIR / f"{tid}.txt").unlink(missing_ok=True)
    return JSONResponse({"ok": True})


# ---------- saved EPUBs ----------
# A remote EPUB can be saved to disk under .epubs/ so it can be read offline
# (no re-fetch, no CORS). The registry (epubs.json) maps id -> metadata, the
# same pattern as saved texts.

class SaveEpubRequest(BaseModel):
    url: str
    name: str = ""
    provider: str = "local"


@app.post("/api/epub/save")
def save_epub(req: SaveEpubRequest) -> JSONResponse:
    """Fetch a remote EPUB and store it locally for offline reading.
    Re-saving the same URL is a no-op (deduped by URL)."""
    if not re.match(r"^https?://", req.url):
        raise HTTPException(400, "only http(s) URLs are allowed")
    reg = _load_epubs()
    for tid, m in reg.items():
        if m.get("url") == req.url:
            return JSONResponse({"ok": True, "id": tid, "added": False})
    try:
        r = urllib.request.Request(req.url, headers={"User-Agent": "pdf-torrent-reader/1.0"})
        with urllib.request.urlopen(r, timeout=120) as resp:
            data = resp.read()
    except Exception as exc:
        raise HTTPException(502, f"could not fetch epub: {exc}")
    if not data.strip():
        raise HTTPException(400, "empty document")
    EPUB_DIR.mkdir(exist_ok=True)
    tid = uuid.uuid4().hex
    (EPUB_DIR / f"{tid}.epub").write_bytes(data)
    reg[tid] = {"name": req.name, "size": len(data), "url": req.url,
                "provider": req.provider, "added_at": time.time()}
    _save_epubs(reg)
    return JSONResponse({"ok": True, "id": tid, "added": True})


@app.get("/api/epubs")
def list_epubs() -> JSONResponse:
    return JSONResponse([
        {"id": tid, "name": m["name"], "size": m["size"], "url": m.get("url", ""),
         "provider": m.get("provider", "local")}
        for tid, m in _load_epubs().items()
    ])


@app.get("/api/epubs/{tid}")
def get_epub(tid: str) -> Response:
    """Serve a saved EPUB so the in-site reader can show it offline."""
    reg = _load_epubs()
    if tid not in reg:
        raise HTTPException(404, "epub not found")
    path = EPUB_DIR / f"{tid}.epub"
    if not path.is_file():
        raise HTTPException(404, "epub file missing")
    return Response(path.read_bytes(), media_type="application/epub+zip")


@app.delete("/api/epubs/{tid}")
def delete_epub(tid: str) -> JSONResponse:
    reg = _load_epubs()
    if tid not in reg:
        raise HTTPException(404, "epub not found")
    del reg[tid]
    _save_epubs(reg)
    (EPUB_DIR / f"{tid}.epub").unlink(missing_ok=True)
    return JSONResponse({"ok": True})


# ---------- saved files (html / mobi / other) ----------
# The remaining live-result kinds (HTML, MOBI, generic files) can be saved to
# disk under .files/ for offline reading, the same pattern as saved texts and
# EPUBs. The registry (files.json) also records the kind so the UI can pick
# the right content type when serving.

FILE_KINDS = {"html", "mobi", "file"}
FILE_MIME = {"html": "text/html", "mobi": "application/x-mobipocket-ebook", "file": "application/octet-stream"}


class SaveFileRequest(BaseModel):
    url: str
    name: str = ""
    provider: str = "local"
    kind: str = "file"


@app.post("/api/file/save")
def save_file(req: SaveFileRequest) -> JSONResponse:
    """Fetch a remote document (html/mobi/file) and store it locally for
    offline reading. Re-saving the same URL is a no-op (deduped by URL)."""
    if not re.match(r"^https?://", req.url):
        raise HTTPException(400, "only http(s) URLs are allowed")
    kind = req.kind if req.kind in FILE_KINDS else "file"
    reg = _load_files()
    for tid, m in reg.items():
        if m.get("url") == req.url:
            return JSONResponse({"ok": True, "id": tid, "added": False})
    try:
        r = urllib.request.Request(req.url, headers={"User-Agent": "pdf-torrent-reader/1.0"})
        with urllib.request.urlopen(r, timeout=120) as resp:
            data = resp.read()
    except Exception as exc:
        raise HTTPException(502, f"could not fetch file: {exc}")
    if not data.strip():
        raise HTTPException(400, "empty document")
    FILES_DIR.mkdir(exist_ok=True)
    tid = uuid.uuid4().hex
    (FILES_DIR / f"{tid}.bin").write_bytes(data)
    reg[tid] = {"name": req.name, "size": len(data), "url": req.url,
                "provider": req.provider, "kind": kind, "added_at": time.time()}
    _save_files(reg)
    return JSONResponse({"ok": True, "id": tid, "added": True})


@app.get("/api/files")
def list_files() -> JSONResponse:
    return JSONResponse([
        {"id": tid, "name": m["name"], "size": m["size"], "url": m.get("url", ""),
         "provider": m.get("provider", "local"), "kind": m.get("kind", "file")}
        for tid, m in _load_files().items()
    ])


@app.get("/api/files/{tid}")
def get_file(tid: str) -> Response:
    """Serve a saved file so the in-site reader can show it offline."""
    reg = _load_files()
    if tid not in reg:
        raise HTTPException(404, "file not found")
    path = FILES_DIR / f"{tid}.bin"
    if not path.is_file():
        raise HTTPException(404, "file missing")
    return Response(path.read_bytes(), media_type=FILE_MIME.get(reg[tid].get("kind", "file"), "application/octet-stream"))


@app.delete("/api/files/{tid}")
def delete_file(tid: str) -> JSONResponse:
    reg = _load_files()
    if tid not in reg:
        raise HTTPException(404, "file not found")
    del reg[tid]
    _save_files(reg)
    (FILES_DIR / f"{tid}.bin").unlink(missing_ok=True)
    return JSONResponse({"ok": True})


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
