#!/usr/bin/env python3
"""Apocalypse Spatial OS server extensions.

Adds mutating maintenance actions and personal plan quota adapters while
keeping the existing Spatial OS API/UI contracts unchanged.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import quota_adapters
import spatial_server as spatial

PORT = spatial.PORT
http = spatial.http
legacy = spatial.legacy

# spatial.ops() resolves its module-global `quotas` function at runtime. Replace
# that data source once here so both /api/ops and any internal refresh use the
# same real adapter without changing the existing LLM QUOTA UI contract.
spatial.quotas = quota_adapters.get_quotas


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _live_claude_process(session_id: str):
    """Return a live Claude process record for this session, if one exists."""
    session_dir = Path.home() / ".claude" / "sessions"
    if not session_dir.exists():
        return None
    for p in session_dir.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        sid = str(data.get("sessionId") or data.get("session_id") or "")
        if sid != session_id:
            continue
        pid = data.get("pid")
        if pid is None:
            try:
                pid = int(p.stem)
            except Exception:
                pid = None
        if pid and _pid_alive(pid):
            return {
                "pid": int(pid),
                "status": data.get("status") or "running",
                "waiting_for": data.get("waitingFor") or data.get("waiting_for") or "",
            }
    return None


def repair_conversation(session_id: str):
    """Remove invalid JSONL records from one Claude transcript, with backup.

    This implements the repair workflow documented by apocalypse-openclaw, but
    adds live-process protection, change detection, backup, and atomic replace.
    """
    if not session_id or "/" in session_id or "\\" in session_id or session_id.startswith("."):
        return None, "bad id", 400

    tpath = legacy._find_transcript_path(session_id)
    if not tpath:
        return None, "session transcript not found", 404

    live = _live_claude_process(session_id)
    if live:
        return None, (
            f"session is still open in Claude Code (pid {live['pid']}, "
            f"status {live['status']}); close/stop it before repair"
        ), 409

    try:
        before = tpath.stat()
        raw_lines = tpath.read_bytes().splitlines()
    except Exception as exc:
        return None, f"could not read transcript: {exc}", 500

    good = []
    bad = []
    nonempty = 0
    for idx, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            continue
        nonempty += 1
        try:
            text = raw.decode("utf-8")
            json.loads(text)
            good.append(raw)
        except Exception as exc:
            bad.append({"line": idx, "error": str(exc)[:160]})

    if not bad:
        return {
            "ok": True,
            "changed": False,
            "session_id": session_id,
            "total_records": nonempty,
            "valid_records": len(good),
            "removed_records": 0,
            "message": "Conversation file is already valid.",
        }, None, 200

    # Refuse to overwrite if something changed after our read.
    try:
        now = tpath.stat()
        if now.st_size != before.st_size or now.st_mtime_ns != before.st_mtime_ns:
            return None, "transcript changed while being inspected; repair aborted", 409
    except Exception as exc:
        return None, f"could not re-check transcript: {exc}", 500

    backup_dir = legacy.DATA_DIR / "repair_backups"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = backup_dir / f"{session_id}-{stamp}.jsonl"
    tmp = tpath.with_name(f".{tpath.name}.repair-{os.getpid()}.tmp")

    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tpath, backup)
        with open(tmp, "wb") as f:
            for raw in good:
                f.write(raw)
                f.write(b"\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, tpath)
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return None, f"repair write failed: {exc}", 500

    return {
        "ok": True,
        "changed": True,
        "session_id": session_id,
        "total_records": nonempty,
        "valid_records": len(good),
        "removed_records": len(bad),
        "removed": bad[:50],
        "backup": str(backup),
        "transcript": str(tpath),
    }, None, 200


class Handler(spatial.Handler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/quotas":
            # Keep the endpoint explicit so consumers can refresh plan capacity
            # without fetching the rest of OPS. Never return GProxy credentials.
            return self.send_json(quota_adapters.get_quotas())
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        prefix = "/api/sessions2/"
        suffix = "/repair"
        if path.startswith(prefix) and path.endswith(suffix):
            session_id = path[len(prefix):-len(suffix)]
            payload, error, status = repair_conversation(session_id)
            if error:
                return self.send_json({"ok": False, "error": error}, status)
            return self.send_json(payload, status)
        return super().do_POST()


if __name__ == "__main__":
    legacy.DATA_DIR.mkdir(parents=True, exist_ok=True)
    legacy.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    pid_file = legacy.DATA_DIR / "server.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    threading.Thread(target=legacy.broadcast_thread, daemon=True).start()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Apocalypse Spatial OS running at http://localhost:{PORT}", flush=True)
    try:
        srv.serve_forever()
    finally:
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass
