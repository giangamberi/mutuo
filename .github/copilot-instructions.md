# PDF Torrent Reader — Copilot Instructions

You are working on a small, single-user web app that reads PDFs streamed directly
from a BitTorrent download. No database, no auth — one person, one machine,
localhost only.

## Architecture (read first)

| File | Role |
|------|------|
| `app.py` | FastAPI entry point. REST API + static front end + PDF Range streaming. |
| `torrent_engine.py` | Background thread wrapping a `libtorrent.session`. In-memory download state. |
| `catalog.py` | JSON-file-backed torrent catalog (`catalog.json`). Source of truth for *what* to track. |
| `feed.json` | Input list of magnet URIs to sync into the catalog. |
| `catalog.json` | Persisted catalog. Created/updated at runtime — do not hand-edit. |
| `static/index.html` | Single-page front end (torrent list, file list, PDF.js viewer, drag-and-drop). |
| `static/pdfjs-dist/build/` | Bundled PDF.js 3.x (minified). Served locally, no CDN dependency. |
| `e2e_test.py` | Self-contained test. Part A = deterministic HTTP range checks. Part B = cross-process DHT download (best-effort, may skip). |
| `make_test_torrent.py` | Creates a `.torrent` file for a given PDF so you can seed locally. |
| `seeder_linux.py` / `seeder_macos.py` | Standalone seeders for testing (Linux / macOS). |
| `.torrents/` | Runtime directory. Stores copies of dropped `.torrent` files (auto-created). |

## Key invariants

- **Catalog is the persistence layer.** The engine is in-memory; on restart the
  catalog is re-read and every entry re-added to the session. If you add a new
  way to track a torrent, make sure it lands in `catalog.json` or it will be
  lost on restart.
- **Magnet URI is the key.** Catalog keys and engine state are matched by
  magnet string. The front end does this join inside `refresh()` via a
  `byMagnet` map keyed by `magnetKey()` (extracts the `xt=` info-hash). If
  you change how magnets are generated or stored, update both sides.
- **PDFs are served via HTTP Range requests.** The `GET /api/pdf?ih=<info_hash>&f=<file_index>`
  endpoint handles `Range` headers manually (PDF.js depends on this). Do not
  replace it with `FileResponse` unless you verify PDF.js still works.
- **Bind to `127.0.0.1` only.** No auth by design. If someone asks to add
  multi-user support, that's a different project — flag it, don't implement it.

## Code style

- Python: no type-checker config, no linter config. Match the existing style:
  module-level singletons (`engine`, `catalog`), small dataclasses,
  `JSONResponse` for all API responses.
- Front end: vanilla JS in a single `<script>` block. No build step, no
  framework. CSS is inline in `<style>`.
- Comments: explain *why*, not *what*. The existing comments are terse and
  focused on non-obvious libtorrent behaviour.

## How to add a feature

1. Identify which layer touches it: catalog (persistence), engine (download
   state), app (HTTP / API), or front end (UI).
2. If it changes the catalog schema, update `catalog.py` (`add()`/`items()`/
   `sync()`) and the API endpoints that read it.
3. If it changes the engine, make sure the `TorrentState` dataclass is updated
   and `torrents()` snapshot includes the new field.
4. If it touches the front end, edit `static/index.html` directly. The
   `refresh()` function is the single polling loop and the place where catalog
   entries are joined to engine state — add new DOM updates there.
5. Run `python3 e2e_test.py` (Part A at minimum) before committing.

## Testing

```bash
# Deterministic HTTP range + catalog tests (no network needed):
python3 e2e_test.py

# Full e2e including real DHT download (needs DHT access, may skip):
python3 e2e_test.py
```

Both run in a single process. Test A pre-places the synthetic PDF where the
engine expects it (so it *verifies* rather than downloads) and drives the app
via FastAPI's `TestClient`, checking 206/200/416 and the catalog endpoints.
Test B does a real magnet download across two in-process libtorrent sessions
(a seeder + a client engine) on localhost; it's best-effort and prints
`SKIPPED (no peers ...)` if DHT is blocked — that is not a failure. On success
it prints `ALL E2E TESTS PASSED`.

## Running

```bash
python3 -m uvicorn app:app --host 127.0.0.1 --port 8080
# → http://127.0.0.1:8080
```

Dependencies: `pip install -r requirements.txt` (needs Python 3.10–3.13;
`libtorrent` has no 3.14 Windows wheel).

## Do not

- Do not add a database, ORM, or migration layer.
- Do not add authentication or multi-user features.
- Do not switch the front end to a build tool or framework.
- Do not change the bind address from `127.0.0.1`.
- Do not commit `catalog.json` or contents of `.torrents/` — they are
  runtime state. `catalog.json` is gitignored; `.torrents/` is not in
  `.gitignore` but is empty at rest (only populated after a file drop).
  `feed.json` IS tracked — it's a template (empty list). If you add magnets
  to it for local testing, revert before committing.

## File structure

```
pdf-torrent-reader/
├── .github/
│   ├── copilot-instructions.md   ← you are here
│   └── project-context.md
├── .torrents/                    # runtime (not gitignored, empty at rest)
├── static/
│   └── index.html                # front end (single file, no build)
├── app.py                        # FastAPI + REST API + PDF streaming
├── torrent_engine.py             # libtorrent session in a thread
├── catalog.py                    # JSON catalog (persistence)
├── feed.json                     # input magnet list
├── catalog.json                  # runtime catalog (gitignored)
├── make_test_torrent.py          # test torrent generator
├── seeder_linux.py               # test seeder (Linux)
├── seeder_macos.py               # test seeder (macOS)
├── e2e_test.py                   # self-contained test
├── requirements.txt
└── README.md
```
