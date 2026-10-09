# PDF Torrent Reader — Project Context

## What it is

A single-user, localhost-only web application that lets you **read PDFs
directly from a BitTorrent download** without waiting for the file to fully
save to disk. It pairs a `libtorrent` download engine with a FastAPI HTTP
layer that streams partial files to PDF.js in the browser.

Use case: you find a PDF on a torrent (e.g. a public-domain book from the
Internet Archive), drop the `.torrent` file or paste a magnet link, and the
PDF becomes readable in your browser as soon as the metadata and enough
pieces arrive.

## Who it's for

One person, on one machine. No auth, no multi-user support, no cloud
backend. The server binds to `127.0.0.1:8080` and trusts the local user.

## Tech stack

| Layer | Technology |
|-------|-----------|
| Front end | Vanilla HTML/CSS/JS, PDF.js 3.x (bundled in `static/pdfjs-dist/`) |
| API | Python 3.10–3.13, FastAPI, uvicorn |
| Download | libtorrent 2.x (C++ core, Python bindings) |
| Persistence | Plain JSON files (`catalog.json`, `feed.json`) |
| Testing | `e2e_test.py` — self-contained, no external deps |

## How it works (data flow)

```
User adds torrent (magnet paste, .torrent file, or feed.json)
        │
        ▼
  catalog.py  ── writes/updates catalog.json (magnet → {name, magnet, files})
        │
        ▼
  app.py  ── POST /api/catalog (magnet) or POST /api/torrent (file) or POST /api/sync
        │
        ▼
  torrent_engine.py  ── adds torrent to libtorrent session (background thread)
        │
        ├── metadata arrives → PDF file list appears in UI
        ├── pieces download  → PDF becomes readable (HTTP Range streaming)
        └── state snapshots  → GET /api/torrents (polled every 4 s by front end)
        │
        ▼
  static/index.html  ── PDF.js fetches /api/pdf?ih=..&f=.. with Range headers
```

### Key design decisions

1. **Catalog is the source of truth for *what* to track.** It's a JSON file
   on disk. The engine is in-memory and rebuilt on every restart by
   re-adding all catalog entries. Downloaded bytes also persist in
   `downloads/`, so a restart resumes from what's already on disk. This means
   the app survives restarts without a database.

2. **Info-hash is the join key.** The catalog keys each item by the info-hash
   extracted from the magnet (`Catalog._magnet_key`, the `xt=` part). The
   engine stores both `info_hash` (dict key) and `magnet` on each
   `TorrentState`. The front end matches catalog entries to engine state in
   `refresh()` via a `byMagnet` map keyed by `magnetKey()` (same info-hash
   extraction). If you change how info-hashes or magnets are generated, update
   all three layers.

3. **PDFs stream via HTTP Range, not download-then-serve.** The
   `GET /api/pdf?ih=<info_hash>&f=<file_index>` endpoint in `app.py` handles
   `Range` headers manually. This lets PDF.js render pages as they arrive,
   without waiting for the full file. A `Range` request returns `206 Partial
   Content`; a no-`Range` request returns the whole file with `200`.

4. **The engine thread is detached and daemon.** It runs a
   `libtorrent.session` in a loop, processing `session.alerts()` every
   0.25 s. It's started in the FastAPI startup hook and stopped in the
   shutdown hook. The thread is not joined on exit (the process just dies).

5. **DHT is enabled explicitly.** `start()` in `torrent_engine.py` adds two
   DHT routers (`router.bittorrent.com:6881`, `router.utorrent.com:6881`) and
   binds the session to ports 6881–6899. UPnP/LSD are left at libtorrent's
   defaults. Without DHT, magnet-only torrents with no reachable trackers
   won't find peers.

6. **DHT/session state is persisted across restarts** in
   `downloads/.session_state.json` (gitignored). A fresh libtorrent session has
   an empty DHT routing table, so a cold restart sits at 0 peers on
   DHT-reliant torrents for minutes. `start()` calls `_load_state()` to restore
   the saved blob (libtorrent 2.x `save_state` embeds the DHT table, and
   `load_state` restores it); `_save_state()` runs every 30 s from the run
   loop and once on `stop()`, written atomically via a `.tmp` rename. It's
   best-effort: if the file is missing or corrupt the engine just starts cold.

## File-by-file notes

### `app.py`
- FastAPI app, ~195 lines.
- REST endpoints:
  - `GET /` — serves `static/index.html`
  - `GET /api/catalog` — list all catalog entries
  - `POST /api/catalog` — add a magnet or `.torrent` URL to the catalog + engine
  - `POST /api/torrent` — upload one or more `.torrent` files (multipart,
    field name `files`). Each is parsed, registered as a magnet, and added
    to the engine; the response reports `added`, `skipped` (already present),
    and `errors` (per-file). Files carry full metadata, so PDF lists appear
    instantly.
  - `POST /api/sync` — re-merge feed.json into catalog, enqueue new torrents
  - `GET /api/torrents` — engine state snapshots (`status(include_files=True)`)
  - `GET /api/file?ih=..&f=..` — resolve (info_hash, file_index) to name/size
  - `GET /api/pdf?ih=..&f=..` — HTTP Range streaming for PDF.js
- `GET /` and `POST /api/catalog` are the main entry points; the drag-and-drop
  path uses `POST /api/torrent`.
- `catalog.json` and `.torrents/` are created on first write.

### `torrent_engine.py`
- `TorrentEngine` class: wraps a `libtorrent.session` in a daemon thread
  (`_run` loop, daemon thread, not joined on exit).
- `TorrentState` dataclass: `info_hash` (hex, dict key), `name`, `magnet`,
  `save_path`, `ready` (metadata received), `fs` (`lt.file_storage` or None),
  `progress`, `state`, `num_peers`, `total_size`, `total_done`, `file_progress`
  (per-file bytes done).
- `add_torrent(source, magnet="")` accepts a magnet URI, a `.torrent` file
  path, or a `.torrent` URL. Returns the info-hash hex. For magnets the file
  list is empty until metadata arrives via DHT; for files/URLs it's immediate.
- `add_torrent_async(source)`: same, on a daemon thread (used at startup/sync).
- `status(include_files=True)`: thread-safe snapshot list of dicts matching
  the front end's expectations (includes `files` with index/name/size/done).
- `file_size(ih, idx)`, `wait_for_range(ih, idx, start, length, timeout)`,
  `fetch_range(ih, idx, start, length)`: the three methods the `/api/pdf`
  endpoint uses to stream a range only after its pieces are available.
- `_load_state()` / `_save_state()`: persist the libtorrent session blob
  (2.x `save_state` embeds the DHT routing table) to
  `downloads/.session_state.json` so a restart resumes with a warm DHT table
  instead of 0 peers for minutes. Saved every 30 s and on `stop()`, written
  atomically; loaded in `start()`. Best-effort — a missing/corrupt file just
  means a cold start.

### `catalog.py`
- `Catalog` class: in-memory dict of `CatalogItem` objects, keyed by
  info-hash extracted from the magnet (`_magnet_key`). Backed by a JSON file
  (`catalog.json`), loaded in `__init__`.
- `add(name, magnet, files)` / `has(magnet)` / `items()` / `save()`.
- `sync(feed_url)`: merges a feed (local path or URL) into the catalog,
  returns count of newly added items.
- Feed format (JSON): a list of `{"name", "magnet", "files"?}` objects, or an
  object with an `items`/`torrents` key. Entries may use a `.torrent` URL in
  place of a magnet (the engine accepts URLs too).

### `static/index.html`
- Single file, no build step. ~210 lines of HTML+CSS+JS.
- Left panel: torrent list (name, progress bar, state badge, magnet copy
  button) → clicking a torrent shows its file list.
- Right panel: PDF.js viewer (bundled locally, no CDN).
- Header: magnet input, Sync feed button, status text.
- Drag-and-drop: drop one or more `.torrent` files anywhere in the window →
  POST all of them to `/api/torrent` → refresh. The status bar reports how many
  were added / already present / failed.
- Polling: `refresh()` runs every 4 s (`setInterval`), fetches `/api/catalog`
  and `/api/torrents`, joins them by info-hash (`magnetKey()`), re-renders both
  panels, updates progress bars. `magnetKey(m)` extracts the `xt=` part from a
  magnet.

### `e2e_test.py`
- Single process, self-contained. Torrent creation is hand-rolled
  (`lt.bencode` + `hashlib`); libtorrent 2.x's `create_torrent` bindings are
  deprecated and unreliable here.
- Test A (deterministic, no network): pre-places the synthetic PDF where the
  engine expects it (so it *verifies* rather than downloads), then drives the
  app via FastAPI's `TestClient` — checks `206` (head/tail), `200` (full),
  `416` (invalid range), and catalog add/list.
- Test C (deterministic, no network): partial read. Seeds only the first
  piece of a 3-piece file and proves that a present range is served
  immediately while a missing range blocks and times out — the "read before
  fully downloaded" path.
- Test B (best-effort): a real magnet download across two in-process
  libtorrent sessions (seeder + client engine) on localhost. Prints
  `SKIPPED (no peers ...)` if DHT is blocked — not a failure.
- Success message: `ALL E2E TESTS PASSED`.

### `make_test_torrent.py`
- Creates a `.torrent` file for a given PDF using a local `libtorrent`
  `create_torrent` call. Used by the e2e test and for manual testing.

### `seeder_linux.py` / `seeder_macos.py`
- Standalone scripts that seed a given `.torrent` file. Used to provide
  peers for local testing. The macOS version includes a `dlopen` workaround
  for libtorrent 2.0.11.

## Running & testing

```bash
# Install
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run
python3 -m uvicorn app:app --host 127.0.0.1 --port 8080
# → http://127.0.0.1:8080

# Test
python3 e2e_test.py

# Manual test: create a test torrent, seed it, then add the magnet to the app
python3 make_test_torrent.py test.pdf
python3 seeder_linux.py test.torrent   # terminal 1
# paste magnet into the app UI, or drag test.torrent onto the browser
```

## Constraints

- **No database.** JSON files only.
- **No auth.** 127.0.0.1 only.
- **No build step.** Vanilla JS, no bundler, no framework.
- **No multi-user.** Single person, single machine.
- **Python 3.10–3.13.** `libtorrent` has no 3.14 Windows wheel.
- **`catalog.json` and `downloads/` are gitignored** — runtime state. `feed.json`
  is tracked as a template (empty array); revert local edits before committing.
  `.torrents/` is not gitignored but is empty at rest (only populated after a
  dropped file).

## Legal note

The app is a neutral torrent client. It does not host, index, or distribute
any content. Users are responsible for only downloading content they have
the legal right to access (e.g. public-domain works from the Internet
Archive, Creative Commons films, open-source Linux distros).
