# Writing a Mutuo plugin

A plugin is a **single `.py` file** in this folder. Drop it in, and it shows
up in the **Plugins** tab — no restart, no build step, no registration. The
app loads it on demand (never at startup), so a broken plugin can't take the
app down.

Start from an existing one — `gutenberg.py` is the cleanest reference.

## The two entry points

| Function | What it does |
|----------|--------------|
| `search(query, config) -> list[dict]` | A **live** search. Gives the plugin its own searchable tab in the sidebar; results appear as the user types. |
| `fetch(query) -> list[dict]` | A **one-shot** run (the ▶ Run button) for sources that aren't a live search. |

Define either or both. `query` is the search string; `config` is a dict of
the values from your `CONFIG` settings (see below).

## Result items

Each returned item is a dict with a `name` plus **one** of:

```python
{"name": "Title", "url": "https://…/book.pdf"}   # a PDF to open (downloaded on click)
{"name": "Title", "magnet": "magnet:?xt=…"}      # a torrent to track
{"name": "Title", "path": "C:/…/book.pdf"}       # a local file to read in place
```

Optional keys:

- **`"kind"`** — how to open a `url` item: `"pdf"` (default), `"html"`,
  `"text"`, or `"epub"`. Use `"html"` for a web page, `"text"` for a plain
  text file, `"epub"` for an EPUB.
- **`"meta"`** — a short subtitle line shown under the name (e.g. author,
  year, size).
- **`"resolve_id"`** — for two-stage sources: an identifier your `resolve()`
  uses to fetch the real PDF later (see below).

## Optional: `CONFIG` settings

A module-level `CONFIG` list exposes settings the UI renders in the search
bar and passes back to `search()` as the `config` dict:

```python
CONFIG = [
    {"key": "sort", "label": "Sort by", "type": "select",
     "default": "relevance",
     "options": [
         {"value": "relevance", "label": "Relevance"},
         {"value": "date", "label": "Newest"},
     ]},
    {"key": "limit", "label": "Max results", "type": "text",
     "default": "20", "placeholder": "e.g. 20"},
]
```

- `type` is `"select"` (needs `options`) or `"text"`.
- In `search()`, read them with a fallback: `config.get("sort", "relevance")`.

## Optional: `covers(items) -> list`

Return a cover image (URL or `data:` URI) for each result, **aligned by
index** with the items you returned from `search()`. Return `""` for an item
with no cover. This is what fills the thumbnails in the result list.

```python
def covers(items: list) -> list:
    return [(it or {}).get("cover_url", "") for it in (items or [])]
```

## Optional: `resolve(ids) -> dict` (two-stage sources)

For sources where the list and the file are separate calls (e.g. archive.org:
search returns identifiers, the PDF URL comes from a second metadata call),
tag each `search()` item with a `"resolve_id"` and define:

```python
def resolve(ids: list) -> dict:
    # {resolve_id: {"url": …, "meta"?} | None}
    ...
```

The app calls it with a batch of `resolve_id`s after the list loads and
swaps in the real PDFs. Return `None` for an id with no openable file.

## Metadata

Two module-level strings are read from your source (via AST, without running
it) for the Plugins tab:

```python
NAME = "My Source"
DESCRIPTION = "One line shown in the Plugins tab."
```

`NAME` defaults to the filename if omitted.

## A complete minimal example

```python
"""Example: search a JSON API and return the matching PDFs."""
import json
import urllib.parse
import urllib.request

NAME = "Example"
DESCRIPTION = "Search the Example archive."

API = "https://api.example.com/search"
UA = {"User-Agent": "mutuo-plugin/1.0"}

CONFIG = [
    {"key": "limit", "label": "Max results", "type": "text",
     "default": "20", "placeholder": "e.g. 20"},
]


def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def search(query: str, config: dict) -> list[dict]:
    limit = int(config.get("limit") or 20)
    url = f"{API}?q={urllib.parse.quote(query)}&limit={limit}"
    data = _get_json(url)
    items = []
    for r in data.get("results", []):
        if not r.get("pdf_url"):
            continue
        items.append({
            "name": r["title"],
            "url": r["pdf_url"],
            "kind": "pdf",
            "meta": r.get("author", ""),
        })
    return items


def covers(items: list) -> list:
    # no real covers — return empty strings (the list just shows no thumbnail)
    return [""] * len(items or [])
```

Save it as `plugins/example.py` and it appears in the Plugins tab
immediately.

## Notes & conventions

- **Stdlib only** — the built-in plugins use `urllib`/`json`/`re`, no third-
  party deps. (Pillow is available for generating cover images, as
  `arxiv.py`/`openalex.py` do.)
- **Be polite to the API** — set a `User-Agent`, keep timeouts short, and
  fail fast with a clear message rather than retrying in a loop. The front
  end keeps the previous results on screen, so a fast, honest error is better
  than a hang.
- **Personal plugins** — add your filename to `.gitignore` to keep it out of
  the repo and the installer (the built-ins are the git-tracked ones).
- **Testing** — run the app (`python desktop.py`), open the Plugins tab, and
  search. A plugin that raises shows the error inline without crashing the
  app.
