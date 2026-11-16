# Mutuo

A shared reading library for a team: one app, one download pool. Books arrive
as torrents, direct PDF links, or from built-in providers (arXiv, Google
Books, OpenAlex, Project Gutenberg) and plugins; they are read in a native
desktop window — PDF, plain text, EPUB, or HTML — with per-person profiles,
a personal shelf, and reading-position memory.

The name is Latin for "I lend": the catalog is shared, the reading is yours.

Intended for **content you are authorized to share** (your own PDFs, internal
company documents, open-access material). Torrents are a transport for files
you control; this is not a distribution service for third-party copyrighted
material.

## Features

- **PDFs streamed from BitTorrent** — only the pieces the viewer actually
  reads are downloaded, so first paint is fast even for large files.
- **Direct PDF links** — paste any `https://…pdf` URL in the header; it is
  downloaded, validated, and read like a local file.
- **Providers & plugins** — named search sources (arXiv, Google Books,
  OpenAlex, Gutenberg) plus user plugins (`plugins/*.py`) that can add live
  search tabs, one-shot fetches, and cover images.
- **Multiple readers** — PDF.js for PDFs, an in-site text reader, epub.js
  for EPUBs, and an HTML proxy for web pages.
- **Profiles** — per-person data (shelf, favorites, saved texts, settings)
  under `profiles/<name>/`; the shared part (catalog, downloads) stays
  shared.
- **Shelf & favorites** — a personal bookshelf with To Read / Reading /
  Complete status, tags, covers, and reading-position restore (PDF page,
  text scroll %, EPUB CFI).
- **Windows installer** — one-file Inno Setup installer for a no-Python
  install (see below).

## How it works

```
Browser (PDF.js)                      FastAPI (app.py)                 libtorrent 2.x (engine thread)
     |  GET /api/pdf?ih=..&f=0            |                                  |
     |  Range: bytes=0-65535  --------->  |  parse range                     |
     |  (PDF.js header fetch)             |  wait_for_range(0, 65536) ------->  verify pieces 0..N
     |  (blocks until pieces verified)    |                                  |  (auto_managed + DHT/peers)
     |  206 Partial Content  <----------- |  fetch_range = disk read <------- |  (verified bytes on disk)
     |  ... more ranges ...               |                                  |
```

- **`torrent_engine.py`** runs one `lt.session` in a daemon thread. Each torrent
  is stored under `downloads/<info_hash>/`. It polls state every 250 ms,
  re-asserts piece priorities for whatever ranges are being read, and signals
  a condition variable when verified bytes become available.
- **`app.py`** is a small FastAPI app. `/api/pdf` maps an HTTP range to the
  torrent pieces it covers, blocks until they're verified, then reads them
  from disk and returns a `206`. Everything else PDF.js needs (`200`,
  `416`, `Accept-Ranges`) is handled too. It also serves the readers
  (text/EPUB/HTML proxies), the plugin API, and the profile-scoped
  registries (shelf, favorites, saved texts/EPUBs/files).
- **`catalog.py`** is the list of PDFs — persisted to `catalog.json`,
  synced from an optional `feed.json`. It also holds **providers**
  (`providers.json`): named torrent lists, each with its own feed. The UI
  shows one tab per provider; the "Local" tab is the built-in catalog.
- **`desktop.py`** is the native shell (pywebview / system WebView2). It
  shows a profile picker, spawns the FastAPI server scoped to
  `profiles/<name>/`, and navigates the same window to the app.
- **`static/index.html`** is a single-page UI: a rail (Shelf / Favorites /
  My Shelf / Plugins / Settings), a provider tab bar + file browser, and
  the readers. No build step, no framework.

## Install

```bash
pip install -r requirements.txt
```

Needs Python 3.10–3.13 (`libtorrent` has no 3.14 Windows wheel). The desktop
shell uses the system WebView2 runtime, which is preinstalled on Windows
10/11.

## Run

**Desktop app (recommended):**

```bash
python desktop.py
```

Pick (or create) a profile; the window opens the app for that profile.
Closing the window stops the server.

**Plain web server (no window):**

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8080
# open http://127.0.0.1:8080
```

Binds to localhost (single machine, no auth). Add PDFs by pasting a
`magnet:?xt=urn:btih:...` URI or an `https://…pdf` link in the header, or
by putting entries in `feed.json` and clicking **Sync feed**.

### feed.json format

```json
[
  {"name": "Acme Q3 report", "magnet": "magnet:?xt=urn:btih:...", "files": ["report.pdf"]},
  {"name": "Handbook",      "magnet": "magnet:?xt=urn:btih:..."}
]
```

`files` is optional — when omitted, every PDF in the torrent is offered.
The path may also be a URL; `POST /api/sync` fetches and merges it.

### Providers

The sidebar has one tab per **provider** — a named torrent list. The
**Local** tab is the built-in catalog (`catalog.json` + `feed.json`).
Click **+ provider** to add another (e.g. "Archive.org", "Company shares");
each provider points at its own feed (a URL or local file in `feed.json`
format) and gets its own tab.

- A provider's feed is merged into the catalog at startup and on **Sync
  feed** (the header button syncs the active tab). Torrents are tagged with
  the provider they came from, so each tab shows only its own list.
- Per-tab ⋯ menu: enable/disable syncing, sync now, remove the provider
  (its already-added torrents stay in the catalog and session).
- Magnets pasted or `.torrent` files dropped while a tab is active are
  filed under that provider.
- Providers persist to `providers.json` (runtime state, like `catalog.json`).

### Plugins

A plugin is a `.py` file in `plugins/` defining `search(query, config)`
(live search tab) and/or `fetch(query)` (one-shot run). Results are PDFs,
torrents, or local files; a plugin can also supply cover images. Built-in
plugins: `gutenberg.py`, `arxiv.py`, `google_books.py`, `openalex.py`.
Personal plugins are gitignored and excluded from the installer.

## Windows installer

```powershell
.\build_installer.ps1
```

1. PyInstaller builds a onedir bundle (`dist\Mutuo\`) — only built-in
   plugins are bundled.
2. Inno Setup packages it into `installer\output\Mutuo-Setup-<ver>.exe`.

Prereqs: PyInstaller in the venv, Inno Setup 6
(`winget install JRSoftware.InnoSetup`). User data lives in
`%APPDATA%\Mutuo`, so updates never touch it.

## Test

```bash
python e2e_test.py
```

- **Test A** (deterministic, no network): verifies the file, then exercises
  the HTTP layer end-to-end — `200` full, `206` head/tail ranges, `416`
  invalid range, and the catalog add/list round-trip.
- **Test B** (best-effort): a real cross-session magnet download over DHT on
  localhost. Skipped (not failed) if the sandbox blocks DHT.

## Design notes & known limits

- **Partial downloads are the point.** Only pieces covering the ranges you
  view are verified. Opening a 20 MB PDF typically pulls a few MB before
  first paint. Pieces stay on disk afterward, so re-reading is instant.
- **No auth / no multi-tenancy.** Deliberate: it's one machine, and
  profiles are a convenience, not a security boundary. If you ever point it
  at a network, add auth and treat it as a *distributor* (that materially
  changes the legal posture).
- **libtorrent 2.x binding is quirky.** Several methods you'd expect
  (`handle.file_storage()`, `file_slice()`, `piece_passed()`,
  `update_piece_priorities`) don't exist in the Python bindings; see the API
  notes at the top of `torrent_engine.py` for the working equivalents.
- **NAT/outbound UDP.** Magnet metadata and DHT discovery need outbound UDP
  (or reachable trackers/peers). Behind a restrictive NAT the metadata fetch
  can be slow; adding known peers/trackers to the magnet helps.
- **Disk.** Verified files accumulate under `downloads/`. Delete old
  info-hash dirs to reclaim space.
