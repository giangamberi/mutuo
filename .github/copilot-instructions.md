# PDF Torrent Reader — Copilot Instructions

You are working on a small web app that reads PDFs streamed directly from a
BitTorrent download. No database. It is currently single-user and bound to
localhost; a login / multi-user layer is **planned but not yet implemented** —
see "Planned: login & multi-user" below before touching anything auth-related.

## Architecture (read first)

| File | Role |
|------|------|
| `app.py` | FastAPI entry point. REST API + static front end + PDF Range streaming. |
| `torrent_engine.py` | Background thread wrapping a `libtorrent.session`. In-memory download state. |
| `catalog.py` | JSON-file-backed torrent catalog (`catalog.json`). Source of truth for *what* to track. |
| `feed.json` | Input list of magnet URIs to sync into the catalog. |
| `catalog.json` | Persisted catalog. Created/updated at runtime — do not hand-edit. |
| `static/index.html` | Single-page front end (torrent list, file list, PDF.js viewer, drag-and-drop, plugins tab). |
| `static/pdfjs-dist/build/` | Bundled PDF.js 3.x (minified). Served locally, no CDN dependency. |
| `plugin_loader.py` | Discovers/runs plugins (`.py` files in `plugins/`). Loaded on demand, never at startup. |
| `plugins/` | User plugins. Each is a `.py` file defining `search(query, config)` and/or `fetch(query)`. |
| `plugin_state.json` | Per-plugin active flag + config. Runtime state — do not hand-edit. |
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
- **Bind to `127.0.0.1` only.** The app is single-user today. Multi-user
  support is planned (see "Planned: login & multi-user") but not built — do
  not start implementing it unless explicitly asked.

## Plugins (custom behaviours)

A **plugin** is a `.py` file in `plugins/`. It is the extension point for
behaviours that don't fit a static provider feed. A plugin can define either or
both of:

- `search(query, config) -> list[dict]` — a **live** search. Makes the plugin an
  *active provider*: when toggled active it appears as a searchable provider tab
  (the search box + the plugin's config controls live in the search bar, and
  results are fetched on demand).
- `fetch(query) -> list[dict]` — a **one-shot** run (the "▶ Run" button).

Each returned item is one of:

- `{"name", "url"}` — a PDF to open (downloaded on click, deduped by URL)
- `{"name", "magnet"}` — a torrent to track (added to the catalog + session)
- `{"name", "path"}` — a local PDF to read in place

An item may also carry a `"meta"` string shown as a subtitle line. A plugin may
define `CONFIG = [ {"key","label","type","default","options"}, ... ]` to expose
settings (e.g. sort order, language, topic) that the UI renders and passes to
`search()` as the `config` dict. `type` is `"select"` (with `options`) or
`"text"`.

How it fits the existing layers:
- **Active toggle:** the `🔌 Plugins` tab has an **Active** checkbox per plugin
  (`POST /api/plugins/{pid}/active`). An active plugin is injected into
  `/api/providers` as a live provider tab (id `plugin:<id>`, `live: true`).
  Toggling it off removes the tab.
- **Config:** `POST /api/plugins/{pid}/config` saves the plugin's settings
  (persisted in `plugin_state.json` alongside the active flag). The live tab's
  search bar renders the `CONFIG` controls; changing one re-runs the search and
  persists the value.
- **Live search:** `GET /api/plugins/{pid}/search?query=…&config=<json>` runs
  `search(query, config)` and returns the items. Clicking a result calls
  `POST /api/plugins/{pid}/open` (downloads the PDF by URL, deduped) and opens
  it in the viewer.
- **One-shot run:** `POST /api/plugins/{pid}/run` runs `fetch(query)` and tags
  every result with provider `"plugin:<id>"` (torrents → catalog; PDFs →
  downloaded). Re-downloading the same URL is a no-op (deduped by URL).
- **Persistence:** plugin *files* live in `plugins/` (tracked). The active flag
  + config live in `plugin_state.json`. One-shot results live in `catalog.json`
  / `pdfs.json` like anything else. "Clear" (`DELETE /api/plugins/{pid}/items`)
  drops a plugin's loaded items.
- **Safety:** plugins are loaded on demand via `importlib` (never imported at
  startup) and new plugin code is `compile()`-checked before saving, so a broken
  plugin can't take the app down. Metadata (NAME/DESCRIPTION/CONFIG, and whether
  `search`/`fetch` exist) is read from source via AST, so the list view works
  even for a plugin that would error at import. A plugin's functions run
  in-process — treat plugin code as trusted (single-user app).

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

## Planned: login & multi-user (NOT implemented yet)

A login system is planned so the app can be shared company-wide: each person
logs in from any device and their saved **providers** (and later **plugins**)
load for them. It is intentionally **not** built yet — this section is the
agreed design so it can be picked up later without re-deciding everything.
Do not implement it unless explicitly asked.

- **Per-user:** only the provider list (and future plugins, same pattern).
  Each user sees and edits their own providers.
- **Shared:** the PDF catalog, downloads, and the torrent engine stay shared —
  one download pool for the whole company. A torrent anyone adds is available
  to everyone.
- **No access tracking:** do not log or record what anyone opens or accesses.
- **Real security (non-negotiable):**
  - Passwords hashed with PBKDF2-HMAC-SHA256 (≥200k iterations, per-user salt)
    or argon2 — never stored in plaintext.
  - Server-side sessions: random 64-hex-char token, 30-day TTL, delivered as an
    `HttpOnly`, `SameSite=Lax` cookie. All `/api/*` endpoints gated behind a
    valid session (login/register exempt).
  - A `static/login.html` page + a user chip with logout in the header; the
    front end redirects to `/login` on 401.
- **Staging:**
  - Stage 1 — keep binding to `127.0.0.1`; verify the whole flow locally.
  - Stage 2 — expose over the network via **Tailscale/WireGuard** (preferred)
    or a reverse proxy with HTTPS. Requires the user's infra choice first.
- **Reference:** a working `auth.py` (UserStore + SessionStore) plus the
  per-user provider wiring in `app.py`/`catalog.py` was prototyped and then
  reverted to keep the app in its single-user state. The design above captures
  what that prototype did; rebuild from this spec rather than from memory.

## Do not

- Do not add a database, ORM, or migration layer.
- Do not implement the login / multi-user system unless explicitly asked —
  it is planned (see above) but deferred.
- Do not switch the front end to a build tool or framework.
- Do not change the bind address from `127.0.0.1`.
- Do not commit `catalog.json`, `plugin_state.json`, or contents of `.torrents/`
  — they are runtime state. `catalog.json` and `plugin_state.json` are
  gitignored; `.torrents/` is not in `.gitignore` but is empty at rest (only
  populated after a file drop). `feed.json` IS tracked — it's a template (empty
  list). If you add magnets to it for local testing, revert before committing.

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
├── plugin_loader.py              # plugin discovery + runner
├── plugins/                      # user plugins (search(query, config) / fetch(query))
│   └── gutenberg.py              # example: live search of Project Gutenberg
├── plugin_state.json             # runtime: per-plugin active flag + config
├── feed.json                     # input magnet list
├── catalog.json                  # runtime catalog (gitignored)
├── make_test_torrent.py          # test torrent generator
├── seeder_linux.py               # test seeder (Linux)
├── seeder_macos.py               # test seeder (macOS)
├── e2e_test.py                   # self-contained test
├── requirements.txt
└── README.md
```
