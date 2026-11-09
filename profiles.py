"""Local profiles for the desktop app.

A *profile* is a named, self-contained set of app data (catalog, downloads,
shelves, progress, plugin state, …) stored under ``profiles/<name>/``. Profiles
live on this machine only; the desktop app (``desktop.py``) lets the user pick
one at startup and switch between them. A profile may carry an *optional*
password (PBKDF2-HMAC-SHA256, per-profile salt) that gates selecting it.

``profiles.json`` (at the repo root) is the registry: a list of
``{name, has_password, salt?, hash?, created}``.

The per-profile data layout mirrors what the app used to keep at the root:
the JSON registries plus the on-disk content directories. ``plugins/`` (code)
and ``static/`` stay shared at the root.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import threading
import time
from pathlib import Path

BASE = Path(__file__).parent
# All mutable data (profiles/, profiles.json) lives in a data dir. In a dev
# checkout that's the repo root; the frozen desktop app sets PTR_DATA_DIR to
# a per-user location (e.g. %APPDATA%) so the install dir stays read-only.
DATA_DIR = Path(os.environ.get("PTR_DATA_DIR") or BASE)
PROFILES_FILE = DATA_DIR / "profiles.json"
PROFILES_DIR = DATA_DIR / "profiles"

# per-profile data: JSON registries + on-disk content directories
PROFILE_FILES = [
    "catalog.json", "providers.json", "pdfs.json", "texts.json", "epubs.json",
    "files.json", "favorites.json", "shelf.json", "recent.json",
    "plugin_state.json", "feed.json",
]
PROFILE_DIRS = [".torrents", ".pdfs", ".texts", ".epubs", ".files", "downloads"]

# PBKDF2 parameters (same as the old web-login auth): 200k iterations of
# SHA-256 is the OWASP-recommended floor; ~0.1s per hash.
PBKDF2_ITERATIONS = 200_000


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    ).hex()


def profile_dir(name: str) -> Path:
    return PROFILES_DIR / name


class ProfileStore:
    """Registry of local profiles (profiles.json)."""

    def __init__(self, path: Path = PROFILES_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._profiles: list = []
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self._profiles = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._profiles = []

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._profiles, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def list(self) -> list[dict]:
        # public view: never expose salt/hash
        with self._lock:
            return [
                {"name": p["name"], "has_password": p.get("has_password", False),
                 "created": p.get("created")}
                for p in self._profiles
            ]

    def get(self, name: str) -> dict | None:
        with self._lock:
            return next((p for p in self._profiles if p["name"] == name), None)

    def exists(self, name: str) -> bool:
        return self.get(name) is not None

    def create(self, name: str, password: str = "") -> None:
        name = (name or "").strip()
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise ValueError("invalid profile name")
        with self._lock:
            if any(p["name"] == name for p in self._profiles):
                raise ValueError("profile already exists")
            entry = {"name": name, "created": time.time()}
            if password:
                salt = secrets.token_bytes(16)
                entry["has_password"] = True
                entry["salt"] = salt.hex()
                entry["hash"] = _hash_password(password, salt)
            else:
                entry["has_password"] = False
            self._profiles.append(entry)
            self._save()
        self._scaffold(name)

    def _scaffold(self, name: str) -> None:
        d = profile_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        for f in PROFILE_FILES:
            p = d / f
            if not p.exists() and f == "feed.json":
                p.write_text("[]\n", encoding="utf-8")
            # the other JSON files are created on first write by the app
        for sub in PROFILE_DIRS:
            (d / sub).mkdir(exist_ok=True)

    def verify(self, name: str, password: str) -> bool:
        p = self.get(name)
        if not p:
            return False
        if not p.get("has_password"):
            return password == ""
        return hmac.compare_digest(
            _hash_password(password, bytes.fromhex(p["salt"])), p["hash"]
        )

    def rename(self, name: str, new_name: str) -> None:
        """Rename a profile (registry entry + its data directory).

        The caller must ensure no server is running for ``name`` first — the
        data dir can't be moved while the server holds files in it open.
        """
        new_name = (new_name or "").strip()
        if not new_name or new_name in (".", "..") or "/" in new_name or "\\" in new_name:
            raise ValueError("invalid profile name")
        with self._lock:
            if any(p["name"] == new_name for p in self._profiles):
                raise ValueError("profile already exists")
            entry = next((p for p in self._profiles if p["name"] == name), None)
            if not entry:
                raise ValueError("profile not found")
            entry["name"] = new_name
            self._save()
        d = profile_dir(name)
        if d.exists():
            d.rename(profile_dir(new_name))

    def set_password(self, name: str, current: str, new: str) -> None:
        """Set (or clear, with new="") a profile's password. If the profile
        already has one, the current password must be supplied and correct."""
        with self._lock:
            entry = next((p for p in self._profiles if p["name"] == name), None)
            if not entry:
                raise ValueError("profile not found")
            if entry.get("has_password"):
                if not hmac.compare_digest(
                    _hash_password(current or "", bytes.fromhex(entry["salt"])),
                    entry["hash"]):
                    raise ValueError("current password is incorrect")
            if new:
                salt = secrets.token_bytes(16)
                entry["has_password"] = True
                entry["salt"] = salt.hex()
                entry["hash"] = _hash_password(new, salt)
            else:
                entry["has_password"] = False
                entry.pop("salt", None)
                entry.pop("hash", None)
            self._save()

    def delete(self, name: str) -> None:
        with self._lock:
            self._profiles = [p for p in self._profiles if p["name"] != name]
            self._save()
        d = profile_dir(name)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


profiles = ProfileStore()


def migrate_root_data() -> None:
    """One-time: fold the legacy root-level data into a ``default`` profile.

    Runs only when ``profiles.json`` doesn't exist yet. If there's legacy data
    at the root (``catalog.json``), it's moved into ``profiles/default/``;
    otherwise an empty ``default`` profile is created. Idempotent — a no-op
    once ``profiles.json`` exists.
    """
    if PROFILES_FILE.exists():
        return
    profiles.create("default")
    d = profile_dir("default")
    if (BASE / "catalog.json").exists():
        for f in PROFILE_FILES:
            src = BASE / f
            if src.exists():
                shutil.move(str(src), str(d / f))
        for sub in PROFILE_DIRS:
            src = BASE / sub
            if src.exists():
                shutil.move(str(src), str(d / sub))
