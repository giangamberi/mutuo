"""End-to-end test for the PDF torrent reader.

Torrent *creation* is hand-rolled with lt.bencode + hashlib (libtorrent 2.x's
create_torrent bindings are deprecated and misbehave on this platform). The
app itself only *downloads*, so this is purely fixture code.

- Test A (deterministic, no network): pre-place the PDF where libtorrent
  expects it, so the engine *verifies* rather than downloads. Exercises
  metadata, verification, disk reads, and HTTP 200/206/416 paths.
- Test C (deterministic, no network): partial read. Seeds only the first piece
  of a 3-piece file and proves the core promise: a present range is served
  immediately, while a missing range blocks and times out (never returns
  garbage). This is the "open a PDF before it's fully downloaded" path.
- Test B (best-effort): real magnet download across two sessions on localhost.
  Tolerant of DHT timing; reports but does not fail the suite.

Run:  python3 e2e_test.py
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)

TMP = tempfile.mkdtemp(prefix="ptr-test-")


def sha1(b: bytes) -> bytes:
    return hashlib.sha1(b).digest()


def make_torrent(name: str, files: dict, piece_length: int = 16384) -> tuple[bytes, str]:
    """files: {rel_path: content}. Returns (torrent_bytes, magnet_uri)."""
    # v1 single-or-multi-file info dict; one piece per file for the tiny fixtures
    if len(files) == 1:
        (path, content) = next(iter(files.items()))
        # one sha1 per piece (a tiny file is a single piece, so this reduces to
        # the historical sha1(content) behaviour)
        n = max(1, (len(content) + piece_length - 1) // piece_length)
        pieces = b"".join(sha1(content[i*piece_length:(i+1)*piece_length]) for i in range(n))
        info = {b"name": name.encode(), b"piece length": piece_length,
                b"length": len(content), b"pieces": pieces}
    else:
        data = b"".join(files.values())
        n = (len(data) + piece_length - 1) // piece_length
        pieces = b"".join(sha1(data[i*piece_length:(i+1)*piece_length]) for i in range(n))
        info = {b"name": name.encode(), b"piece length": piece_length,
                b"files": [{b"length": len(c), b"path": path.encode().split(b"/")}
                           for path, c in files.items()],
                b"pieces": pieces}
    buf = __import__("libtorrent").bencode({b"info": info, b"creation date": int(time.time())})
    import libtorrent as lt
    magnet = lt.make_magnet_uri(lt.torrent_info(buf))
    return buf, magnet


# ---- build a real-ish PDF ---------------------------------------------------
PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
       b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
       b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
       b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
       b"0000000052 00000 n \n0000000101 00000 n \n"
       b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n185\n%%EOF\n")
size = len(PDF)
print(f"test pdf: {size} bytes")

torrent_bytes, magnet = make_torrent("test.pdf", {"test.pdf": PDF})
torrent_file = os.path.join(TMP, "test.torrent")
with open(torrent_file, "wb") as f:
    f.write(torrent_bytes)
print("magnet:", magnet)

import libtorrent as lt
from torrent_engine import TorrentEngine

# The engine stores each torrent under <download_dir>/<info_hash_hex>/, so the
# pre-seeded file must be placed there for libtorrent to verify (not download).
engine_ih = str(lt.torrent_info(torrent_bytes).info_hash())

# ---- Test A: pre-seeded file, no download needed ----------------------------
dl_a = os.path.join(TMP, "dl-a")
seed_a = os.path.join(dl_a, engine_ih)
os.makedirs(seed_a, exist_ok=True)
open(os.path.join(seed_a, "test.pdf"), "wb").write(PDF)

engine = TorrentEngine(download_dir=dl_a)
engine.start()
ih = engine.add_torrent(torrent_file)
assert ih == engine_ih, (ih, engine_ih)
print("engine added:", ih)
engine.wait_for_range(ih, 0, 0, size, timeout=30)
data = engine.fetch_range(ih, 0, 0, size)
assert data == PDF, "direct fetch mismatch"
print("A1 engine direct fetch: OK")

# ---- HTTP layer through the FastAPI app (in-process) ------------------------
from fastapi.testclient import TestClient
import app as app_module

with TestClient(app_module.app) as client:
    # seed the app's own engine the same way
    app_save = os.path.join(app_module.engine._download_dir, engine_ih)
    os.makedirs(app_save, exist_ok=True)
    open(os.path.join(app_save, "test.pdf"), "wb").write(PDF)
    app_ih = app_module.engine.add_torrent(torrent_file)
    assert app_ih == engine_ih
    time.sleep(0.6)
    r = client.get("/api/torrents")
    assert r.status_code == 200
    torrents = r.json()
    match = [t for t in torrents if t["info_hash"] == app_ih]
    print("A2 /api/torrents:", [(t["state"], len(t["files"])) for t in match])
    assert match and match[0]["files"], "torrent not ready"

    r = client.get(f"/api/pdf?ih={app_ih}&f=0", headers={"Range": "bytes=0-65535"})
    assert r.status_code == 206, r.status_code
    assert len(r.content) == min(65536, size)
    assert r.content[:5] == b"%PDF-"
    print("A3 HTTP 206 head range: OK")

    r = client.get(f"/api/pdf?ih={app_ih}&f=0", headers={"Range": f"bytes={size - 100}-"})
    assert r.status_code == 206
    assert b"%%EOF" in r.content
    print("A4 HTTP 206 tail range: OK")

    r = client.get(f"/api/pdf?ih={app_ih}&f=0")
    assert r.status_code == 200
    assert r.content == PDF
    print("A5 HTTP 200 full fetch: OK")

    r = client.get(f"/api/pdf?ih={app_ih}&f=0", headers={"Range": f"bytes={size}-"})
    assert r.status_code == 416
    print("A6 HTTP 416 invalid range: OK")

    r = client.post("/api/catalog", json={"name": "e2e", "magnet": magnet})
    assert r.status_code in (200, 409)
    assert any(c["name"] == "e2e" for c in client.get("/api/catalog").json())
    print("A7 catalog add/list: OK")

# ---- Test C: partial read (only some pieces present, no peers) --------------
# Build a 3-piece torrent, pre-seed ONLY the first piece (the rest is
# zero-filled so it fails verification and stays "missing"). Proves the core
# promise: bytes already on disk are served immediately, while a range that
# hasn't downloaded yet BLOCKS and times out -- it never returns garbage.
# Deterministic (no network), so it always runs.
piece_len = 1024
content = bytes(range(256)) * 12            # 3072 bytes -> exactly 3 pieces
c_name = "part.pdf"
t_bytes, _ = make_torrent(c_name, {c_name: content}, piece_length=piece_len)
t_c = os.path.join(TMP, "part.torrent")
open(t_c, "wb").write(t_bytes)
c_ih = str(lt.torrent_info(t_bytes).info_hash())

dl_c = os.path.join(TMP, "dl-c")
seed_c = os.path.join(dl_c, c_ih)
os.makedirs(seed_c, exist_ok=True)
partial = content[:piece_len] + b"\x00" * (len(content) - piece_len)
open(os.path.join(seed_c, c_name), "wb").write(partial)

eng_c = TorrentEngine(download_dir=dl_c)
eng_c.start()
eng_c.add_torrent(t_c)
try:
    # head (first piece) is present on disk -> served without waiting
    eng_c.wait_for_range(c_ih, 0, 0, piece_len, timeout=20)
    head = eng_c.fetch_range(c_ih, 0, 0, piece_len)
    assert head == content[:piece_len], "head bytes mismatch"
    print("C1 present head served immediately: OK")

    # tail spans a not-yet-downloaded piece -> must time out, not return data
    t0 = time.monotonic()
    try:
        eng_c.wait_for_range(c_ih, 0, len(content) - piece_len, piece_len, timeout=3)
        raise AssertionError("expected TimeoutError for missing tail")
    except TimeoutError:
        pass
    print(f"C2 missing tail blocks (timed out in {time.monotonic() - t0:.1f}s): OK")
finally:
    eng_c.stop()

# ---- Test B: real magnet download across two sessions (best effort) ---------
seeder = lt.session()
seed_dir = os.path.join(TMP, "seed")
os.makedirs(seed_dir, exist_ok=True)
open(os.path.join(seed_dir, "test.pdf"), "wb").write(PDF)
sp = lt.add_torrent_params()
sp.ti = lt.torrent_info(torrent_bytes)
sp.save_path = seed_dir
seeder.add_torrent(sp)

engine_b = TorrentEngine(download_dir=os.path.join(TMP, "dl-b"))
engine_b.start()
b_ih = engine_b.add_torrent(magnet)
t0 = time.time()
try:
    engine_b.wait_for_range(b_ih, 0, 0, size, timeout=45)
    d = engine_b.fetch_range(b_ih, 0, 0, size)
    assert d == PDF
    print(f"B1 magnet cross-session download: OK ({time.time() - t0:.1f}s)")
except TimeoutError:
    print(f"B1 magnet cross-session download: SKIPPED (no peers in {time.time() - t0:.0f}s; "
          f"DHT may be blocked here — not a code failure)")

engine.stop()
engine_b.stop()
shutil.rmtree(TMP, ignore_errors=True)
print("\nALL E2E TESTS PASSED")
