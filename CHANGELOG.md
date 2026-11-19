# Changelog

All notable changes to **Mutuo** are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/).

Mutuo started life as a single-file "PDF torrent reader" and grew into a
desktop reading app. `v0.7.0` is the first tagged release and captures the
app as it stands today; earlier work is folded into it below.

## [Unreleased]

## [0.7.1] - 2026-11-19

### Added
- **OPDS catalog browsing** — add any OPDS feed (e.g. Standard Ebooks,
  ManyBooks) as a catalog and browse its shelves and folders. `opds.py`
  parses both Atom and OPDS 2.0 JSON feeds; books open in the in-site
  readers (PDF, EPUB, text, HTML).
- **Catalog search** — feeds that expose a `rel="search"` link get a real
  search box (the `{searchTerms}` placeholder is filled in client-side);
  results open in the in-site reader.
- **Cross-provider search** — opening a book's detail page in a catalog
  automatically searches your other providers for the same title; the
  results render below the feed and open directly.
- **Continue reading on launch** — the app reopens your last book at the
  saved page (Settings → "Resume on launch", on by default).
- **Global search** — the search box in the Library and Catalogs tabs
  searches the whole library (torrents, direct PDFs, saved texts, EPUBs and
  files) by name/title/author via `GET /api/search`, with an optional
  full-text pass over saved text documents that returns a snippet.
- **Clear all** button in the Recent tab (cached files are kept).
- **Linux & macOS installers** — a GitHub Actions workflow
  (`.github/workflows/release.yml`) now builds all three platforms and
  attaches them to the release on a `v*` tag. `build_bundle.sh` packages the
  onedir bundle (`.tar.gz` on Linux, `.app` zip on macOS).
- **Startup update check** — on launch the app quietly checks the latest
  GitHub release and offers to open it if a newer version exists.
- **Plugin authoring guide** — `plugins/README.md` documents the full plugin
  API (entry points, result items, `CONFIG`, `covers()`, `resolve()`).

### Changed
- **OPDS feed parsing hardened** — XHTML-wrapped titles, feed-typed
  navigation links, and image-typed covers are now classified correctly;
  covers come from `<opds:image>` or a thumbnail link.
- **Catalog covers** — grid cards show covers, with a placeholder fallback
  for 403/hanging cover URLs.
- `desktop.py` is now cross-platform: the data dir is
  `~/Library/Application Support/Mutuo` on macOS and `~/.local/share/Mutuo`
  on Linux, and stale-port cleanup uses `lsof` on POSIX.

## [0.7.0] - 2026-11-16

The first tagged release. Everything below shipped together as **Mutuo**, the
renamed PDF torrent reader.

### Added

#### Core reader
- FastAPI app that streams PDFs directly from a BitTorrent download, with a
  PDF.js viewer driven by HTTP Range requests.
- JSON-file-backed torrent catalog and a `feed.json` magnet list.
- Deterministic e2e test for reading a partially-downloaded PDF.

#### Desktop app & packaging
- **Desktop shell** — a `pywebview` window wrapping the web UI, with local
  per-user **profiles** (each profile has its own data folder).
- **Windows installer** — Inno Setup package plus a profile settings UI.
- **Renamed to Mutuo** — from "PDF Torrent Reader"; data dir migrates
  automatically from the old location.
- **License** added.

#### Built-in plugins & providers
- **Live plugin providers** — plugins with a `search()` get their own
  searchable sidebar tab; results appear as you type.
- **Gutenberg** — live search of Project Gutenberg.
- **arXiv**, **Google Books**, and **OpenAlex** built-in plugins.
- **Local-provider metadata filters** for the built-in feeds.

#### Reading
- **In-site readers** for plain text and HTML, plus a **Reading view**
  (To Read / Reading / Complete) and **Favorites**.
- **My Shelf** — a reading view that remembers your position in each book.
- **In-site EPUB reading**, local text saving, and a text-reader toolbar.
- **In-text search** in the plain-text reader.
- **Catalogue grid view**, per-state list settings, and shelf covers.

#### Torrents & downloads
- **Drag-and-drop** `.torrent` files (multiple at once).
- **Torrent list** as buttons with a loading page and live swarm stats.
- **Per-torrent actions** and **per-file include/exclude**.
- **Opt-in downloads** and per-file delete (keep the torrent).
- **Online PDF links** (e.g. arXiv) in the top link field.
- **Download-for-later** on live search results, with a saved badge and an
  open-folder button on downloaded PDFs.

#### Settings & UI
- **App theming**, size controls, and settings polish.
- **Tab position** setting, close-book button, **PDF metadata editing**, and
  **plugin management**.
- Providers/Plugins toggle moved to the top bar; Plugins promoted to a main
  rail tab.

### Changed
- **Bundle trimmed** — only built-in (git-tracked) plugins are bundled; the
  PDF.js bundle drops source maps, the demo PDF, and unused locales; unused
  PIL AVIF/WebP codecs are removed. Installer shrank from ~29.6 MB to
  ~25.8 MB.
- **README** rewritten to be more inviting, with an installation tutorial.

### Fixed
- 500 error on `.torrent` drop (coerce libtorrent `bytes`/`announce_entry`).
- Mojibake — text files now read as UTF-8, not the OS default encoding.
- Unclickable file links (event delegation instead of per-link `onclick`).
- Stale UI caching — no-cache headers on `index.html`.
- Shelf row/control overlap for short titles.
- Live-list flicker; Gutendex timeouts now bounded.
- Replaced deprecated `@app.on_event` with FastAPI lifespan.

### Removed
- The DCM plugin.

[Unreleased]: https://github.com/giangamberi/mutuo/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/giangamberi/mutuo/releases/tag/v0.7.0
