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

import json
import os
import re
import shutil
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import quote

import libtorrent as lt
from contextlib import asynccontextmanager
from typing import List
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import plugin_loader
from catalog import Catalog, ProviderCatalog
from torrent_engine import TorrentEngine, TorrentError, _ih_hex

BASE = Path(__file__).parent
UPLOAD_DIR = BASE / ".torrents"
PDF_DIR = BASE / ".pdfs"          # direct .pdf files dropped into the reader
PDFS_INDEX = BASE / "pdfs.json"   # registry of direct PDFs (id -> name/size/provider)
@asynccontextmanager
async def _lifespan(app: FastAPI):
    # startup
    engine.start()
    feed = BASE / "feed.json"
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
    try:
        yield
    finally:
        # shutdown
        engine.stop()


app = FastAPI(title="pdf-torrent-reader", lifespan=_lifespan)

engine = TorrentEngine(download_dir=str(BASE / "downloads"))
catalog = Catalog(str(BASE / "catalog.json"))
providers = ProviderCatalog(str(BASE / "providers.json"))
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


@app.post("/api/sync")
def sync_feed() -> JSONResponse:
    """Re-pull feed.json and enqueue any new torrents."""
    feed = BASE / "feed.json"
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
                        "source": "folder", "path": str(f)}
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
                "added_at": time.time(), "source": "plugin", "url": url}
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


@app.post("/api/plugins")
def add_plugin(req: PluginAddRequest) -> JSONResponse:
    try:
        info = plugin_loader.add_plugin(req.name, req.code, req.needs_query)
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
                "source": "folder", "path": str(p)}
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


def _pdf_meta(path: str) -> dict:
    """Extract filterable metadata from a PDF (title/author/subject/keywords/
    page count) plus the file's mtime. Returns {} on any error so a broken or
    password-protected PDF never breaks the list. Cached by the caller."""
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


def _ensure_pdf_meta(reg: dict) -> bool:
    """Fill in missing PDF metadata for every registered PDF that has a readable
    file. Cached in the registry (meta_done flag) so extraction runs once per
    file, not on every list call. Returns True if anything changed."""
    changed = False
    for pid, m in reg.items():
        if m.get("meta_done"):
            continue
        path = m.get("path") or str(PDF_DIR / f"{pid}.pdf")
        if Path(path).is_file():
            m.update(_pdf_meta(path))
        m["meta_done"] = True
        changed = True
    return changed


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
        reg[pid] = {"name": fname, "size": len(data),
                    "provider": provider, "added_at": time.time()}
        added.append({"id": pid, "name": fname, "size": len(data)})
    if errors and not added:
        _save_pdfs(reg)
        raise HTTPException(400, "; ".join(f"{e['file']}: {e['error']}" for e in errors))
    _save_pdfs(reg)
    return JSONResponse({"ok": True, "added": added, "errors": errors})


@app.get("/api/pdfs")
def list_pdfs() -> JSONResponse:
    reg = _load_pdfs()
    if _ensure_pdf_meta(reg):
        _save_pdfs(reg)
    return JSONResponse([
        {"id": pid, "name": m["name"], "size": m["size"],
         "provider": m.get("provider", "local"),
         "source": m.get("source", "upload"),
         "title": m.get("title", ""), "author": m.get("author", ""),
         "subject": m.get("subject", ""), "keywords": m.get("keywords", ""),
         "pages": m.get("pages", 0), "mtime": m.get("mtime", 0)}
        for pid, m in reg.items()
    ])


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
