#!/usr/bin/env python3
"""Windows desktop shell for Apocalypse Spatial OS.

The desktop app embeds the existing Spatial OS in a native application window
using Windows WebView2 via pywebview. It starts the local Apocalypse HTTP
server in-process when needed, but never runs workspace analysis or calls an
LLM just to launch the app.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

import webview

# Make sibling modules importable both from source and from PyInstaller.
BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import server as legacy  # noqa: E402
import spatial_server_plus as spatial  # noqa: E402

# Keep the exact same origin as the standalone/browser launcher so UI
# preferences (for example apocalypse.uiScale.v2) share one localStorage.
URL = f"http://localhost:{spatial.PORT}"


def _spatial_alive(timeout: float = 0.7) -> bool:
    try:
        with urllib.request.urlopen(URL + "/api/world", timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _port_is_apocalypse() -> bool:
    if _spatial_alive():
        return True
    try:
        with urllib.request.urlopen(URL + "/api/events", timeout=0.5) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _start_embedded_server():
    """Start Spatial OS in this process. Returns server or None if already up."""
    if _spatial_alive():
        return None
    if _port_is_apocalypse():
        raise RuntimeError(
            "Apocalypse legacy server is already running on port 7749. "
            "Stop it first, then reopen Apocalypse.exe."
        )

    legacy.DATA_DIR.mkdir(parents=True, exist_ok=True)
    legacy.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    pid_file = legacy.DATA_DIR / "server.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    threading.Thread(target=legacy.broadcast_thread, daemon=True).start()
    srv = spatial.http.server.ThreadingHTTPServer(("127.0.0.1", spatial.PORT), spatial.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="apocalypse-http").start()

    deadline = time.time() + 5.0
    while time.time() < deadline:
        if _spatial_alive():
            return srv
        time.sleep(0.08)
    srv.shutdown()
    raise RuntimeError("Apocalypse Spatial OS failed to start on port 7749")


def _cleanup_server(srv) -> None:
    if srv is None:
        return
    try:
        srv.shutdown()
        srv.server_close()
    except Exception:
        pass
    try:
        pid_file = legacy.DATA_DIR / "server.pid"
        if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pid_file.unlink()
    except Exception:
        pass


def main() -> int:
    try:
        srv = _start_embedded_server()
    except Exception as exc:
        webview.create_window(
            "Apocalypse — Startup Error",
            html=(
                "<body style='background:#17191D;color:#E6E2DA;font-family:Segoe UI;"
                "padding:28px'><h2>Apocalypse could not start</h2>"
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

    def _after_start(win):
        try:
            win.maximize()
        except Exception:
            pass

    try:
        webview.start(_after_start, window, gui="edgechromium", debug=False, private_mode=False)
    finally:
        _cleanup_server(srv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
