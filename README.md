# PDF Torrent Reader

A local, single-user web app that reads PDFs straight out of BitTorrent.
You give it a list of torrents (your own files, or internal company shares);
it downloads the pieces the reader actually needs and streams them to PDF.js
over HTTP Range requests — no full-file download required for first paint.

Intended for **content you are authorized to share** (your own PDFs, internal
company documents). It is a client for a swarm you control; it is not a
distribution service for third-party copyrighted material.

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
  from disk and returns a `206`. Everything else PDF.js needs (`200`, `416`,
  `Accept-Ranges`) is handled too.
- **`catalog.py`** is the list of PDFs — persisted to `catalog.json`, synced
  from an optional `feed.json` (a JSON list you point at an internal source).
- **`static/index.html`** is a single-page UI: a torrent/file browser on the
  left, the PDF.js viewer on the right.

## Install

```bash
pip install libtorrent fastapi uvicorn httpx   # libtorrent pulls the C++ core
```

## Run

```bash
cd pdf-torrent-reader
python3 -m uvicorn app:app --host 127.0.0.1 --port 8080
# open http://127.0.0.1:8080
```

Binds to localhost by default (single-user, no auth). Add PDFs by pasting a
`magnet:?xt=urn:btih:...` URI in the header, or by putting entries in
`feed.json` and clicking **Sync feed**.

### feed.json format

```json
[
  {"name": "Acme Q3 report", "magnet": "magnet:?xt=urn:btih:...", "files": ["report.pdf"]},
  {"name": "Handbook",      "magnet": "magnet:?xt=urn:btih:..."}
]
```

`files` is optional — when omitted, every PDF in the torrent is offered.
The path may also be a URL; `POST /api/sync` fetches and merges it.

## Test

```bash
python3 e2e_test.py
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
- **No auth / no multi-tenancy.** Deliberate: it's one user on one machine.
  If you ever point it at a network, add auth and treat it as a *distributor*
  (that materially changes the legal posture).
- **libtorrent 2.x binding is quirky.** Several methods you'd expect
  (`handle.file_storage()`, `file_slice()`, `piece_passed()`,
  `update_piece_priorities`) don't exist in the Python bindings; see the API
  notes at the top of `torrent_engine.py` for the working equivalents.
- **NAT/outbound UDP.** Magnet metadata and DHT discovery need outbound UDP
  (or reachable trackers/peers). Behind a restrictive NAT the metadata fetch
  can be slow; adding known peers/trackers to the magnet helps.
- **Disk.** Verified files accumulate under `downloads/`. Delete old
  info-hash dirs to reclaim space.
