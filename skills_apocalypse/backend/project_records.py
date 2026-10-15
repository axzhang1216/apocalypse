#!/usr/bin/env python3
"""Project-local, lossless chat record maintenance for Apocalypse.

This module deliberately does not call an LLM. It tails the same normalized
user/assistant transcript formats used elsewhere in Apocalypse and maintains a
human-readable ``chat_history/record.md`` inside each project root.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path.home() / ".claude" / "apocalypse"
SETTINGS_FILE = DATA_DIR / "settings.json"
STATE_FILE = DATA_DIR / "project_records_state.json"
WORKSPACE_FILE = DATA_DIR / "workspace.json"
POLL_SECONDS = float(os.environ.get("APOCALYPSE_RECORD_POLL_SECONDS", "5"))
MAX_BYTES_PER_SOURCE = int(os.environ.get("APOCALYPSE_RECORD_READ_CHUNK", str(8 * 1024 * 1024)))

_PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
import sys  # noqa: E402
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))
import transcripts  # noqa: E402

_lock = threading.RLock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_wake = threading.Event()
_state: dict = {"version": 1, "sources": {}, "stats": {}}
_runtime = {
    "running": False,
    "last_sync_at": None,
    "last_error": None,
    "monitored_projects": 0,
    "tracked_sessions": 0,
    "messages_written": 0,
    "pending_messages": 0,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, fallback):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, type(fallback)) else fallback
    except Exception:
        return fallback


def enabled() -> bool:
    cfg = _read_json(SETTINGS_FILE, {})
    return bool(cfg.get("project_chat_records", True))


def set_enabled(value: bool) -> dict:
    with _lock:
        cfg = _read_json(SETTINGS_FILE, {})
        cfg["project_chat_records"] = bool(value)
        _atomic_json(SETTINGS_FILE, cfg)
        _wake.set()
    return status()


def _load_state() -> None:
    global _state
    data = _read_json(STATE_FILE, {})
    if data.get("version") == 1:
        _state = {
            "version": 1,
            "sources": data.get("sources") or {},
            "stats": data.get("stats") or {},
        }
    else:
        _state = {"version": 1, "sources": {}, "stats": {}}


def _save_state() -> None:
    _state["stats"] = {
        "last_sync_at": _runtime.get("last_sync_at"),
        "messages_written": int(_runtime.get("messages_written") or 0),
    }
    _atomic_json(STATE_FILE, _state)


def _norm_path(value) -> str:
    try:
        return os.path.normcase(os.path.abspath(os.path.expanduser(str(value))))
    except Exception:
        return str(value or "")


def _is_within(child: Path, parent: Path) -> bool:
    try:
        c = _norm_path(child)
        p = _norm_path(parent)
        return c == p or c.startswith(p.rstrip("\\/") + os.sep)
    except Exception:
        return False


def _workspace_roots() -> list[Path]:
    ws = _read_json(WORKSPACE_FILE, {})
    roots: list[Path] = []
    for key, project in (ws.get("projects") or {}).items():
        raw = (project or {}).get("cwd") or key
        if not raw:
            continue
        try:
            p = Path(str(raw)).expanduser()
            if p.exists() and p.is_dir():
                roots.append(p.resolve())
        except Exception:
            continue
    roots.sort(key=lambda p: len(_norm_path(p)), reverse=True)
    return roots


def _project_root(cwd: str, workspace_roots: list[Path]) -> Path | None:
    if not cwd:
        return None
    try:
        p = Path(cwd).expanduser().resolve(strict=False)
    except Exception:
        return None
    for root in workspace_roots:
        if _is_within(p, root):
            return root
    for candidate in [p, *p.parents]:
        try:
            if (candidate / ".git").exists():
                return candidate
        except Exception:
            break
    return p if p.exists() and p.is_dir() else None


def _fmt_time(ts) -> str:
    try:
        dt = transcripts._ts_to_dt(ts)
        if dt is not None:
            return dt.astimezone().strftime("%Y/%m/%d %H:%M")
    except Exception:
        pass
    return datetime.now().astimezone().strftime("%Y/%m/%d %H:%M")


def _agent_label(agent: str) -> str:
    return {
        "claude": "Claude",
        "codex": "Codex",
        "grok": "Grok",
        "hermes": "Hermes",
        "openclaw": "OpenClaw",
        "pi": "Pi",
    }.get((agent or "").lower(), (agent or "Assistant").strip().title())


def _message_fingerprint(agent: str, session_id: str, entry: dict) -> str:
    raw = "\0".join([
        agent or "", session_id or "", str(entry.get("role") or ""),
        str(entry.get("ts") or ""), str(entry.get("text") or ""),
    ])
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:20]


def _load_markers(record_path: Path) -> set[str]:
    if not record_path.exists():
        return set()
    out: set[str] = set()
    try:
        with open(record_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("<!-- apocalypse-msg:"):
                    token = line[len("<!-- apocalypse-msg:"):].split(" -->", 1)[0].strip()
                    if token:
                        out.add(token)
    except Exception:
        pass
    return out


def _ensure_history_dir(root: Path) -> Path:
    history = root / "chat_history"
    history.mkdir(parents=True, exist_ok=True)
    ignore = history / ".gitignore"
    if not ignore.exists():
        try:
            ignore.write_text("*\n!.gitignore\n", encoding="utf-8")
        except OSError:
            pass
    record = history / "record.md"
    if not record.exists():
        record.write_text("# Conversation Record\n", encoding="utf-8")
    return record


def _append_entries(record_path: Path, agent: str, session_id: str, entries: list[dict]) -> int:
    if not entries:
        return 0
    existing = _load_markers(record_path)
    fresh: list[tuple[str, dict]] = []
    for entry in entries:
        if entry.get("role") not in ("user", "assistant") or not str(entry.get("text") or "").strip():
            continue
        fp = _message_fingerprint(agent, session_id, entry)
        if fp not in existing:
            fresh.append((fp, entry))
            existing.add(fp)
    if not fresh:
        return 0

    label = _agent_label(agent)
    session_marker = f"<!-- apocalypse-session:{agent}:{session_id} -->"
    needs_header = True
    try:
        if record_path.exists():
            needs_header = session_marker not in record_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass

    chunks: list[str] = []
    if needs_header:
        chunks.append(f"\n---\n## [{_fmt_time(fresh[0][1].get('ts'))}] {label} Session\n{session_marker}\n")
    for fp, entry in fresh:
        role = "User" if entry.get("role") == "user" else label
        text = str(entry.get("text") or "").rstrip()
        chunks.append(f"\n<!-- apocalypse-msg:{fp} -->\n**{role}:** {text}\n")
    with open(record_path, "a", encoding="utf-8", newline="\n") as f:
        f.write("".join(chunks))
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    return len(fresh)


def _read_new_records(path: Path, agent: str, cursor: dict) -> tuple[list[dict], int, int]:
    try:
        size = path.stat().st_size
    except OSError:
        return [], int(cursor.get("offset") or 0), int(cursor.get("raw_lines") or 0)
    offset = int(cursor.get("offset") or 0)
    raw_lines = int(cursor.get("raw_lines") or 0)
    if size < offset:
        offset = 0
        raw_lines = 0
    if size == offset:
        return [], offset, raw_lines
    entries: list[dict] = []
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(MAX_BYTES_PER_SOURCE)
        end = f.tell()
    if data and not data.endswith(b"\n"):
        cut = data.rfind(b"\n")
        if cut < 0:
            return [], offset, raw_lines
        end = offset + cut + 1
        data = data[:cut + 1]
    for raw in data.splitlines():
        raw_lines += 1
        try:
            d = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            continue
        try:
            entry = transcripts.extract_transcript_line(agent, d)
        except Exception:
            entry = None
        if entry is not None:
            entries.append(entry)
    return entries, end, raw_lines


def sync_once() -> dict:
    if not enabled():
        with _lock:
            _runtime.update({"last_error": None, "pending_messages": 0})
        return status()

    workspace_roots = _workspace_roots()
    projects_seen: set[str] = set()
    sessions_seen: set[str] = set()
    written = 0
    errors: list[str] = []
    try:
        projects = transcripts.scan(include_live=True)
    except Exception as exc:
        with _lock:
            _runtime["last_error"] = f"transcript scan failed: {exc}"
        return status()

    for project in (projects or {}).values():
        cwd = str(project.get("cwd") or "")
        root = _project_root(cwd, workspace_roots)
        if root is None:
            continue
        try:
            record_path = _ensure_history_dir(root)
        except Exception as exc:
            errors.append(f"{root}: {exc}")
            continue
        projects_seen.add(_norm_path(root))
        for session in project.get("sessions") or []:
            path = Path(session.get("path") or "")
            if not path.is_file():
                continue
            agent = str(session.get("agent") or project.get("agent") or "")
            sid = str(session.get("id") or path.stem)
            sessions_seen.add(f"{agent}:{sid}")
            key = _norm_path(path)
            cursor = _state["sources"].setdefault(key, {
                "offset": 0, "raw_lines": 0, "agent": agent,
                "session_id": sid, "project_root": str(root),
            })
            try:
                entries, new_offset, raw_lines = _read_new_records(path, agent, cursor)
                count = _append_entries(record_path, agent, sid, entries)
                cursor.update({
                    "offset": new_offset, "raw_lines": raw_lines, "agent": agent,
                    "session_id": sid, "project_root": str(root), "updated_at": _now_iso(),
                })
                written += count
            except Exception as exc:
                errors.append(f"{agent}:{sid}: {exc}")

    with _lock:
        _runtime["last_sync_at"] = _now_iso()
        _runtime["monitored_projects"] = len(projects_seen)
        _runtime["tracked_sessions"] = len(sessions_seen)
        _runtime["messages_written"] = int(_runtime.get("messages_written") or 0) + written
        _runtime["pending_messages"] = 0
        _runtime["last_error"] = "; ".join(errors[:3]) if errors else None
        _save_state()
    return status()


def status() -> dict:
    with _lock:
        return {
            "enabled": enabled(),
            "running": bool(_runtime.get("running")),
            "last_sync_at": _runtime.get("last_sync_at"),
            "last_error": _runtime.get("last_error"),
            "monitored_projects": int(_runtime.get("monitored_projects") or 0),
            "tracked_sessions": int(_runtime.get("tracked_sessions") or 0),
            "messages_written": int(_runtime.get("messages_written") or 0),
            "pending_messages": int(_runtime.get("pending_messages") or 0),
            "record_name": "chat_history/record.md",
            "uses_llm": False,
        }


def _loop() -> None:
    with _lock:
        _runtime["running"] = True
    try:
        while not _stop.is_set():
            try:
                sync_once()
            except Exception as exc:
                with _lock:
                    _runtime["last_error"] = f"record monitor failed: {exc}"
            _wake.wait(POLL_SECONDS)
            _wake.clear()
    finally:
        with _lock:
            _runtime["running"] = False


def start() -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _load_state()
        stats = _state.get("stats") or {}
        _runtime["last_sync_at"] = stats.get("last_sync_at")
        _runtime["messages_written"] = int(stats.get("messages_written") or 0)
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="apocalypse-project-records", daemon=True)
        _thread.start()


def stop() -> None:
    _stop.set()
    _wake.set()
