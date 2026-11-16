"""Desktop shell for the PDF torrent reader.

A native window (pywebview / system WebView2) that:
  1. shows a profile picker (static/profiles.html) on launch,
  2. on selection, spawns the FastAPI server as a subprocess with
     ``PTR_PROFILE=<name>`` so all data is scoped to ``profiles/<name>/``,
  3. navigates the same window to the running app.

Switching profiles (from the app header) stops the server, returns to the
picker, and starts a fresh server for the chosen profile. Closing the window
stops the server.

Run with:  python desktop.py

Frozen (PyInstaller) build: the same exe is re-spawned as the server via
``--server <profile>``; all mutable data lives in PTR_DATA_DIR (set below to
%APPDATA%/Mutuo) so the install dir stays read-only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import webview

FROZEN = getattr(sys, "frozen", False)


def _app_dir() -> Path:
    """Where the app's read-only files (static/, plugins/) live. When frozen,
    PyInstaller stages them in sys._MEIPASS (the bundle dir), not next to the
    exe."""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).parent


def _data_dir() -> Path:
    """Where mutable data (profiles/) lives. The frozen app points this at a
    per-user, platform-appropriate location; a dev checkout uses the repo
    root."""
    if FROZEN:
        if os.name == "nt":
            base = Path(os.environ.get("APPDATA") or Path.home())
            d = base / "Mutuo"
            # one-time migration from the pre-rename data dir, so existing
            # profiles carry over instead of starting empty
            old = base / "SharedKnowledge"
            if old.is_dir() and not d.exists():
                old.rename(d)
        elif sys.platform == "darwin":
            d = Path.home() / "Library" / "Application Support" / "Mutuo"
        else:
            base = Path(os.environ.get("XDG_DATA_HOME")
                        or (Path.home() / ".local" / "share"))
            d = base / "Mutuo"
    else:
        d = Path(__file__).parent
    d.mkdir(parents=True, exist_ok=True)
    return d


DATA_DIR = _data_dir()
# profiles.py / plugin_loader.py read PTR_DATA_DIR at import time, so it must
# be set BEFORE they're imported.
os.environ.setdefault("PTR_DATA_DIR", str(DATA_DIR))

import profiles  # noqa: E402  (after PTR_DATA_DIR is set)


def _enable_dpi_awareness() -> None:
    # Without this, Windows bitmap-stretches the whole window on a scaled
    # display (125%/150%), which is why the app looks blurry. Marking the
    # process per-monitor DPI-aware makes WebView2 render at native pixels.
    # Must run before the window is created.
    if os.name != "nt":
        return
    try:
        import ctypes
        # PER_MONITOR_DPI_AWARE_V2 = -4; fall back to system-aware (1) on
        # older Windows that don't know the per-monitor context.
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_enable_dpi_awareness()

APP_DIR = _app_dir()
HOST = "127.0.0.1"
PORT = 8080
APP_URL = f"http://{HOST}:{PORT}/"
PROFILES_URL = (APP_DIR / "static" / "profiles.html").as_uri()
READY_TIMEOUT = 30.0  # libtorrent session init + catalog sync can take a few s


# ---- server subprocess management -----------------------------------------

_server_proc: subprocess.Popen | None = None
_server_lock = threading.Lock()
_active_profile: str | None = None  # profile the running server serves


def _log_path() -> Path:
    return Path(tempfile.gettempdir()) / "pdf-reader-server.log"


def _kill_port(port: int) -> None:
    """Best-effort: kill whatever is listening on ``port`` (a stale server)."""
    if os.name != "nt":
        # lsof is the portable way to find a port's owner on POSIX; if it's
        # missing we just skip (a stale server then fails to bind and the
        # new one reports it).
        try:
            out = subprocess.check_output(
                ["lsof", "-ti", f"tcp:{port}"], text=True, errors="ignore")
        except (OSError, subprocess.CalledProcessError):
            return
        for pid in out.split():
            if pid.isdigit() and int(pid) != os.getpid():
                try:
                    os.kill(int(pid), 15)
                except OSError:
                    pass
        return
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"], text=True, errors="ignore"
        )
    except (OSError, subprocess.CalledProcessError):
        return
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
            pids.add(parts[4])
    for pid in pids:
        if pid.isdigit() and int(pid) != os.getpid():
            try:
                subprocess.run(["taskkill", "/F", "/PID", pid],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass


def _wait_ready(port: int, timeout: float = READY_TIMEOUT) -> bool:
    url = f"http://{HOST}:{port}/api/profile"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def start_server(profile_name: str) -> bool:
    """Spawn the uvicorn server for ``profile_name`` and wait until it's up.

    Frozen: re-spawn this same exe with ``--server <profile>`` (it runs the
    FastAPI app in-process). Dev: spawn ``python -m uvicorn app:app``.
    """
    global _server_proc
    with _server_lock:
        _stop_locked()
        _kill_port(PORT)
        time.sleep(0.3)
        env = dict(os.environ)
        env["PTR_PROFILE"] = profile_name
        env["PTR_DATA_DIR"] = str(DATA_DIR)
        log = open(_log_path(), "ab")
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        if FROZEN:
            cmd = [sys.executable, "--server", profile_name]
        else:
            cmd = [sys.executable, "-m", "uvicorn", "app:app",
                   "--host", HOST, "--port", str(PORT)]
        _server_proc = subprocess.Popen(
            cmd, cwd=str(APP_DIR), env=env, stdout=log, stderr=subprocess.STDOUT, **kwargs,
        )
        ok = _wait_ready(PORT)
        if not ok:
            _stop_locked()
        return ok


def stop_server() -> None:
    with _server_lock:
        _stop_locked()


def _stop_locked() -> None:
    global _server_proc
    proc, _server_proc = _server_proc, None
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except OSError:
            pass


# ---- JS bridge (exposed to the picker + app as window.pywebview.api) -------

class ProfileAPI:
    def __init__(self, window):
        self._window = window

    def list_profiles(self) -> list[dict]:
        return profiles.profiles.list()

    def create_profile(self, name: str, password: str = "") -> dict:
        try:
            profiles.profiles.create(name, password or "")
            return {"ok": True}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def delete_profile(self, name: str) -> dict:
        try:
            profiles.profiles.delete(name)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def _navigate_later(self, url: str) -> None:
        # Navigate on a separate thread *after* this method returns, so the
        # return value is delivered to the current page before it's replaced
        # (navigating immediately makes pywebview try to resolve the promise
        # on the old, now-gone page and log a harmless JavascriptException).
        def _go():
            time.sleep(0.3)
            self._window.load_url(url)
        threading.Thread(target=_go, daemon=True).start()

    def select_profile(self, name: str, password: str = "") -> dict:
        global _active_profile
        if not profiles.profiles.verify(name, password or ""):
            return {"ok": False, "error": "Wrong password."}
        if not start_server(name):
            return {"ok": False, "error": "Server failed to start — see the log."}
        _active_profile = name
        self._navigate_later(APP_URL)
        return {"ok": True}

    def switch_profile(self) -> dict:
        """Return to the profile picker (stops the current server)."""
        global _active_profile
        stop_server()
        _active_profile = None
        self._navigate_later(PROFILES_URL)
        return {"ok": True}

    def active_profile(self) -> dict:
        return {"name": _active_profile}

    def rename_profile(self, name: str, new_name: str) -> dict:
        """Rename the active profile. The server is stopped first (its data
        dir can't be moved while files are open), then restarted."""
        global _active_profile
        if name != _active_profile:
            return {"ok": False, "error": "Only the active profile can be renamed."}
        stop_server()
        try:
            profiles.profiles.rename(name, new_name)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not start_server(new_name):
            return {"ok": False, "error": "Server failed to restart — see the log."}
        _active_profile = new_name
        self._navigate_later(APP_URL)  # reload so the UI shows the new name
        return {"ok": True}

    def set_profile_password(self, current: str, new: str) -> dict:
        """Change (or clear, new="") the active profile's password."""
        global _active_profile
        if not _active_profile:
            return {"ok": False, "error": "No active profile."}
        try:
            profiles.profiles.set_password(_active_profile, current or "", new or "")
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}


# ---- update check ----------------------------------------------------------
# Compare the running version (version.py) against the latest GitHub release
# tag and offer to download it. Runs once per launch on a background thread
# and never blocks or crashes the app — a network failure just means no
# prompt. The frozen app has no git, so this uses the GitHub API, not git.

RELEASE_API = "https://api.github.com/repos/giangamberi/mutuo/releases/latest"
RELEASE_URL = "https://github.com/giangamberi/mutuo/releases/latest"


def _version_tuple(v: str) -> tuple:
    out = []
    for part in v.split("."):
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out)


def check_for_updates() -> None:
    """Background thread: prompt for an update if a newer release is out."""
    try:
        import version
        current = version.__version__
        req = urllib.request.Request(
            RELEASE_API,
            headers={"User-Agent": "mutuo/1.0",
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        latest = (data.get("tag_name") or "").lstrip("v")
        if not latest or _version_tuple(latest) <= _version_tuple(current):
            return
        if os.name != "nt":
            return
        import ctypes
        MB_YESNO = 0x00000004
        ans = ctypes.windll.user32.MessageBoxW(
            0,
            f"A new version of Mutuo is available: {latest} "
            f"(you have {current}).\n\nClick Yes to open the download page.",
            "Mutuo — update available", MB_YESNO)
        if ans == 1:  # IDYES
            webbrowser.open(RELEASE_URL)
    except Exception:
        pass  # offline / API down / not Windows — never disturb the user


# ---- window ----------------------------------------------------------------

def run_server(profile_name: str) -> None:
    """Frozen ``--server`` mode: run the FastAPI app in-process (the GUI
    process spawns us this way)."""
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, log_level="warning")


def main() -> None:
    # frozen server subprocess: no GUI, just serve and block
    if FROZEN and len(sys.argv) >= 3 and sys.argv[1] == "--server":
        run_server(sys.argv[2])
        return

    api = None
    closing = {"flag": False}

    def on_close():
        if closing["flag"]:
            return
        closing["flag"] = True
        stop_server()

    api = ProfileAPI(window=None)  # window set below once it exists
    window = webview.create_window(
        "Mutuo",
        PROFILES_URL,
        width=1100,
        height=760,
        min_size=(760, 520),
        background_color="#14161a",
        js_api=api,
        # pywebview defaults to text_select=False, which injects
        # "body { user-select: none }" and blocks selecting/copying text in
        # the reader. Enable it so txt/HTML content can be selected (images
        # are unaffected — they have no text to select).
        text_select=True,
    )
    api._window = window
    window.events.closing += on_close

    # offer an update if a newer release is out (background, non-blocking)
    threading.Thread(target=check_for_updates, daemon=True).start()

    webview.start()
    # webview.start() returns when the window is closed
    stop_server()


if __name__ == "__main__":
    main()
