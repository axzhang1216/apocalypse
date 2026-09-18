#!/usr/bin/env python3
"""One-way local AI-agent conversation archive for Apocalypse.

Goals:
- start asynchronously with Apocalypse and never delay the UI
- preserve native transcript files where possible
- export only conversation/session tables from SQLite-backed agents
- never copy auth/token/config stores wholesale
- incremental, non-destructive synchronization: source -> archive only

The archive root is configurable from Settings. Existing archives are never
removed when the root changes.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DATA_DIR = Path.home() / ".claude" / "apocalypse"
CONFIG_FILE = DATA_DIR / "storage.json"
STATUS_FILE = DATA_DIR / "storage_status.json"
DEFAULT_ROOT = DATA_DIR / "archive"
DEFAULT_INTERVAL_SECONDS = 300
COPY_CHUNK = 1024 * 1024

_lock = threading.Lock()
_start_lock = threading.Lock()
_started = False
_wakeup = threading.Event()
_runtime: dict[str, Any] = {
    "running": False,
    "last_sync_at": None,
    "last_error": None,
    "copied_files": 0,
    "unchanged_files": 0,
    "exported_databases": 0,
    "bytes_copied": 0,
    "agents": {},
}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _expand_path(value: Any) -> Path:
    raw = os.path.expandvars(str(value or "").strip())
    if not raw:
        return DEFAULT_ROOT
    return Path(raw).expanduser().resolve(strict=False)


def config() -> dict[str, Any]:
    raw = _read_json(CONFIG_FILE)
    cfg = raw if isinstance(raw, dict) else {}
    interval = cfg.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    try:
        interval = max(60, int(interval))
    except Exception:
        interval = DEFAULT_INTERVAL_SECONDS
    return {
        "root": str(_expand_path(cfg.get("root") or DEFAULT_ROOT)),
        "enabled": bool(cfg.get("enabled", True)),
        "interval_seconds": interval,
    }


def set_root(root: str) -> dict[str, Any]:
    path = _expand_path(root)
    if not str(root or "").strip():
        raise ValueError("Storage folder cannot be empty.")
    # The archive is data, not an agent's live state directory.
    live_roots = [
        Path.home() / ".claude" / "projects",
        Path.home() / ".codex" / "sessions",
        Path.home() / ".pi" / "agent" / "sessions",
    ]
    for live in live_roots:
        try:
            if live.exists() and (path == live or live in path.parents):
                raise ValueError(f"Storage folder cannot be inside live agent sessions: {live}")
        except OSError:
            pass
    path.mkdir(parents=True, exist_ok=True)
    cfg = config()
    cfg["root"] = str(path)
    _write_json(CONFIG_FILE, cfg)
    request_sync()
    return status()


def _status_from_disk() -> dict[str, Any]:
    x = _read_json(STATUS_FILE)
    return x if isinstance(x, dict) else {}


def status() -> dict[str, Any]:
    cfg = config()
    previous = _status_from_disk()
    with _lock:
        runtime = dict(_runtime)
    merged = {**previous, **runtime}
    merged.update({
        "root": cfg["root"],
        "enabled": cfg["enabled"],
        "interval_seconds": cfg["interval_seconds"],
        "default_root": str(DEFAULT_ROOT),
    })
    return merged


def _sig(path: Path) -> dict[str, int]:
    st = path.stat()
    return {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}


def _same_sig(a: Any, b: Any) -> bool:
    return isinstance(a, dict) and isinstance(b, dict) and a.get("size") == b.get("size") and a.get("mtime_ns") == b.get("mtime_ns")


def _manifest_path(root: Path) -> Path:
    return root / "_meta" / "manifest.json"


def _manifest(root: Path) -> dict[str, Any]:
    x = _read_json(_manifest_path(root))
    return x if isinstance(x, dict) else {"version": 1, "files": {}, "databases": {}}


def _safe_component(value: str) -> str:
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad else c for c in value)
    return out.strip(" .") or "default"


def _atomic_copy(src: Path, dst: Path) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name("." + dst.name + f".apoc-{os.getpid()}.tmp")
    total = 0
    try:
        with open(src, "rb") as r, open(tmp, "wb") as w:
            while True:
                chunk = r.read(COPY_CHUNK)
                if not chunk:
                    break
                w.write(chunk)
                total += len(chunk)
            w.flush()
            os.fsync(w.fileno())
        try:
            shutil.copystat(src, tmp)
        except Exception:
            pass
        os.replace(tmp, dst)
        return total
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _iter_regular(root: Path, patterns: Iterable[str]) -> Iterable[Path]:
    if not root.exists():
        return
    seen: set[Path] = set()
    for pattern in patterns:
        try:
            for p in root.glob(pattern):
                try:
                    if p.is_file() and not p.is_symlink():
                        rp = p.resolve(strict=False)
                        if rp not in seen:
                            seen.add(rp)
                            yield p
                except OSError:
                    continue
        except OSError:
            continue


def _copy_group(agent: str, root: Path, patterns: Iterable[str], prefix: Path, manifest: dict[str, Any], stats: dict[str, Any]) -> None:
    files = manifest.setdefault("files", {})
    for src in _iter_regular(root, patterns):
        try:
            rel = src.relative_to(root)
        except ValueError:
            rel = Path(src.name)
        key = f"{agent}|{src.resolve(strict=False)}"
        try:
            before = _sig(src)
        except OSError:
            continue
        dst = prefix / agent / rel
        if _same_sig(files.get(key), before) and dst.exists():
            stats["unchanged_files"] += 1
            stats["agents"].setdefault(agent, {"copied": 0, "unchanged": 0, "databases": 0})["unchanged"] += 1
            continue
        try:
            copied = _atomic_copy(src, dst)
            after = _sig(src)
            stats["copied_files"] += 1
            stats["bytes_copied"] += copied
            stats["agents"].setdefault(agent, {"copied": 0, "unchanged": 0, "databases": 0})["copied"] += 1
            # Only mark stable files as synchronized. Active JSONL files that
            # changed during copy are picked up again on the next pass.
            if _same_sig(before, after):
                files[key] = after
        except Exception as exc:
            stats["errors"].append(f"{agent}: {src}: {type(exc).__name__}")


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__base64__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (str, int, float)) or value is None:
        return value
    return str(value)


def _conversation_tables(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT name, COALESCE(sql,'') FROM sqlite_master WHERE type='table'").fetchall()
    allow = ("session", "message", "transcript", "chat", "conversation")
    deny = ("auth", "credential", "secret", "token", "apikey", "api_key", "profile", "memory", "embedding")
    out = []
    for name, sql in rows:
        low = str(name).lower()
        if low.startswith("sqlite_") or not any(x in low for x in allow) or any(x in low for x in deny):
            continue
        if "virtual table" in str(sql).lower() or any(low.endswith(x) for x in ("_fts", "_data", "_idx", "_docsize", "_config")):
            continue
        out.append((str(name), str(sql)))
    return out


def _redacted_columns(columns: list[str]) -> set[int]:
    deny = ("password", "secret", "token", "credential", "api_key", "apikey", "access_key", "refresh")
    return {i for i, name in enumerate(columns) if any(x in str(name).lower() for x in deny)}


def _export_sqlite(agent: str, label: str, src: Path, dst_root: Path, manifest: dict[str, Any], stats: dict[str, Any]) -> None:
    try:
        before = _sig(src)
    except OSError:
        return
    key = f"{agent}|sqlite|{src.resolve(strict=False)}"
    dbs = manifest.setdefault("databases", {})
    out_dir = dst_root / agent / _safe_component(label)
    if _same_sig(dbs.get(key), before) and out_dir.exists():
        stats["agents"].setdefault(agent, {"copied": 0, "unchanged": 0, "databases": 0})["unchanged"] += 1
        return

    try:
        uri = "file:" + src.resolve(strict=False).as_posix() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=4)
        conn.execute("PRAGMA query_only=ON")
        tables = _conversation_tables(conn)
        schema_meta = []
        out_dir.mkdir(parents=True, exist_ok=True)
        for table, sql in tables:
            info = conn.execute(f'PRAGMA table_info("{table.replace(chr(34), chr(34)*2)}")').fetchall()
            columns = [str(x[1]) for x in info]
            redact = _redacted_columns(columns)
            tmp = out_dir / f".{_safe_component(table)}.jsonl.tmp"
            final = out_dir / f"{_safe_component(table)}.jsonl"
            quoted = table.replace('"', '""')
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                cur = conn.execute(f'SELECT * FROM "{quoted}"')
                for row in cur:
                    obj = {}
                    for i, col in enumerate(columns):
                        obj[col] = "[REDACTED]" if i in redact and row[i] is not None else _jsonable(row[i])
                    f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, final)
            schema_meta.append({"table": table, "columns": columns, "create_sql": sql})
        conn.close()
        _write_json(out_dir / "_schema.json", {
            "source_kind": "sqlite-conversation-export",
            "source_name": src.name,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "tables": schema_meta,
        })
        after = _sig(src)
        stats["exported_databases"] += 1
        stats["agents"].setdefault(agent, {"copied": 0, "unchanged": 0, "databases": 0})["databases"] += 1
        if _same_sig(before, after):
            dbs[key] = after
    except Exception as exc:
        stats["errors"].append(f"{agent}: {src}: sqlite export {type(exc).__name__}")


def _hermes_roots() -> list[Path]:
    explicit = os.environ.get("HERMES_HOME", "").strip()
    if explicit:
        return [Path(explicit).expanduser()]
    roots = [Path.home() / ".hermes"]
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        roots.insert(0, Path(os.environ["LOCALAPPDATA"]) / "hermes")
    # Preserve order while deduplicating.
    out = []
    for p in roots:
        if p not in out:
            out.append(p)
    return out


def _sync_sources(root: Path, manifest: dict[str, Any], stats: dict[str, Any]) -> None:
    chat_root = root / "agent-chats"
    home = Path.home()

    claude_root = Path(os.environ.get("CLAUDE_CONFIG_DIR", "") or (home / ".claude")).expanduser()
    _copy_group("claude", claude_root, ("projects/**/*.jsonl", "history.jsonl"), chat_root, manifest, stats)

    codex_root = Path(os.environ.get("CODEX_HOME", "") or (home / ".codex")).expanduser()
    _copy_group("codex", codex_root, ("sessions/**/*.jsonl", "archived_sessions/**/*.jsonl", "session_index.jsonl"), chat_root, manifest, stats)

    pi_override = os.environ.get("PI_CODING_AGENT_SESSION_DIR", "").strip()
    pi_root = Path(pi_override).expanduser() if pi_override else home / ".pi" / "agent" / "sessions"
    _copy_group("pi", pi_root, ("**/*.jsonl",), chat_root / "pi", manifest, stats)

    # Hermes canonical messages are SQLite-backed. Export only conversation
    # tables, never the whole database (which may contain unrelated state).
    for i, hroot in enumerate(_hermes_roots()):
        if not hroot.exists():
            continue
        tag = "default" if i == 0 else f"root-{i+1}"
        if (hroot / "state.db").exists():
            _export_sqlite("hermes", tag, hroot / "state.db", chat_root, manifest, stats)
        for profile_db in hroot.glob("profiles/*/state.db"):
            _export_sqlite("hermes", "profile-" + profile_db.parent.name, profile_db, chat_root, manifest, stats)
        _copy_group("hermes", hroot, ("sessions/saved/*.json", "sessions/**/*.jsonl"), chat_root, manifest, stats)

    # OpenClaw keeps current transcripts beside auth/runtime state in per-agent
    # SQLite files. Export only conversation-like tables to avoid copying auth.
    oc_root = Path(os.environ.get("OPENCLAW_STATE_DIR", "") or (home / ".openclaw")).expanduser()
    for db in oc_root.glob("agents/*/agent/openclaw-agent.sqlite"):
        agent_id = db.parent.parent.name
        _export_sqlite("openclaw", agent_id, db, chat_root, manifest, stats)
    _copy_group("openclaw", oc_root, ("agents/*/sessions/**/*.jsonl", "agents/*/sessions/**/*.json", "sessions/**/*.jsonl", "sessions/**/*.json"), chat_root, manifest, stats)

    # Grok CLI does not guarantee a persistent transcript store, but archive it
    # when a local sessions directory exists. auth.json is deliberately excluded.
    grok_root = Path(os.environ.get("GROK_HOME", "") or (home / ".grok")).expanduser()
    _copy_group("grok", grok_root, ("sessions/**/*.jsonl", "sessions/**/*.json", "sessions/**/*.sqlite", "sessions/**/*.db"), chat_root, manifest, stats)


def sync_now() -> dict[str, Any]:
    if not _lock.acquire(blocking=False):
        return status()
    try:
        cfg = config()
        if not cfg["enabled"]:
            return status()
        root = Path(cfg["root"])
        root.mkdir(parents=True, exist_ok=True)
        with_running = dict(_runtime)
        with_running["running"] = True
        _runtime.update(with_running)

        manifest = _manifest(root)
        stats: dict[str, Any] = {
            "running": True,
            "last_sync_at": None,
            "last_error": None,
            "copied_files": 0,
            "unchanged_files": 0,
            "exported_databases": 0,
            "bytes_copied": 0,
            "agents": {},
            "errors": [],
        }
        try:
            _sync_sources(root, manifest, stats)
            manifest["version"] = 1
            manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
            _write_json(_manifest_path(root), manifest)
        except Exception as exc:
            stats["errors"].append(type(exc).__name__)

        stats["running"] = False
        stats["last_sync_at"] = datetime.now(timezone.utc).isoformat()
        if stats["errors"]:
            stats["last_error"] = "; ".join(stats["errors"][:5])
        _runtime.update(stats)
        _write_json(STATUS_FILE, stats)
        return status()
    finally:
        _runtime["running"] = False
        _lock.release()


def request_sync() -> dict[str, Any]:
    _wakeup.set()
    if not _started:
        start_background_sync()
    return status()


def _loop() -> None:
    # First pass immediately after Apocalypse starts.
    sync_now()
    while True:
        cfg = config()
        interval = cfg["interval_seconds"]
        _wakeup.wait(interval)
        _wakeup.clear()
        sync_now()


def start_background_sync() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
        threading.Thread(target=_loop, daemon=True, name="apocalypse-chat-archive").start()


if __name__ == "__main__":
    print(json.dumps(sync_now(), ensure_ascii=False, indent=2))
