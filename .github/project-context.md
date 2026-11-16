# Mutuo — Project Context

## What it is

**Mutuo** is a single-user desktop app that lets you **read PDFs directly
from a BitTorrent download** without waiting for the file to fully save to
disk. It pairs a `libtorrent` download engine with a FastAPI HTTP layer that
streams partial files to PDF.js, wrapped in a native `pywebview` window with
local **profiles**.

Use case: you find a PDF on a torrent (e.g. a public-domain book from the
Internet Archive), drop the `.torrent` file or paste a magnet link, and the
PDF becomes readable as soon as the metadata and enough pieces arrive. It
also reads plain text, EPUB, and web pages, and has built-in search plugins
(Gutenberg, arXiv, Google Books, OpenAlex).

## Who it's for

One person, on one machine. No auth, no multi-user support, no cloud
backend. The server binds to `127.0.0.1` and trusts the local user.

## Tech stack

| Layer | Technology |
|-------|-----------|
| Front end | Vanilla HTML/CSS/JS, PDF.js 3.x (bundled in `static/pdfjs-dist/`) |
| Desktop shell | pywebview (system WebView2 / WebKit) |
| API | Python 3.10–3.13, FastAPI, uvicorn |
| Download | libtorrent 2.x (C++ core, Python bindings) |
| Persistence | Plain JSON files, scoped per profile |
| Testing | `e2e_test.py` — self-contained, no external deps |

## How it works (data flow)

```
desktop.py  ── profile picker → spawns uvicorn scoped to profiles/<name>/
        │
        ▼
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
   0.25 s. It's started in the FastAPI lifespan startup hook and stopped in
   the shutdown hook. The thread is not joined on exit (the process just
   dies).

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

7. **Profiles scope all mutable data.** `desktop.py` sets `PTR_PROFILE` and
   `PTR_DATA_DIR`; `app.py` and `profiles.py` read them at import time, so
   every registry and content dir lives under `profiles/<name>/`. The
   read-only app files (`static/`, `plugins/`) stay shared at the root.

## File-by-file notes

### `app.py`
- FastAPI app. All mutable data is scoped to the active profile
  (`profiles/<name>/`, selected via the `PTR_PROFILE` env var; defaults to
  `"default"`). On first run it folds legacy root-level data into the default
  profile (`profiles.migrate_root_data()`).
- Per-profile registries: `catalog.json`, `providers.json`, `pdfs.json`,
  `texts.json`, `epubs.json`, `files.json`, `favorites.json`, `shelf.json`,
  `recent.json`, `plugin_state.json`, `feed.json`.
- REST endpoints (abridged):
  - `GET /` — serves `static/index.html`
  - `GET/POST /api/catalog` — list / add a magnet or `.torrent` URL
  - `POST /api/torrent` — upload one or more `.torrent` files (multipart,
    field `files`); response reports `added` / `skipped` / `errors`
  - `POST /api/sync` — re-merge `feed.json` into the catalog
  - `GET /api/torrents` — engine state snapshots
  - `GET /api/torrent?ih=..` — one torrent's snapshot (stats + file list)
  - `POST /api/torrent/{ih}/{action}` — `pause` | `resume` | `recheck`
  - `DELETE /api/torrent/{ih}` — body `{"delete_files": bool}`
  - `POST /api/torrent/{ih}/file-priority` — body `{"index", "priority"}`
  - `DELETE /api/torrent/{ih}/file/{index}` — delete one file, keep torrent
  - `GET /api/pdf?ih=..&f=..` — HTTP Range streaming for PDF.js
  - plus the readers (text/EPUB/HTML proxies), the plugin API, and the
    profile-scoped registries (shelf, favorites, saved texts/EPUBs/files)
- `GET /` and `POST /api/catalog` are the main entry points; the drag-and-drop
  path uses `POST /api/torrent`.

### `torrent_engine.py`
- `TorrentEngine` class: wraps a `libtorrent.session` in a daemon thread
  (`_run` loop, not joined on exit).
- `TorrentState` dataclass: `info_hash` (hex, dict key), `name`, `magnet`,
  `save_path`, `ready` (metadata received), `fs` (`lt.file_storage` or None),
  `progress`, `state`, `num_peers`, `total_size`, `total_done`,
  `file_progress` (per-file bytes done).
- `add_torrent(source, magnet="")` accepts a magnet URI, a `.torrent` file
  path, or a `.torrent` URL. Returns the info-hash hex. For magnets the file
  list is empty until metadata arrives via DHT; for files/URLs it's immediate.
- **Opt-in downloads:** every torrent is added with
  `default_dont_download | auto_managed`, so metadata is fetched (the file
  list shows) but *no file downloads until the user includes one*. Including
  a file = `set_file_priority(ih, idx, 4)`; excluding = priority 0. A torrent
  with no included files reports state `idle` (not `downloading`).
- **Pending includes** (`_pending_include`): `/api/pdf` calls
  `set_file_priority` to include the file before waiting. On a *magnet* the
  metadata may not have arrived yet (fs is None), so an include is queued and
  applied the moment `has_metadata` flips. Without this, opening a magnet's
  PDF pre-metadata would block until the read timeout.
- `delete_file(ih, idx)`: deletes one file from disk + sets its priority to 0,
  but keeps the torrent in the session. `torrent_handle.delete_files(idx)` is
  NOT bound in this libtorrent build, so the on-disk file is removed with
  `os.remove` at `save_path / fs.file_path(idx)`.
- `status(include_files=True)`: thread-safe snapshot list of dicts matching
  the front end's expectations (includes `files` with index/name/size/done).
- `file_size(ih, idx)`, `wait_for_range(ih, idx, start, length, timeout)`,
  `fetch_range(ih, idx, start, length)`: the three methods the `/api/pdf`
  endpoint uses to stream a range only after its pieces are available.
- `pause(ih)`, `resume(ih)`, `force_recheck(ih)`: per-torrent actions.
- `remove_torrent(ih, delete_files=False)`: drop from session; with
  `delete_files=True` the per-torrent data dir is also `rmtree`'d.
- `set_file_priority(ih, idx, prio)`: this 2.x binding's `prioritize_files`
  takes a single index-aligned list (NOT a `(indices, prio)` pair), so the
  method reads `file_priorities()`, patches one index, and writes back.
- `_state_str`: `paused` is checked before `seeding` so a paused, complete
  torrent reads "paused", not "seed".
- `_load_state()` / `_save_state()`: persist the libtorrent session blob to
  `downloads/.session_state.json` so a restart resumes with a warm DHT table.
  Saved every 30 s and on `stop()`, written atomically; loaded in `start()`.

### `catalog.py`
- `Catalog` class: in-memory dict of `CatalogItem` objects, keyed by
  info-hash extracted from the magnet (`_magnet_key`). Backed by a JSON file
  (`catalog.json`), loaded in `__init__`.
- `add(name, magnet, files)` / `has(magnet)` / `items()` / `save()`.
- `sync(feed_url)`: merges a feed (local path or URL) into the catalog,
  returns count of newly added items.
- `ProviderCatalog` (`providers.json`): named torrent lists, each with its own
  feed. The UI shows one tab per provider; the "Local" tab is the built-in
  catalog.
- Feed format (JSON): a list of `{"name", "magnet", "files"?}` objects, or an
  object with an `items`/`torrents` key. Entries may use a `.torrent` URL in
  place of a magnet.

### `profiles.py`
- `ProfileStore`: registry of local profiles (`profiles.json`), a list of
  `{name, has_password, salt?, hash?, created}`.
- A profile is a named, self-contained set of app data under
  `profiles/<name>/` (the JSON registries + content dirs). `plugins/` and
  `static/` stay shared at the root.
- Optional per-profile password: PBKDF2-HMAC-SHA256, 200k iterations,
  per-profile salt. A convenience lock, not a security boundary.
- `DATA_DIR` comes from `PTR_DATA_DIR` (set by `desktop.py` in the frozen
  app); in a dev checkout it's the repo root.

### `desktop.py`
- The native shell (pywebview / system WebView2). Shows a profile picker
  (`static/profiles.html`) on launch, spawns the FastAPI server scoped to
  `profiles/<name>/` (re-spawning the same exe with `--server <profile>` when
  frozen), and navigates the same window to the app.
- Sets `PTR_DATA_DIR` (per-user data dir) and `PTR_PROFILE` before importing
  `profiles`/`app`.
- On startup, a background thread checks the latest GitHub release and offers
  to open it if a newer version exists (silent no-op on failure, e.g. a
  private repo).

### `static/index.html`
- Single file, no build step. A rail (Shelf / Favorites / My Shelf / Plugins
  / Settings), a provider tab bar + file browser, and the readers (PDF.js,
  text, EPUB, HTML).
- Drag-and-drop: drop one or more `.torrent` files anywhere in the window →
  POST all of them to `POST /api/torrent` → refresh.
- Polling: `refresh()` runs every 4 s (`setInterval`), fetches
  `/api/catalog` and `/api/torrents`, joins them by info-hash (`magnetKey()`),
  re-renders both panels, updates progress bars. `magnetKey(m)` extracts the
  `xt=` part from a magnet.

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
  immediately while a missing range blocks and times out.
- Test B (best-effort): a real magnet download across two in-process
  libtorrent sessions (seeder + client engine) on localhost. Prints
  `SKIPPED (no peers ...)` if DHT is blocked — not a failure.
- Success message: `ALL E2E TESTS PASSED`.

## Running & testing

```bash
# Install
python -m venv .venv && .venv\Scripts\activate   # Windows
# source .venv/bin/activate                       # macOS / Linux
pip install -r requirements.txt

# Run (desktop shell — shows the profile picker)
python desktop.py

# Or run the server directly (plain browser tab)
python -m uvicorn app:app --host 127.0.0.1 --port 8080
# → http://127.0.0.1:8080

# Test
python e2e_test.py
```

## Constraints

- **No database.** JSON files only.
- **No auth.** 127.0.0.1 only.
- **No build step.** Vanilla JS, no bundler, no framework.
- **No multi-user.** Single person, single machine.
- **Python 3.10–3.13.** `libtorrent` has no 3.14 Windows wheel.
- **`catalog.json`, `profiles/`, and `downloads/` are gitignored** — runtime
  state. `feed.json` is tracked as a template (empty array); revert local
  edits before committing.

## Legal note

The app is a neutral torrent client. It does not host, index, or distribute
any content. Users are responsible for only downloading content they have
the legal right to access (e.g. public-domain works from the Internet
Archive, Creative-Commons films, open-source Linux distros).
