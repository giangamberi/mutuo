"""Plugin loader: discover and run user-supplied "plugins".

A plugin is a single .py file in the plugins/ directory. It can define:

    search(query, config) -> list[dict]   a *live* search (makes the plugin an
                                           active provider with a searchable tab)
    fetch(query) -> list[dict]            a one-shot run (the "Run" button)

Each returned item is one of:

    {"name": str, "url":  str}     -> a PDF to open (downloaded on click)
    {"name": str, "magnet": str}   -> a torrent to track
    {"name": str, "path": str}     -> a local PDF to read in place

An item may also carry a "meta" string shown as a subtitle line. A plugin may
define CONFIG = [ {"key","label","type","default","options"}, ... ] to expose
settings (e.g. sort order, language) that the UI renders and passes to
search() as the `config` dict.

Plugins are the extension point for *behaviours* that don't fit a static
provider feed: e.g. a plugin that searches Project Gutenberg and returns the
matching books as PDFs. The app never imports a plugin at startup — it is
loaded on demand (and can be removed at any time), so a broken plugin can't
take the app down.
"""
from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent / "plugins"
STATE_PATH = Path(__file__).parent / "plugin_state.json"


@dataclass
class Plugin:
    id: str
    name: str
    description: str = ""
    path: str = ""
    added_at: float = field(default_factory=time.time)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or f"p{int(time.time())}"


def _module_name(path: Path) -> str:
    # unique, import-safe module name so two plugins can't collide in sys.modules
    return f"ptr_plugin_{_slug(path.stem)}"


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(_module_name(path), str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load plugin module from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _meta_from_source(path: Path) -> dict:
    """Pull NAME/DESCRIPTION/NEEDS_QUERY/CONFIG out of a plugin's source via
    AST (without executing it) and detect whether it defines search()/fetch(),
    so the list view works even for a plugin that would error at import time."""
    meta = {"name": path.stem, "description": "", "needs_query": True,
            "config_schema": None, "has_search": False, "has_fetch": False}
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return meta
    try:
        tree = ast.parse(src)
    except SyntaxError:
        m = re.search(r"^\s*NAME\s*=\s*['\"](.+?)['\"]", src, re.M)
        if m:
            meta["name"] = m.group(1)
        m = re.search(r"^\s*DESCRIPTION\s*=\s*['\"](.+?)['\"]", src, re.M)
        if m:
            meta["description"] = m.group(1)
        m = re.search(r"^\s*NEEDS_QUERY\s*=\s*(True|False)", src, re.M)
        if m:
            meta["needs_query"] = m.group(1) == "True"
        return meta
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            if node.name == "search":
                meta["has_search"] = True
            elif node.name == "fetch":
                meta["has_fetch"] = True
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if not isinstance(t, ast.Name):
                continue
            if t.id == "NAME" and isinstance(node.value, ast.Constant):
                meta["name"] = str(node.value.value)
            elif t.id == "DESCRIPTION" and isinstance(node.value, ast.Constant):
                meta["description"] = str(node.value.value)
            elif t.id == "NEEDS_QUERY" and isinstance(node.value, ast.Constant):
                meta["needs_query"] = bool(node.value.value)
            elif t.id == "CONFIG":
                try:
                    meta["config_schema"] = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    meta["config_schema"] = None
    return meta


# ---- per-plugin state (active flag + config), persisted in plugin_state.json ----

def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def get_state(pid: str) -> dict:
    s = load_state().get(pid, {})
    return {"active": bool(s.get("active", False)), "config": dict(s.get("config", {}))}


def set_active(pid: str, active: bool) -> None:
    state = load_state()
    state.setdefault(pid, {})["active"] = bool(active)
    _save_state(state)


def set_config(pid: str, config: dict) -> None:
    state = load_state()
    state.setdefault(pid, {})["config"] = dict(config or {})
    _save_state(state)


def list_plugins() -> list[dict]:
    """Every plugin file in plugins/, with metadata from its source and the
    persisted active/config state."""
    if not PLUGIN_DIR.is_dir():
        return []
    out = []
    for f in sorted(PLUGIN_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        meta = _meta_from_source(f)
        st = get_state(f.stem)
        out.append({"id": f.stem, "name": meta["name"],
                   "description": meta["description"],
                   "needs_query": meta["needs_query"],
                   "config_schema": meta["config_schema"],
                   "has_search": meta["has_search"],
                   "has_fetch": meta["has_fetch"],
                   "active": st["active"], "config": st["config"],
                   "path": str(f)})
    return out


def search_plugin(pid: str, query: str, config: dict) -> list[dict]:
    """Run one plugin's search(query, config) and normalise its result to a
    list of {"name", "url"|"magnet"|"path", "meta"?} items. Raises on a bad
    plugin or a malformed result so the caller can surface a clear error."""
    path = PLUGIN_DIR / f"{pid}.py"
    if not path.is_file():
        raise FileNotFoundError(f"plugin not found: {pid}")
    mod = _load_module(path)
    search = getattr(mod, "search", None)
    if not callable(search):
        raise RuntimeError(f"plugin {pid} has no search() function")
    raw = search(query or "", dict(config or {}))
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise RuntimeError(f"plugin {pid} search() must return a list")
    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        item = {"name": name}
        for k in ("url", "magnet", "path"):
            v = str(entry.get(k, "")).strip()
            if v:
                item[k] = v
        meta = str(entry.get("meta", "")).strip()
        if meta:
            item["meta"] = meta
        items.append(item)
    return items


def run_plugin(pid: str, query: str) -> list[dict]:
    """Run one plugin's fetch(query) and normalise its result to a list of
    {"name", "magnet"|"url"|"path"} items. Raises on a bad plugin or a
    malformed result so the caller can surface a clear error."""
    path = PLUGIN_DIR / f"{pid}.py"
    if not path.is_file():
        raise FileNotFoundError(f"plugin not found: {pid}")
    mod = _load_module(path)
    fetch = getattr(mod, "fetch", None)
    if not callable(fetch):
        raise RuntimeError(f"plugin {pid} has no fetch() function")
    # A plugin may define fetch() (no query) or fetch(query). Pass the query
    # only when it accepts one, so both shapes work.
    takes_query = len(inspect.signature(fetch).parameters) > 0
    raw = fetch(query) if takes_query else fetch()
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise RuntimeError(f"plugin {pid} fetch() must return a list")
    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        magnet = str(entry.get("magnet", "")).strip()
        url = str(entry.get("url", "")).strip()
        p = str(entry.get("path", "")).strip()
        if not name:
            continue
        if magnet:
            items.append({"name": name, "magnet": magnet})
        elif url:
            items.append({"name": name, "url": url})
        elif p:
            items.append({"name": name, "path": p})
    return items


def add_plugin(name: str, code: str, needs_query: bool = True) -> dict:
    """Save new plugin source as plugins/<slug>.py. The code is validated by
    compiling it (not executed) so a syntax error is caught before saving. The
    generated header documents the search(query, config) contract."""
    if not name.strip():
        raise ValueError("plugin name required")
    if not code.strip():
        raise ValueError("plugin code required")
    compile(code, f"<plugin {name}>", "exec")  # raises SyntaxError if bad
    PLUGIN_DIR.mkdir(exist_ok=True)
    pid = _slug(name)
    base = pid
    path = PLUGIN_DIR / f"{pid}.py"
    while path.exists():
        pid = f"{base}-{int(time.time())}"
        path = PLUGIN_DIR / f"{pid}.py"
    header = (
        f'"""{name} plugin.\n\n'
        "Define search(query, config) -> list[dict]. Each item is one of:\n"
        '  {"name": str, "url": str}      a PDF to open (downloaded on click)\n'
        '  {"name": str, "magnet": str}   a torrent to track\n'
        '  {"name": str, "path": str}     a local PDF to read in place\n'
        'Optionally add a "meta" string for a subtitle line, and define\n'
        'CONFIG = [ {"key","label","type","default","options"}, ... ] for settings.\n'
        f'"""\n'
        f'NAME = {name!r}\n'
        f'DESCRIPTION = "custom plugin"\n\n'
    )
    path.write_text(header + code, encoding="utf-8")
    return {"id": pid, "name": name, "path": str(path)}


def remove_plugin(pid: str) -> bool:
    path = PLUGIN_DIR / f"{pid}.py"
    if path.is_file():
        path.unlink()
        return True
    return False
