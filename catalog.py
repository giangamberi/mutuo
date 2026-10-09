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
import time
import urllib.request
from dataclasses import dataclass, field


@dataclass
class CatalogItem:
    name: str
    magnet: str
    files: list[str] = field(default_factory=list)
    added_at: float = field(default_factory=time.time)


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
            with open(self._path) as fh:
                for entry in self._entries(json.load(fh)):
                    self._items[self._magnet_key(entry["magnet"])] = CatalogItem(
                        name=entry.get("name", ""),
                        magnet=entry["magnet"],
                        files=entry.get("files", []),
                    )
        except FileNotFoundError:
            pass

    def save(self) -> None:
        entries = [
            {"name": it.name, "magnet": it.magnet, "files": it.files, "added_at": it.added_at}
            for it in self._items.values()
        ]
        with open(self._path, "w") as fh:
            json.dump(entries, fh, indent=2)

    def items(self) -> list[CatalogItem]:
        return list(self._items.values())

    def has(self, magnet: str) -> bool:
        return self._magnet_key(magnet) in self._items

    def add(self, name: str, magnet: str, files: list[str] | None = None) -> bool:
        key = self._magnet_key(magnet)
        if key in self._items:
            return False
        self._items[key] = CatalogItem(name=name, magnet=magnet, files=files or [])
        return True

    def sync(self, feed_url: str | None) -> int:
        """Merge a feed into the catalog. Returns number of newly added items."""
        if not feed_url:
            return 0
        if feed_url.startswith(("http://", "https://")):
            with urllib.request.urlopen(feed_url, timeout=30) as resp:
                data = json.load(resp)
        else:  # local file path
            with open(feed_url) as fh:
                data = json.load(fh)
        added = 0
        for entry in self._entries(data):
            if self.add(entry.get("name", ""), entry["magnet"], entry.get("files")):
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
