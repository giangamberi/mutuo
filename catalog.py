"""Catalog: the list of PDFs to make available, sourced from a feed.

Feed format (JSON):
    [
      {"name": "Acme Q3 report", "magnet": "magnet:?xt=urn:btih:...", "files": ["report.pdf"]},
      ...
    ]
or
    {"items": [ ... same objects ... ]}

"files" is optional; when omitted the reader shows every PDF in the torrent.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CatalogItem:
    name: str
    magnet: str
    files: list[str] = field(default_factory=list)
    added_at: float = field(default_factory=time.time)
    provider: str = "local"   # which provider's list this torrent came from


class Catalog:
    def __init__(self, path: str):
        self._path = path
        self._items: dict[str, CatalogItem] = {}  # keyed by info hash from magnet
        self._load()

    @staticmethod
    def _magnet_key(magnet: str) -> str:
        for part in magnet.split("&"):
            if part.startswith("xt="):
                return part[3:]
        return magnet.strip()

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError):
            # a crash mid-save can leave a truncated file — keep it (renamed)
            # so nothing is lost, and start empty instead of failing to boot
            try:
                Path(self._path).rename(Path(self._path).with_name(Path(self._path).name + ".corrupt"))
            except OSError:
                pass
            return
        for entry in self._entries(data):
            self._items[self._magnet_key(entry["magnet"])] = CatalogItem(
                name=entry.get("name", ""),
                magnet=entry["magnet"],
                files=entry.get("files", []),
                provider=entry.get("provider", "local"),
            )

    def save(self) -> None:
        entries = [
            {"name": it.name, "magnet": it.magnet, "files": it.files,
             "added_at": it.added_at, "provider": it.provider}
            for it in self._items.values()
        ]
        # atomic (tmp + rename) like the other registries: a crash mid-write
        # must not leave a half-written file that _load then has to recover
        p = Path(self._path)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        tmp.replace(p)

    def items(self) -> list[CatalogItem]:
        return list(self._items.values())

    def has(self, magnet: str) -> bool:
        return self._magnet_key(magnet) in self._items

    def add(self, name: str, magnet: str, files: list[str] | None = None,
            provider: str = "local") -> bool:
        key = self._magnet_key(magnet)
        if key in self._items:
            return False
        self._items[key] = CatalogItem(name=name, magnet=magnet, files=files or [],
                                       provider=provider)
        return True

    def remove(self, magnet: str) -> bool:
        """Drop an entry (by magnet or bare info-hash) and persist. Returns True
        if it was present. Without this, a removed torrent is re-added on the
        next feed sync or restart. The engine/API pass the bare hex info-hash,
        but entries are keyed by the full magnet, so also match on the xt= hash."""
        key = self._magnet_key(magnet)
        if key in self._items:
            del self._items[key]
            self.save()
            return True
        ih = key.lower()
        if ih:
            for k in list(self._items):
                if f"xt=urn:btih:{ih}" in self._items[k].magnet.lower():
                    del self._items[k]
                    self.save()
                    return True
        return False

    def remove_provider(self, provider: str) -> int:
        """Drop every item tagged with `provider` (e.g. a plugin's results).
        Returns how many were removed."""
        keys = [k for k, it in self._items.items() if it.provider == provider]
        for k in keys:
            del self._items[k]
        if keys:
            self.save()
        return len(keys)

    def sync(self, feed_url: str | None, provider: str = "local") -> int:
        """Merge a feed into the catalog. Returns number of newly added items.

        `provider` tags each new item so the UI can show it under the right
        tab; a magnet already owned by another provider is left untouched."""
        if not feed_url:
            return 0
        if feed_url.startswith(("http://", "https://")):
            with urllib.request.urlopen(feed_url, timeout=30) as resp:
                data = json.load(resp)
        else:  # local file path
            with open(feed_url, encoding="utf-8") as fh:
                data = json.load(fh)
        added = 0
        for entry in self._entries(data):
            if self.add(entry.get("name", ""), entry["magnet"], entry.get("files"),
                        provider=provider):
                added += 1
        if added:
            self.save()
        return added

    @staticmethod
    def _entries(data):
        if isinstance(data, dict):
            data = data.get("items", data.get("torrents", []))
        for entry in data:
            magnet = entry.get("magnet", "")
            if magnet.startswith("magnet:"):
                yield entry
            elif entry.get("url", "").endswith(".torrent"):
                # accept bare .torrent URLs; the engine also accepts URLs
                yield {"name": entry.get("name", ""), "magnet": entry["url"], "files": entry.get("files", [])}


@dataclass
class Provider:
    id: str
    name: str
    feed: str = ""          # local path or URL, same shape as feed.json
    description: str = ""
    enabled: bool = True
    added_at: float = field(default_factory=time.time)


class ProviderCatalog:
    """Named torrent *providers* — each one owns its own torrent list (a feed
    in the same format as feed.json). The default "Local" provider is the
    built-in catalog (catalog.json + feed.json); extra providers are
    persisted to providers.json.

    A provider's items are resolved lazily: its feed is fetched on demand and
    merged into the main catalog, so the app's single source of truth for
    *what to track* stays the catalog. The provider list itself is only the
    UI grouping."""

    def __init__(self, path: str):
        self._path = path
        self._providers: dict[str, Provider] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError):
            # see Catalog._load — recover, don't brick startup
            try:
                Path(self._path).rename(Path(self._path).with_name(Path(self._path).name + ".corrupt"))
            except OSError:
                pass
            return
        for entry in data:
            p = Provider(
                id=entry["id"],
                name=entry.get("name", entry["id"]),
                feed=entry.get("feed", ""),
                description=entry.get("description", ""),
                enabled=entry.get("enabled", True),
                added_at=entry.get("added_at", time.time()),
            )
            self._providers[p.id] = p

    def save(self) -> None:
        entries = [
            {"id": p.id, "name": p.name, "feed": p.feed,
             "description": p.description, "enabled": p.enabled,
             "added_at": p.added_at}
            for p in self._providers.values()
        ]
        p = Path(self._path)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        tmp.replace(p)

    def items(self) -> list[Provider]:
        return list(self._providers.values())

    def get(self, pid: str) -> Provider | None:
        return self._providers.get(pid)

    def add(self, name: str, feed: str = "", description: str = "") -> Provider:
        pid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or f"p{int(time.time())}"
        base = pid
        while pid in self._providers:
            pid = f"{base}-{int(time.time())}"
        p = Provider(id=pid, name=name, feed=feed, description=description)
        self._providers[pid] = p
        return p

    def remove(self, pid: str) -> bool:
        return self._providers.pop(pid, None) is not None

    def set_enabled(self, pid: str, enabled: bool) -> bool:
        p = self._providers.get(pid)
        if p is None:
            return False
        p.enabled = enabled
        return True
