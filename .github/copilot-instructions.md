# Mutuo — Copilot Instructions

You are working on **Mutuo**, a single-user desktop app that reads PDFs
streamed directly from a BitTorrent download. No database. It is wrapped in a
`pywebview` window with local **profiles** and binds to `127.0.0.1`. A login /
multi-user layer is **planned but not yet implemented** — see "Planned: login
& multi-user" below before touching anything auth-related.

## Architecture (read first)

| File | Role |
|------|------|
| `app.py` | FastAPI entry point. REST API + static front end + PDF Range streaming. All mutable data is scoped to the active profile. |
| `desktop.py` | pywebview shell. Profile picker, spawns the server scoped to a profile, startup update check. |
| `profiles.py` | Local profiles: registry (`profiles.json`) + per-profile data dirs. Optional PBKDF2 password per profile. |
| `torrent_engine.py` | Background thread wrapping a `libtorrent.session`. In-memory download state. |
| `catalog.py` | JSON-file-backed torrent catalog (`catalog.json`) + providers (`providers.json`). Source of truth for *what* to track. |
| `feed.json` | Input list of magnet URIs to sync into the catalog (per profile). |
| `catalog.json` | Persisted catalog. Created/updated at runtime — do not hand-edit. |
| `static/index.html` | Single-page front end (rail, provider tabs, file list, PDF.js/text/EPUB/HTML readers, drag-and-drop, plugins tab). |
| `static/profiles.html` | The profile picker shown at launch. |
| `static/pdfjs-dist/` | Bundled PDF.js 3.x (minified). Served locally, no CDN dependency. |
| `plugin_loader.py` | Discovers/runs plugins (`.py` files in `plugins/`). Loaded on demand, never at startup. |
| `plugins/` | User plugins. Each is a `.py` file defining `search(query, config)` and/or `fetch(query)`. |
| `plugin_state.json` | Per-plugin active flag + config. Runtime state — do not hand-edit. |
| `version.py` | Single source of truth for the app version (`__version__`). |
| `e2e_test.py` | Self-contained test. A + C = deterministic HTTP range checks. B = cross-session DHT download (best-effort, may skip). |
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
- **Bind to `127.0.0.1` only.** The app is single-user. Multi-user support is
  planned (see "Planned: login & multi-user") but not built — do not start
  implementing it unless explicitly asked.
- **Profiles scope all mutable data.** `app.py` and `profiles.py` read
  `PTR_PROFILE` / `PTR_DATA_DIR` at import time. New per-user state belongs
  under `profiles/<name>/`, not the repo root.

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

An item may also carry a `"meta"` string shown as a subtitle line, a `"kind"`
(`pdf`/`html`/`text`/`epub`), or a `"resolve_id"` for two-stage sources. A
plugin may define `CONFIG = [ {"key","label","type","default","options"}, ... ]`
to expose settings (e.g. sort order, language, topic) that the UI renders and
passes to `search()` as the `config` dict. `type` is `"select"` (with
`options`) or `"text"`.

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
   and the `status()` snapshot includes the new field.
4. If it touches the front end, edit `static/index.html` directly. The
   `refresh()` function is the single polling loop and the place where catalog
   entries are joined to engine state — add new DOM updates there.
5. Run `python e2e_test.py` (Test A at minimum) before committing.

## Testing

```bash
# Deterministic HTTP range + catalog tests (no network needed):
python e2e_test.py
```

All tests run in a single process. Test A pre-places the synthetic PDF where
the engine expects it (so it *verifies* rather than downloads) and drives the
app via FastAPI's `TestClient`, checking 206/200/416 and the catalog
endpoints. Test C proves a present range is served immediately while a missing
range blocks and times out. Test B does a real magnet download across two
in-process libtorrent sessions (a seeder + a client engine) on localhost;
it's best-effort and prints `SKIPPED (no peers ...)` if DHT is blocked — that
is not a failure. On success it prints `ALL E2E TESTS PASSED`.

## Running

```bash
# Desktop shell (profile picker + native window):
python desktop.py

# Or the server directly (plain browser tab):
python -m uvicorn app:app --host 127.0.0.1 --port 8080
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
- Do not commit `catalog.json`, `plugin_state.json`, `profiles/`, or contents
  of `.torrents/` — they are runtime state. `catalog.json`, `plugin_state.json`,
  and `profiles/` are gitignored; `.torrents/` is not in `.gitignore` but is
  empty at rest (only populated after a file drop). `feed.json` IS tracked —
  it's a template (empty list). If you add magnets to it for local testing,
  revert before committing.

## File structure

```
mutuo/
├── .github/
│   ├── copilot-instructions.md   ← you are here
│   ├── project-context.md
│   └── workflows/release.yml     # multi-platform build + release
├── .torrents/                    # runtime (not gitignored, empty at rest)
├── static/
│   ├── index.html                # front end (single file, no build)
│   ├── profiles.html             # profile picker
│   └── pdfjs-dist/               # bundled PDF.js 3.x
├── app.py                        # FastAPI + REST API + PDF streaming
├── desktop.py                    # pywebview shell + profile picker + update check
├── profiles.py                   # local profiles (registry + per-profile data)
├── torrent_engine.py             # libtorrent session in a thread
├── catalog.py                    # JSON catalog + providers (persistence)
├── plugin_loader.py              # plugin discovery + runner
├── plugins/                      # user plugins (search(query, config) / fetch(query))
│   ├── README.md                 # plugin authoring guide
│   ├── gutenberg.py              # live search of Project Gutenberg
│   ├── arxiv.py
│   ├── google_books.py
│   └── openalex.py
├── plugin_state.json             # runtime: per-plugin active flag + config
├── feed.json                     # input magnet list (template)
├── catalog.json                  # runtime catalog (gitignored)
├── version.py                    # __version__ (single source of truth)
├── e2e_test.py                   # self-contained test
├── Mutuo.spec                    # PyInstaller spec (onedir)
├── build_installer.ps1           # Windows installer build
├── build_bundle.sh               # Linux/macOS bundle build
├── installer/Mutuo.iss           # Inno Setup script
├── requirements.txt
└── README.md
```
