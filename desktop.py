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


_PID_FILE = Path(tempfile.gettempdir()) / "pdf-reader-server.pid"
# cmdline fragments that identify one of OUR servers — used only as a fallback
# attribution (e.g. for a stale server written by a version without the
# pidfile). Kept specific on purpose: the port owner is only ever killed when
# we can say it is ours; a generic marker like "python" or the workspace path
# would let us kill unrelated processes (VS Code helpers carry the workspace
# path in their cmdline too).
_OURS_MARKERS = ("uvicorn app:app", "mutuo.exe", "desktop.py --server")


def _write_pidfile(pid: int) -> None:
    try:
        _PID_FILE.write_text(str(pid), encoding="ascii")
    except OSError:
        pass


def _find_port_pid(port: int) -> int | None:
    """The PID currently listening on ``port`` (None when the port is free)."""
    try:
        if os.name != "nt":
            # lsof is the portable way to find a port's owner on POSIX
            out = subprocess.check_output(
                ["lsof", "-ti", f"tcp:{port}"], text=True, errors="ignore", timeout=5)
            for tok in out.split():
                if tok.isdigit() and int(tok) != os.getpid():
                    return int(tok)
            return None
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"], text=True, errors="ignore", timeout=5)
    except (OSError, subprocess.CalledProcessError):
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
            if parts[4].isdigit() and int(parts[4]) != os.getpid():
                return int(parts[4])
    return None


def _alive(pid: int) -> bool:
    try:
        if os.name == "nt":
            # os.kill(pid, 0) is not a safe existence check on Windows
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                text=True, errors="ignore", timeout=5)
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _process_cmdline(pid: int) -> str:
    try:
        if os.name == "nt":
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                text=True, errors="ignore", timeout=5)
        else:
            out = subprocess.check_output(
                ["ps", "-o", "command=", "-p", str(pid)],
                text=True, errors="ignore", timeout=5)
        return (out or "").strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _is_ours(pid: int) -> bool:
    low = _process_cmdline(pid).lower()
    return bool(low) and any(m in low for m in _OURS_MARKERS)


def _terminate(pid: int) -> None:
    # /T kills the process *tree*: the python.exe we spawn on Windows is a
    # launcher whose child is the real interpreter (the one that binds the
    # port), and PyInstaller adds a bootloader layer too — killing just the
    # top process orphans the server and it keeps holding the port.
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            os.kill(pid, 15)
    except (OSError, subprocess.SubprocessError):
        pass


def _kill_pid(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            os.kill(pid, 9)
    except (OSError, subprocess.SubprocessError):
        pass


def _stop_stale_server() -> int | None:
    """Stop a Mutuo server left on the port by a previous launch.

    Only ever kills processes we can attribute to Mutuo: first via the
    pidfile written at spawn, then via a clearly-ours cmdline (covers stale
    servers from versions without the pidfile). A port owned by anything
    else is left alone — the caller refuses to start instead of force-
    killing a stranger's process.

    Returns the PID still occupying the port (None when it's free)."""
    # 1. our own previous server (pidfile)
    try:
        old = int(_PID_FILE.read_text().strip())
    except (OSError, ValueError):
        old = None
    else:
        _PID_FILE.unlink(missing_ok=True)
        if _alive(old) and _is_ours(old):
            _terminate(old)
            for _ in range(20):  # ~5 s for a clean shutdown
                if not _alive(old):
                    break
                time.sleep(0.25)
            if _alive(old):
                _kill_pid(old)
    # 2. whatever still owns the port
    pid = _find_port_pid(PORT)
    if pid is not None and _is_ours(pid):
        _terminate(pid)
        for _ in range(20):
            if not _alive(pid):
                break
            time.sleep(0.25)
        if _alive(pid):
            _kill_pid(pid)
        for _ in range(20):  # wait for the socket to be released
            if _find_port_pid(PORT) is None:
                return None
            time.sleep(0.25)
    return _find_port_pid(PORT)


def _wait_ready(port: int, profile: str, timeout: float = READY_TIMEOUT) -> bool:
    """Wait until the server answers AND reports the profile we just started.
    The name check matters: a 200 from a stale server (the kill missed, or
    the port was foreign) would otherwise let the window navigate to the
    wrong profile's data."""
    url = f"http://{HOST}:{port}/api/profile"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200 and json.loads(r.read()).get("name") == profile:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


_last_start_error = ""  # surfaced to the picker when start_server fails


def start_server(profile_name: str) -> bool:
    """Spawn the uvicorn server for ``profile_name`` and wait until it's up.

    Frozen: re-spawn this same exe with ``--server <profile>`` (it runs the
    FastAPI app in-process). Dev: spawn ``python -m uvicorn app:app``.
    Refuses to start (with a message) when the port is held by a process
    that is not a Mutuo server.
    """
    global _server_proc, _last_start_error
    with _server_lock:
        _stop_locked()
        _last_start_error = ""
        foreign = _stop_stale_server()
        if foreign:
            _last_start_error = (
                f"Port {PORT} is already in use by PID {foreign} (not a Mutuo "
                f"server) — close that program and try again.")
            print(f"server: {_last_start_error}")
            return False
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
        _write_pidfile(_server_proc.pid)
        ok = _wait_ready(PORT, profile_name)
        if not ok:
            _last_start_error = ("server did not report readiness within "
                                 f"{READY_TIMEOUT:.0f}s — see {_log_path()}")
            _stop_locked()
        return ok


def stop_server() -> None:
    with _server_lock:
        _stop_locked()


def _stop_locked() -> None:
    global _server_proc
    proc, _server_proc = _server_proc, None
    if proc and proc.poll() is None:
        # _terminate (taskkill /T), not proc.terminate(): the spawned
        # python.exe is a launcher and the real server is its child — see
        # _terminate for why only a tree kill actually stops it.
        _terminate(proc.pid)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_pid(proc.pid)
    # clear the pidfile, but only if it points at the process we just stopped
    # (a fresh start_server may have written a new one meanwhile)
    try:
        if proc is not None and int(_PID_FILE.read_text().strip()) == proc.pid:
            _PID_FILE.unlink(missing_ok=True)
    except (OSError, ValueError):
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
            return {"ok": False, "error": _last_start_error
                    or "Server failed to start — see the log."}
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
            return {"ok": False, "error": _last_start_error
                    or "Server failed to restart — see the log."}
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
