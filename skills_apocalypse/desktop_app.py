#!/usr/bin/env python3
"""Windows desktop shell for Apocalypse Spatial OS."""
from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

import webview

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import app_lifecycle
import server as core
import spatial_server_plus as spatial

URL = f"http://localhost:{spatial.PORT}"
HEALTH_URL = URL + "/api/settings/status"


def _alive(timeout: float = 0.7) -> bool:
    """Cheap server readiness probe.

    Do not use /api/world here: building WORLD scans transcripts/workspace and
    made desktop startup do the expensive work once before the window opened,
    then again when the UI called refreshAll().
    """
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def _port_occupied() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=0.5) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def _start_server():
    if _alive():
        return None
    if _port_occupied():
        raise RuntimeError("Another Apocalypse process is already using port 7749. Close it, then reopen Apocalypse.")

    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    core.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    pid_file = core.DATA_DIR / "server.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    threading.Thread(target=core.broadcast_thread, daemon=True).start()
    server = spatial.http.server.ThreadingHTTPServer(("127.0.0.1", spatial.PORT), spatial.Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="apocalypse-http").start()

    deadline = time.time() + 5
    while time.time() < deadline:
        if _alive():
            return server
        time.sleep(0.08)
    server.shutdown()
    raise RuntimeError("Apocalypse Spatial OS failed to start on port 7749")


def _cleanup(server) -> None:
    if server is None:
        return
    try:
        server.shutdown()
        server.server_close()
    except Exception:
        pass
    try:
        pid_file = core.DATA_DIR / "server.pid"
        if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pid_file.unlink()
    except Exception:
        pass


def _watch_restart(window) -> None:
    """Close the desktop window after the web UI requests RESTART NOW."""
    app_lifecycle.wait_for_restart_request()
    try:
        window.destroy()
    except Exception:
        # If Edge/WebView is already shutting down, main() will still enter its
        # finally block and arm the staged updater.
        pass


def main() -> int:
    try:
        server = _start_server()
    except Exception as exc:
        webview.create_window(
            "Apocalypse — Startup Error",
            html=(
                "<body style='background:#17191D;color:#E6E2DA;font-family:Segoe UI;padding:28px'>"
                "<h2>Apocalypse could not start</h2>"
                f"<pre style='white-space:pre-wrap;color:#F3A1BD'>{str(exc)}</pre></body>"
            ),
            width=720,
            height=360,
            resizable=False,
        )
        webview.start(debug=False)
        return 1

    window = webview.create_window(
        "Apocalypse",
        URL,
        width=1600,
        height=1000,
        min_size=(1100, 700),
        background_color="#17191D",
        resizable=True,
        text_select=True,
    )

    def after(win) -> None:
        try:
            win.maximize()
        except Exception:
            pass
        threading.Thread(target=_watch_restart, args=(win,), daemon=True, name="apocalypse-restart-watch").start()

    try:
        webview.start(after, window, gui="edgechromium", debug=False, private_mode=False)
    finally:
        restart = app_lifecycle.restart_requested()
        _cleanup(server)
        # This only arms when a verified update is actually staged. The helper
        # runs detached, waits for this PID to disappear, then runs Inno Setup
        # with /VERYSILENT. Normal closes leave the app closed; RESTART NOW
        # launches the newly installed executable after setup succeeds.
        try:
            app_lifecycle.arm_update_on_exit(
                parent_pid=os.getpid(),
                restart=restart,
                app_executable=sys.executable,
            )
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
