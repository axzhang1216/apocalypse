#!/usr/bin/env python3
"""Spatial OS runtime with project-local chat record monitoring."""
from __future__ import annotations

import os
import threading
from urllib.parse import urlparse

import project_records
import spatial_server_plus as base

PORT = base.PORT
http = base.http
core = base.core
RECORD_SETTINGS_JS = base._FRONT / "record_settings.js"


class Handler(base.Handler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            try:
                html = base.spatial.SPATIAL_HTML.read_text(encoding="utf-8")
                html = html.replace("</head>", '<link rel="stylesheet" href="/ops_polish.css"></head>')
                html = html.replace(
                    "</body>",
                    '<script src="/ops_polish.js"></script>'
                    '<script src="/onboarding_ui.js"></script>'
                    '<script src="/record_settings.js"></script></body>',
                )
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as exc:
                return self.send_json({"error": str(exc)}, 500)
        if path == "/record_settings.js":
            return self.static(RECORD_SETTINGS_JS, "application/javascript; charset=utf-8")
        if path == "/api/settings/project-records":
            return self.send_json({"ok": True, **project_records.status()})
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/settings/project-records":
            try:
                body = base._read_json_body(self)
                if "enabled" not in body:
                    return self.send_json({"ok": False, "error": "enabled is required"}, 400)
                return self.send_json({"ok": True, **project_records.set_enabled(bool(body.get("enabled")))})
            except ValueError as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 500)
        return super().do_POST()


def main() -> None:
    base.chat_archive.start_background_sync()
    try:
        import stream_watcher
        stream_watcher.start()
    except Exception:
        pass
    try:
        project_records.start()
    except Exception:
        pass
    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    core.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    pid = core.DATA_DIR / "server.pid"
    pid.write_text(str(os.getpid()), encoding="utf-8")
    threading.Thread(target=core.broadcast_thread, daemon=True).start()
    http.server.ThreadingHTTPServer.allow_reuse_address = False
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Apocalypse Spatial OS running at http://localhost:{PORT}", flush=True)
    try:
        srv.serve_forever()
    finally:
        try:
            pid.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
