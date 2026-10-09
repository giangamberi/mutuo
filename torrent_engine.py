"""Torrent engine: runs a libtorrent 2.x session in a background thread.

Serves arbitrary byte ranges of files inside torrents to PDF.js.

libtorrent 2.x Python API notes (verified empirically, see e2e_test.py):
- There is NO handle.file_storage(), handle.file_slice() or handle.piece_passed().
- File layout is read from a `torrent_info.files()` (file_storage) object,
  obtained at add-time for .torrent files, or via handle.get_torrent_info()
  once magnet metadata has arrived.
- "Do we have the bytes?"  -> handle.have_piece(piece_index)
- "Give me the bytes"     -> read them from disk: libtorrent writes verified
  pieces to  <save_path>/<file_path(i)>  so once every piece covering a range
  has passed, a plain file read of that range is correct and cheap.
- file_storage methods that exist: file_name(i), file_path(i), file_size(i),
  file_offset(i), piece_length(), num_pieces(), num_files(), name().
  (piece_size(i) takes an index; piece_length() is the uniform size.)
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import libtorrent as lt


class TorrentError(Exception):
    pass


def _ih_hex(info_hash) -> str:
    """Normalize an info hash to its hex string.

    libtorrent exposes this inconsistently: a method on file-based
    torrent_info, but a plain property on magnet-parsed torrent_info.
    """
    val = info_hash() if callable(info_hash) else info_hash
    return str(val)


def _s(x) -> str:
    """libtorrent returns name()/trackers()/file_name() as bytes on some builds;
    coerce to str before it reaches a JSON response or a magnet URI."""
    if isinstance(x, bytes):
        return x.decode("utf-8", "replace")
    if x is None:
        return ""
    return str(x)


@dataclass
class TorrentState:
    info_hash: str                           # hex string, used as the dict key everywhere
    name: str = ""
    magnet: str = ""
    save_path: str = ""
    ready: bool = False                      # metadata received
    fs: object = None                        # lt.file_storage (or None for magnet pre-metadata)
    progress: float = 0.0
    state: str = "unknown"
    num_peers: int = 0
    num_seeds: int = 0
    num_complete: int = 0
    num_incomplete: int = 0
    upload_rate: int = 0
    download_rate: int = 0
    is_seeding: bool = False
    is_paused: bool = False
    total_size: int = 0
    total_done: int = 0
    file_progress: dict[int, int] = field(default_factory=dict)  # idx -> bytes done
    last_dht: float = 0.0                    # last force_dht_lookup() (magnet bootstrap)


class TorrentEngine:
    def __init__(self, download_dir: str):
        self._download_dir = download_dir
        os.makedirs(download_dir, exist_ok=True)
        # Persist DHT routing table + session state so a restart does NOT start
        # from a cold, empty DHT table — the usual reason re-added torrents sit
        # at 0 peers for minutes right after a restart.
        self._state_file = Path(download_dir) / ".session_state.json"
        self._session = lt.session()
        self._torrents: dict[str, TorrentState] = {}     # keyed by hex info-hash string
        self._handle: dict[str, "lt.torrent_handle"] = {}
        self._cond = threading.Condition()
        self._lock = threading.Lock()
        self._pending_prio: dict[str, set[int]] = {}     # hex -> pieces to prioritize
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    # ---------- session state persistence ----------

    def _load_state(self) -> None:
        """Restore DHT routing table + session state from a previous run so a
        restart does NOT start from a cold, empty DHT table. Best-effort: any
        failure just means we start cold (pre-fix behaviour)."""
        if not self._state_file.exists():
            return
        try:
            blob = base64.b64decode(
                json.loads(self._state_file.read_text(encoding="utf-8")).get("session", "")
            )
            if blob:
                self._session.load_state(blob)   # 2.x: save_state embeds DHT; load_state restores it
                print(f"engine: restored session state from {self._state_file.name}")
        except Exception as exc:
            print(f"engine: could not restore session state ({exc}); starting cold")

    def _save_state(self) -> None:
        """Snapshot session state (2.x embeds the DHT routing table in it). Called
        periodically and on shutdown so a restart resumes warm."""
        try:
            blob = self._session.save_state()
            # 2.x returns a dict; bencode to a stable byte blob. 1.x returns bytes.
            if not isinstance(blob, (bytes, bytearray)):
                blob = lt.bencode(dict(blob))
            if not blob:
                return
            payload = {"session": base64.b64encode(bytes(blob)).decode("ascii")}
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self._state_file)   # atomic so a crash never corrupts it
        except Exception as exc:
            print(f"engine: failed to save session state ({exc})")

    # ---------- session setup ----------

    def start(self) -> None:
        cfg = self._session.get_settings()
        cfg["download_rate_limit"] = 0
        cfg["upload_rate_limit"] = 0
        cfg["active_downloads"] = 16
        cfg["active_limit"] = 32
        cfg["dont_count_slow_torrents"] = True
        self._session.apply_settings(cfg)
        self._session.add_dht_node(("router.bittorrent.com", 6881))
        self._session.add_dht_node(("router.utorrent.com", 6881))
        try:
            self._session.listen_on(6881, 6899)
        except Exception as exc:  # port range unavailable — keep default
            print(f"engine: listen_on failed ({exc}); using default port")
        self._load_state()
        self._thread.start()

    # ---------- public API (thread-safe; wait_* may block) ----------

    def add_torrent(self, source: str, magnet: str = "") -> str:
        """Add a magnet URI, .torrent URL, or .torrent file path. Returns info_hash hex.

        `magnet` optionally records a canonical magnet URI against the state.
        When adding from a .torrent file we pass the file's constructed magnet
        so the UI can still match the entry to its catalog record by name."""
        is_magnet = source.startswith("magnet:")
        # magnets and .torrent files/URLs parse into different objects:
        #   magnet -> add_torrent_params (no metadata yet)  -> libtorrent fetches it via DHT
        #   file/url -> torrent_info (metadata already in hand)
        if is_magnet:
            params = lt.parse_magnet_uri(source)
            name = params.name
            ih_hex = str(params.info_hashes.get_best())
            # no file list until metadata is fetched from the swarm
            fs = None
        else:
            ti = lt.torrent_info(source)
            name = _s(ti.name())
            ih_hex = _ih_hex(ti.info_hash)
            params = lt.add_torrent_params()
            params.ti = ti
            fs = ti.files()
        save_path = os.path.join(self._download_dir, ih_hex)
        os.makedirs(save_path, exist_ok=True)
        with self._lock:
            ts = self._torrents.get(ih_hex)
            if ts is not None:
                # already tracked (e.g. added from a magnet at startup): attach
                # the file list now that full metadata is in hand, so the UI does
                # not sit on "waiting for metadata" forever.
                if ts.fs is None and fs is not None and fs.num_files() > 0:
                    ts.fs = fs
                    ts.ready = True
                return ih_hex
            params.save_path = save_path
            if not is_magnet:
                params.flags = lt.torrent_flags.auto_managed
            try:
                handle = self._session.add_torrent(params)
            except RuntimeError:
                return ih_hex  # already present (e.g. app restart)
            ts = TorrentState(
                info_hash=ih_hex, name=name, save_path=save_path,
                magnet=magnet or (source if is_magnet else ""),
            )
            if fs is not None and fs.num_files() > 0:  # .torrent files carry the file list
                ts.fs = fs
                ts.ready = True
            self._torrents[ih_hex] = ts
            self._handle[ih_hex] = handle
            with self._cond:
                self._cond.notify_all()
        return ih_hex

    def add_torrent_async(self, source: str) -> None:
        threading.Thread(target=self.add_torrent, args=(source,), daemon=True).start()

    def status(self, include_files: bool = True) -> list[dict]:
        return [self._entry(ts, include_files) for ts in list(self._torrents.values())]

    def get(self, ih_hex: str, include_files: bool = True):
        """Snapshot of a single torrent, or None when it is not in the session."""
        ts = self._torrents.get(ih_hex)
        return None if ts is None else self._entry(ts, include_files)

    def _entry(self, ts: TorrentState, include_files: bool = True) -> dict:
        entry = {
            "info_hash": ts.info_hash,
            "name": ts.name,
            "magnet": ts.magnet,
            "ready": ts.ready,
            "progress": ts.progress,
            "state": ts.state,
            "num_peers": ts.num_peers,
            "num_seeds": ts.num_seeds,
            "num_complete": ts.num_complete,
            "num_incomplete": ts.num_incomplete,
            "upload_rate": ts.upload_rate,
            "download_rate": ts.download_rate,
            "is_seeding": ts.is_seeding,
            "is_paused": ts.is_paused,
            "total_size": ts.total_size,
            "total_done": ts.total_done,
        }
        if include_files and ts.fs is not None:
            prios = self._file_prios(ts)
            entry["files"] = [
                {"index": i, "name": _s(ts.fs.file_name(i)),
                 "size": ts.fs.file_size(i),
                 "done": ts.file_progress.get(i, 0),
                 "priority": prios[i]}
                for i in range(ts.fs.num_files())
            ]
        else:
            entry["files"] = []
        return entry

    def _file_prios(self, ts: TorrentState) -> list[int]:
        """Current per-file download priorities (0 means excluded)."""
        handle = self._handle.get(ts.info_hash)
        n = ts.fs.num_files()
        if handle is not None and handle.is_valid():
            try:
                return [int(p) for p in handle.file_priorities()]
            except Exception:
                pass
        return [4] * n

    def file_size(self, ih_hex: str, file_index: int) -> int:
        ts = self._torrents[ih_hex]
        if ts.fs is None:
            raise TorrentError("metadata not ready")
        if file_index >= ts.fs.num_files():
            raise TorrentError(f"no file {file_index}")
        return ts.fs.file_size(file_index)

    def wait_for_range(self, ih_hex: str, file_index: int, start: int, length: int,
                       timeout: float = 300.0) -> None:
        """Block until metadata is ready AND every piece covering
        [start, start+length) of file_index has been verified."""
        ih = ih_hex
        deadline = time.monotonic() + timeout
        while True:
            ts = self._torrents.get(ih)
            if ts is None:
                raise KeyError(ih_hex)
            handle = self._handle.get(ih)
            if ts.fs is not None and handle is not None and handle.is_valid():
                missing = self._missing_pieces(ih, ts, file_index, start, length)
                if not missing:
                    return
                self._pending_prio.setdefault(ih, set()).update(missing)
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("timed out waiting for metadata/pieces")
            with self._cond:
                self._cond.wait(timeout=min(left, 1.0))

    def fetch_range(self, ih_hex: str, file_index: int, start: int, length: int) -> bytes:
        """Return verified bytes by reading them from disk (caller waited first)."""
        ts = self._torrents[ih_hex]
        if ts.fs is None:
            raise TorrentError("metadata not ready")
        path = os.path.join(ts.save_path, ts.fs.file_path(file_index))
        if not os.path.exists(path):
            raise TorrentError("file not on disk yet")
        with open(path, "rb") as fh:
            fh.seek(ts.fs.file_offset(file_index) + start)
            return fh.read(length)

    # ---------- per-torrent actions ----------

    def pause(self, ih_hex: str) -> None:
        handle = self._handle.get(ih_hex)
        if handle is not None and handle.is_valid():
            handle.pause()

    def resume(self, ih_hex: str) -> None:
        handle = self._handle.get(ih_hex)
        if handle is not None and handle.is_valid():
            handle.resume()

    def force_recheck(self, ih_hex: str) -> None:
        handle = self._handle.get(ih_hex)
        if handle is not None and handle.is_valid():
            handle.force_recheck()

    def remove_torrent(self, ih_hex: str, delete_files: bool = False) -> None:
        """Remove from the session; when delete_files, also wipe the per-torrent
        data directory. Deleting the directory ourselves (rather than relying on
        libtorrent's remove flag) is deterministic across libtorrent versions."""
        ts = self._torrents.get(ih_hex)
        save_path = ts.save_path if ts is not None else None
        handle = self._handle.pop(ih_hex, None)
        if handle is not None and handle.is_valid():
            self._session.remove_torrent(handle)
        self._torrents.pop(ih_hex, None)
        self._pending_prio.pop(ih_hex, None)
        if delete_files and save_path:
            shutil.rmtree(save_path, ignore_errors=True)

    def set_file_priority(self, ih_hex: str, file_index: int, priority: int) -> None:
        """Set download priority for one file (0=exclude, 4=normal, 7=max).

        This libtorrent 2.x binding takes a single index-aligned list of
        priorities (not a (indices, priority) pair), so read the current vector,
        patch the one index, and write it back."""
        handle = self._handle.get(ih_hex)
        if handle is None or not handle.is_valid():
            return
        ts = self._torrents.get(ih_hex)
        n = ts.fs.num_files() if ts is not None and ts.fs is not None else 0
        try:
            prios = [int(p) for p in handle.file_priorities()]
        except Exception:
            prios = []
        while len(prios) < n:
            prios.append(4)
        if file_index < n:
            prios[file_index] = priority
        try:
            handle.prioritize_files(prios)
        except (TypeError, AttributeError):
            try:  # 1.x single-file API
                handle.prioritize_file(file_index, priority)
            except Exception:
                pass

    # ---------- internals ----------

    def _missing_pieces(self, ih: str, ts: TorrentState, file_index: int,
                        start: int, length: int) -> set[int]:
        handle = self._handle[ih]
        pl = ts.fs.piece_length()
        off = ts.fs.file_offset(file_index)
        first = (off + start) // pl
        last = (off + start + max(length - 1, 0)) // pl
        return {p for p in range(first, last + 1) if not handle.have_piece(p)}

    def _apply_priorities(self) -> None:
        """Re-assert priorities on requested pieces so the swarm focuses on them."""
        for ih, pieces in self._pending_prio.items():
            if not pieces:
                continue
            handle = self._handle.get(ih)
            if handle is None or not handle.is_valid():
                continue
            try:
                handle.prioritize_pieces(sorted(pieces), 4)
            except Exception:
                pass

    def _refresh_states(self) -> None:
        for handle in self._session.get_torrents():
            try:
                status = handle.status()
            except Exception:
                continue
            ih = str(status.info_hash)
            ts = self._torrents.get(ih)
            if ts is None:
                continue
            self._handle[ih] = handle
            ts.progress = status.progress
            ts.state = self._state_str(status)
            ts.num_peers = status.num_peers
            ts.num_seeds = getattr(status, "num_seeds", 0)
            ts.num_complete = getattr(status, "num_complete", 0)
            ts.num_incomplete = getattr(status, "num_incomplete", 0)
            ts.upload_rate = getattr(status, "upload_payload_rate", 0)
            ts.download_rate = getattr(status, "download_payload_rate", 0)
            ts.is_seeding = bool(getattr(status, "seeding", False))
            ts.is_paused = bool(getattr(status, "paused", False))
            ts.total_size = status.total_wanted
            ts.total_done = status.total_wanted_done
            if ts.fs is None:
                # magnet without metadata yet: nudge DHT so we do not sit at 0
                # peers forever just because the routing table filled slowly.
                if time.monotonic() - ts.last_dht > 10.0:
                    ts.last_dht = time.monotonic()
                    try:
                        handle.force_dht_lookup()
                    except Exception:
                        pass
            # pick up file list when magnet metadata arrives
            if status.has_metadata and ts.fs is None:
                try:
                    ti = handle.get_torrent_info()
                    if ti is not None:
                        ts.fs = ti.files()
                        ts.name = ts.name or ti.name()
                        ts.ready = True
                except Exception:
                    pass
            if ts.fs is not None:
                try:
                    ts.file_progress = {
                        i: int(d) for i, d in enumerate(handle.file_progress())
                    }
                except Exception:
                    pass
        with self._cond:
            self._cond.notify_all()

    @staticmethod
    def _state_str(status: "lt.torrent_status") -> str:
        if status.paused:
            return "paused"
        if status.checking_files:
            return "checking"
        if status.seeding:
            return "seed"
        if status.downloading_metadata:
            return "fetching metadata"
        names = {
            getattr(lt.torrent_status, "state_none", 0): "queued",
            getattr(lt.torrent_status, "state_checking", 1): "checking",
            getattr(lt.torrent_status, "state_downloading", 2): "downloading",
            getattr(lt.torrent_status, "state_downloading_metadata", 3): "fetching metadata",
            getattr(lt.torrent_status, "state_finished", 4): "done",
            getattr(lt.torrent_status, "state_seeding", 5): "seeding",
            getattr(lt.torrent_status, "state_allocating", 6): "allocating",
            getattr(lt.torrent_status, "state_forced_checking", 7): "forced checking",
        }
        return names.get(status.state, f"state={status.state}")

    def _run(self) -> None:
        # save DHT/session state every 30 s so a restart resumes warm
        _last_save = 0.0
        while not self._stop:
            self._session.wait_for_alert(250)  # also acts as a 250ms tick
            self._session.pop_alerts()        # drain; state polling is our signal
            self._refresh_states()
            self._apply_priorities()
            now = time.monotonic()
            if now - _last_save > 30.0:
                self._save_state()
                _last_save = now

    def stop(self) -> None:
        self._stop = True
        try:
            self._save_state()  # final snapshot so the next start restores cleanly
        except Exception:
            pass
