#!/usr/bin/env python3
"""Apocalypse server — stdlib only. Serves dashboard + SSE + transcript API."""

import http.server
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Load platform_utils from the same directory as this script.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from platform_utils import launch_in_terminal  # noqa: E402
import repair as apoc_repair  # noqa: E402

PORT = 7749
DATA_DIR = Path.home() / ".claude" / "apocalypse"
EVENTS_FILE = DATA_DIR / "events.jsonl"
SESSIONS_DIR = DATA_DIR / "sessions"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
DASHBOARD_FILE = Path(__file__).parent / "dashboard.html"
WORKSPACE_FILE = DATA_DIR / "workspace.json"
WORKSPACE_HTML = Path(__file__).parent / "workspace.html"

STALE_SECONDS = 86400  # 1 day
TRANSCRIPT_LIMIT = 50

CODEX_DIR = Path.home() / ".codex"
CODEX_SESSIONS_DIR = CODEX_DIR / "sessions"
CODEX_INDEX_FILE = CODEX_DIR / "session_index.jsonl"
CODEX_TRANSCRIPT_LIMIT = 200

# Records that aren't real conversation turns.
SKIP_TYPES = {
    "attachment",
    "file-history-snapshot",
    "last-prompt",
    "permission-mode",
    "ai-title",
    "queue-operation",
    "hook_success",
    "hook_failure",
    "system",
}

_sse_clients = []
_sse_lock = threading.Lock()


# ─────────────────────────── events.jsonl plumbing ────────────────────────────

def tail_events(last_pos=0):
    if not EVENTS_FILE.exists():
        return [], last_pos
    with open(EVENTS_FILE, "r", encoding="utf-8") as f:
        f.seek(last_pos)
        new = f.readlines()
        new_pos = f.tell()
    return [l.strip() for l in new if l.strip()], new_pos


def broadcast_thread():
    pos = EVENTS_FILE.stat().st_size if EVENTS_FILE.exists() else 0
    while True:
        time.sleep(0.5)
        lines, pos = tail_events(pos)
        if not lines:
            continue
        with _sse_lock:
            clients = list(_sse_clients)
        for c in clients:
            for line in lines:
                try:
                    c.put_nowait(f"data: {line}\n\n".encode("utf-8"))
                except queue.Full:
                    pass


def read_events(n=500):
    if not EVENTS_FILE.exists():
        return []
    with open(EVENTS_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    events = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            pass
    return events


# ─────────────────────────── transcript scanning ──────────────────────────────

def _project_name(cwd):
    if not cwd:
        return "unknown"
    s = str(cwd).replace("\\", "/").rstrip("/")
    if not s:
        return "unknown"
    return s.rsplit("/", 1)[-1]


def _read_tail_records(path, max_bytes=300_000, max_lines=80):
    """Seek to end and parse up to max_lines JSON records from the tail."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            sz = f.tell()
            start = max(0, sz - max_bytes)
            f.seek(start)
            chunk = f.read()
    except Exception:
        return []
    text = chunk.decode("utf-8", errors="replace")
    if start > 0:
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl + 1:]
    lines = text.split("\n")
    records = []
    for line in lines[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return records


def _ts_to_dt(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _determine_status(records, last_ts):
    """Three-state status from the tail records."""
    status = "grey"

    # 1) Last assistant record — if it contains tool_use, Claude is working.
    last_assistant = None
    for r in reversed(records):
        if r.get("type") == "assistant":
            last_assistant = r
            break
    if last_assistant:
        content = (last_assistant.get("message") or {}).get("content") or []
        if isinstance(content, list) and any(
            isinstance(c, dict) and c.get("type") == "tool_use" for c in content
        ):
            status = "green"

    # 2) Otherwise, classify by the last substantive record.
    if status == "grey":
        last_sub = None
        for r in reversed(records):
            if r.get("type", "") in SKIP_TYPES:
                continue
            last_sub = r
            break
        if last_sub:
            t = last_sub.get("type", "")
            content = (last_sub.get("message") or {}).get("content") or []
            if isinstance(content, list):
                if t == "assistant":
                    if any(isinstance(c, dict) and c.get("type") == "text" for c in content):
                        status = "yellow"
                    elif any(isinstance(c, dict) and c.get("type") == "thinking" for c in content):
                        status = "green"
                elif t == "user":
                    if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                        status = "green"

    # 3) Stale beats anything.
    dt = _ts_to_dt(last_ts)
    if dt is not None:
        try:
            now = datetime.now(timezone.utc)
            if (now - dt).total_seconds() > STALE_SECONDS:
                return "grey"
        except Exception:
            pass
    return status


def _analyze_transcript(path):
    cwd = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("cwd"):
                    cwd = d["cwd"]
                    break
    except Exception:
        return None

    tail = _read_tail_records(path)
    last_ts = None
    for r in reversed(tail):
        if r.get("timestamp"):
            last_ts = r["timestamp"]
            break
    if last_ts is None:
        try:
            mt = path.stat().st_mtime
            last_ts = datetime.fromtimestamp(mt, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            last_ts = ""

    status = _determine_status(tail, last_ts)

    return {
        "session_id": path.stem,
        "cwd": cwd or "",
        "project_name": _project_name(cwd),
        "last_ts": last_ts,
        "status": status,
        "resume_id": path.stem,
    }


def scan_transcripts(limit=TRANSCRIPT_LIMIT):
    if not PROJECTS_DIR.exists():
        return []
    results = []
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            try:
                meta = _analyze_transcript(jsonl)
                if meta:
                    results.append(meta)
            except Exception:
                continue
    results.sort(key=lambda r: r.get("last_ts") or "", reverse=True)
    return results[:limit]


# ──────────────────────────── codex transcript scanning ──────────────────────

def _codex_thread_names():
    """Return {session_id: thread_name} from ~/.codex/session_index.jsonl.
    Empty dict if the file is missing or unreadable."""
    names = {}
    if not CODEX_INDEX_FILE.exists():
        return names
    try:
        with open(CODEX_INDEX_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                sid = d.get("id")
                if sid and d.get("thread_name"):
                    names[sid] = d["thread_name"]
    except Exception:
        pass
    return names


def _read_codex_session_meta(path):
    """Read the first session_meta record's payload from a rollout file.
    Returns the payload dict, or None if not found / unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") == "session_meta":
                    return d.get("payload") or {}
                # session_meta is always the first record; stop early.
                break
    except Exception:
        return None
    return None


def scan_codex_transcripts(limit=CODEX_TRANSCRIPT_LIMIT):
    """List Codex sessions (most-recent first) by reading each rollout's
    session_meta. Returns [] if ~/.codex/sessions is absent."""
    if not CODEX_SESSIONS_DIR.exists():
        return []
    names = _codex_thread_names()
    results = []
    for jsonl in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
        if not jsonl.is_file():
            continue
        meta = _read_codex_session_meta(jsonl)
        if not meta:
            continue
        sid = meta.get("id") or jsonl.stem
        cwd = meta.get("cwd") or ""
        results.append({
            "session_id": sid,
            "cwd": cwd,
            "project_name": _project_name(cwd),
            "last_ts": meta.get("timestamp") or "",
            "thread_name": names.get(sid),
            "originator": meta.get("originator") or "",
        })
    results.sort(key=lambda r: r.get("last_ts") or "", reverse=True)
    return results[:limit]


def _find_transcript_path(session_id):
    if not PROJECTS_DIR.exists():
        return None
    if "/" in session_id or "\\" in session_id or session_id.startswith("."):
        return None
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        cand = proj_dir / (session_id + ".jsonl")
        if cand.exists():
            return cand
    return None


_NOISE_PREFIXES_SERVER = (
    "<local-command-caveat>", "<local-command-stdout>",
    "<command-message>", "<command-name>", "<command-args>",
    "<bash-input>", "<bash-stdout>", "<bash-stderr>",
    "You are running as a local coding agent for a Multica",
    "You are running as a chat assistant for a Multica",
    "<persisted-output>",
)


def _is_noise_user(d):
    """Check if a user record is noise (not natural language from a human)."""
    if d.get("isMeta"):
        return True
    msg = d.get("message") or {}
    content = msg.get("content", [])
    if isinstance(content, str):
        text = content.strip()
        if text and any(text.startswith(p) for p in _NOISE_PREFIXES_SERVER):
            return True
        return False
    if not isinstance(content, list):
        return True
    # All tool_results = noise (not human language)
    if all(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
        return True
    # Check text content for noise prefixes
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            text = (c.get("text") or "").strip()
            if text and any(text.startswith(p) for p in _NOISE_PREFIXES_SERVER):
                return True
    return False


def parse_conversation(path):
    """Walk the transcript and produce a list of user/assistant/tool messages."""
    msgs = []
    tool_idx_by_id = {}

    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except Exception:
        return []

    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type", "")
            if t in SKIP_TYPES:
                continue
            ts = d.get("timestamp", "")
            msg = d.get("message") or {}
            content = msg.get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list):
                continue

            if t == "user":
                is_noise = _is_noise_user(d)
                tool_results = [c for c in content
                                if isinstance(c, dict) and c.get("type") == "tool_result"]
                # Only add user text if NOT noise
                if not is_noise:
                    texts = [c.get("text", "") for c in content
                             if isinstance(c, dict) and c.get("type") == "text"]
                    if texts:
                        msgs.append({
                            "role": "user",
                            "ts": ts,
                            "text": "\n".join(t for t in texts if t),
                        })
                # Always process tool results (link to tool_use or add as tool msg)
                for tr in tool_results:
                    tuid = tr.get("tool_use_id")
                    out = tr.get("content", "")
                    if isinstance(out, list):
                        out = "\n".join(
                            c.get("text", "") for c in out
                            if isinstance(c, dict) and c.get("type") == "text"
                        )
                    if not isinstance(out, str):
                        out = json.dumps(out, ensure_ascii=False)
                    if tuid and tuid in tool_idx_by_id:
                        idx = tool_idx_by_id[tuid]
                        msgs[idx]["output"] = out
                        msgs[idx]["is_error"] = bool(tr.get("is_error"))
                    else:
                        msgs.append({
                            "role": "tool",
                            "ts": ts,
                            "tool_use_id": tuid or "",
                            "tool": "",
                            "input": "",
                            "output": out,
                            "is_error": bool(tr.get("is_error")),
                        })

            elif t == "assistant":
                texts = [c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text"]
                tool_uses = [c for c in content
                             if isinstance(c, dict) and c.get("type") == "tool_use"]
                thinkings = [c.get("thinking", "") for c in content
                             if isinstance(c, dict) and c.get("type") == "thinking"]
                if texts:
                    text_joined = "\n".join(t for t in texts if t)
                    if text_joined.strip():
                        msgs.append({"role": "assistant", "ts": ts, "text": text_joined})
                if thinkings and not texts:
                    thinking_joined = "\n".join(t for t in thinkings if t)
                    if thinking_joined.strip():
                        msgs.append({
                            "role": "thinking",
                            "ts": ts,
                            "text": thinking_joined,
                        })
                for tu in tool_uses:
                    tuid = tu.get("id") or ""
                    tool_name = tu.get("name", "")
                    inp = tu.get("input", {})
                    if not isinstance(inp, str):
                        try:
                            inp = json.dumps(inp, ensure_ascii=False, indent=2)
                        except Exception:
                            inp = str(inp)
                    entry = {
                        "role": "tool",
                        "ts": ts,
                        "tool_use_id": tuid,
                        "tool": tool_name,
                        "input": inp,
                        "output": None,
                        "is_error": False,
                    }
                    msgs.append(entry)
                    if tuid:
                        tool_idx_by_id[tuid] = len(msgs) - 1

    return msgs


def _find_codex_transcript(session_id):
    """Locate a Codex rollout file by session id (UUID). Returns Path or None.
    Rejects anything that looks like a path traversal attempt."""
    if not CODEX_SESSIONS_DIR.exists():
        return None
    if "/" in session_id or "\\" in session_id or not session_id or session_id.startswith("."):
        return None
    matches = list(CODEX_SESSIONS_DIR.rglob(f"*{session_id}*.jsonl"))
    return matches[0] if matches else None


def _export_to_cwd(session_id, text, kind):
    """Write text to <session_cwd>/<timestamp>_<sid>.txt and return the
    absolute output path. Returns (None, error_msg) on failure.

    kind='claude' uses _find_transcript_path + _analyze_transcript;
    kind='codex' uses _find_codex_transcript + _read_codex_session_meta.
    """
    if kind == "claude":
        tpath = _find_transcript_path(session_id)
        if not tpath:
            return None, "transcript not found"
        meta = _analyze_transcript(tpath)
        if not meta:
            return None, "could not analyze transcript"
        cwd = meta.get("cwd") or ""
    else:  # codex / pi / openclaw
        if kind == "codex":
            tpath = _find_codex_transcript(session_id)
        elif kind == "pi":
            tpath = _find_pi_style_transcript(PI_SESSIONS_DIR, session_id)
        else:
            tpath = _find_pi_style_transcript(OPENCLAW_AGENTS_DIR, session_id)
        if not tpath:
            return None, "transcript not found"
        meta = _read_codex_session_meta(tpath) if kind == "codex" else _read_pi_style_header(tpath)
        if not meta:
            return None, "could not read session metadata"
        cwd = meta.get("cwd") or ""
    if not cwd:
        return None, "no working directory recorded for this session"
    cwd_path = Path(cwd)
    if not cwd_path.is_dir():
        return None, f"working directory does not exist: {cwd}"
    now = datetime.now()
    stamp = f"{now.year:04d}-{now.month:02d}-{now.day:02d}T{now.hour:02d}-{now.minute:02d}-{now.second:02d}"
    target = cwd_path / f"{stamp}_{session_id}.txt"
    try:
        target.write_text(text, encoding="utf-8")
    except Exception as e:
        return None, f"write failed: {e}"
    return str(target), None


def parse_codex_conversation(path):
    """Parse a Codex rollout JSONL into Claude-compatible messages.

    Output schema matches parse_conversation(): {role, ts, text} for
    user/assistant and {role:'tool', ts, tool_use_id, tool, input, output,
    is_error} for tool calls. developer/reasoning records are skipped.
    """
    msgs = []
    tool_idx_by_call_id = {}

    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except Exception:
        return []

    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "response_item":
                continue
            p = d.get("payload") or {}
            ts = d.get("timestamp", "")
            ptype = p.get("type", "")
            role = p.get("role", "")

            if ptype == "message":
                if role not in ("user", "assistant"):
                    continue  # skip developer / unknown roles
                content = p.get("content") or []
                if not isinstance(content, list):
                    continue
                texts = [c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("text")]
                if texts:
                    joined = "\n".join(t for t in texts if t)
                    if joined.strip():
                        msgs.append({"role": role, "ts": ts, "text": joined})

            elif ptype in ("function_call", "custom_tool_call"):
                call_id = p.get("call_id") or ""
                name = p.get("name") or ""
                raw = p.get("arguments")
                if raw is None:
                    raw = p.get("input", "")
                inp = raw
                # Make shell commands readable: unwrap {"command": "..."}.
                if isinstance(raw, str) and raw.lstrip().startswith("{"):
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            inp = parsed.get("command") or parsed.get("query") or raw
                    except Exception:
                        pass
                if not isinstance(inp, str):
                    try:
                        inp = json.dumps(inp, ensure_ascii=False, indent=2)
                    except Exception:
                        inp = str(inp)
                entry = {
                    "role": "tool",
                    "ts": ts,
                    "tool_use_id": call_id,
                    "tool": name,
                    "input": inp,
                    "output": None,
                    "is_error": False,
                }
                msgs.append(entry)
                if call_id:
                    tool_idx_by_call_id[call_id] = len(msgs) - 1

            elif ptype in ("function_call_output", "custom_tool_call_output"):
                call_id = p.get("call_id") or ""
                out = p.get("output", "")
                if not isinstance(out, str):
                    try:
                        out = json.dumps(out, ensure_ascii=False)
                    except Exception:
                        out = str(out)
                if call_id and call_id in tool_idx_by_call_id:
                    msgs[tool_idx_by_call_id[call_id]]["output"] = out
                else:
                    msgs.append({
                        "role": "tool",
                        "ts": ts,
                        "tool_use_id": call_id,
                        "tool": "",
                        "input": "",
                        "output": out,
                        "is_error": False,
                    })

    return msgs


# ─────────────────────── pi / openclaw / hermes transcript sources ───────────────────────
# pi and openclaw share one session-jsonl format ("pi-style"):
# first record {type:'session', id, timestamp, cwd}; then type='message'
# records with message.role user/assistant/toolResult and content blocks
# text/thinking/toolCall/image. Hermes stores sessions in a SQLite db.

PI_SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"
OPENCLAW_AGENTS_DIR = Path.home() / ".openclaw" / "agents"
HERMES_DB = Path.home() / ".hermes" / "state.db"
OTHER_TRANSCRIPT_LIMIT = 200


def _iter_pi_style_files(root):
    if not root.exists():
        return
    for jsonl in root.rglob("*.jsonl"):
        if jsonl.is_file() and ".trajectory." not in jsonl.name:
            yield jsonl


def _read_pi_style_header(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            d = json.loads(f.readline())
        if d.get("type") == "session":
            return d
    except Exception:
        pass
    return None


def _pi_style_title(path):
    """First user text, truncated — used as a display title."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") != "message":
                    continue
                m = r.get("message") or {}
                if m.get("role") != "user":
                    continue
                for c in (m.get("content") if isinstance(m.get("content"), list) else []):
                    if c.get("type") == "text" and str(c.get("text") or "").strip():
                        return str(c["text"]).strip()[:80]
    except Exception:
        pass
    return None


def scan_pi_style_transcripts(root, limit=OTHER_TRANSCRIPT_LIMIT):
    results = []
    for jsonl in _iter_pi_style_files(root) or []:
        meta = _read_pi_style_header(jsonl)
        if not meta:
            continue
        sid = meta.get("id") or jsonl.stem
        cwd = meta.get("cwd") or ""
        results.append({
            "session_id": sid,
            "cwd": cwd,
            "project_name": _project_name(cwd),
            "last_ts": meta.get("timestamp") or "",
            "thread_name": _pi_style_title(jsonl),
        })
    results.sort(key=lambda r: r.get("last_ts") or "", reverse=True)
    return results[:limit]


def scan_pi_transcripts(limit=OTHER_TRANSCRIPT_LIMIT):
    return scan_pi_style_transcripts(PI_SESSIONS_DIR, limit)


def scan_openclaw_transcripts(limit=OTHER_TRANSCRIPT_LIMIT):
    return scan_pi_style_transcripts(OPENCLAW_AGENTS_DIR, limit)


def _find_pi_style_transcript(root, session_id):
    if "/" in session_id or "\\" in session_id or not session_id or session_id.startswith("."):
        return None
    for jsonl in _iter_pi_style_files(root) or []:
        if session_id in jsonl.stem:
            return jsonl
    return None


def parse_pi_style_conversation(path):
    """pi/openclaw session jsonl -> parse_conversation() schema.
    thinking/image blocks are skipped; toolCall records pair with the
    following toolResult via toolCallId."""
    msgs = []
    pending_tools = {}
    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except Exception:
        return []
    with f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") != "message":
                continue
            m = r.get("message") or {}
            role = m.get("role")
            ts = r.get("timestamp") or ""
            if role == "toolResult":
                text = " ".join(str(c.get("text") or "") for c in (m.get("content") or []) if isinstance(c, dict) and c.get("type") == "text")
                idx = pending_tools.get(m.get("toolCallId"))
                if idx is not None:
                    msgs[idx]["output"] = text
                    msgs[idx]["is_error"] = bool(m.get("isError"))
                else:
                    msgs.append({"role": "tool", "ts": ts, "tool_use_id": m.get("toolCallId") or "", "tool": m.get("toolName") or "", "input": "", "output": text, "is_error": bool(m.get("isError"))})
                continue
            if role not in ("user", "assistant"):
                continue
            texts = []
            for c in (m.get("content") if isinstance(m.get("content"), list) else []):
                if not isinstance(c, dict):
                    continue
                ct = c.get("type")
                if ct == "text" and str(c.get("text") or "").strip():
                    texts.append(str(c["text"]))
                elif ct == "toolCall":
                    msgs.append({"role": "tool", "ts": ts, "tool_use_id": c.get("id") or "", "tool": c.get("name") or "", "input": json.dumps(c.get("arguments") or {}, ensure_ascii=False), "output": None, "is_error": False})
                    pending_tools[c.get("id")] = len(msgs) - 1
            if texts:
                msgs.append({"role": role, "ts": ts, "text": "\n".join(texts)})
    return msgs


def _hermes_connect():
    if not HERMES_DB.exists():
        return None
    import sqlite3
    try:
        return sqlite3.connect("file:%s?mode=ro" % HERMES_DB, uri=True)
    except Exception:
        return None


def scan_hermes_transcripts(limit=OTHER_TRANSCRIPT_LIMIT):
    db = _hermes_connect()
    if not db:
        return []
    try:
        rows = db.execute(
            "SELECT s.id, s.source, s.display_name, MAX(m.timestamp) "
            "FROM sessions s LEFT JOIN messages m ON m.session_id = s.id "
            "GROUP BY s.id ORDER BY MAX(m.timestamp) DESC LIMIT ?",
            (limit if limit else 10 ** 9,)).fetchall()
        out = []
        for sid, source, display, last in rows:
            first = db.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='user' "
                "AND content NOT LIKE '[System:%' ORDER BY timestamp LIMIT 1", (sid,)).fetchone()
            title = first[0].strip()[:80] if first and first[0] else None
            ts = ""
            if last:
                ts = datetime.fromtimestamp(last, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            out.append({
                "session_id": sid,
                "cwd": "",
                "project_name": "hermes-" + (source or "unknown"),
                "last_ts": ts,
                "thread_name": title or display or sid,
            })
        return out
    except Exception:
        return []
    finally:
        db.close()


def parse_hermes_conversation(session_id):
    db = _hermes_connect()
    if not db:
        return []
    try:
        rows = db.execute(
            "SELECT role, content, tool_name, tool_calls, timestamp FROM messages "
            "WHERE session_id=? ORDER BY timestamp", (session_id,)).fetchall()
    except Exception:
        db.close()
        return []
    db.close()
    msgs = []
    for role, content, tool_name, tool_calls, ts in rows:
        iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z") if ts else ""
        text = str(content or "")
        if role == "user" and text.startswith("[System:"):
            continue
        if tool_name:
            msgs.append({"role": "tool", "ts": iso, "tool_use_id": "", "tool": tool_name,
                         "input": str(tool_calls or ""), "output": text or None, "is_error": False})
        elif role in ("user", "assistant") and text.strip():
            msgs.append({"role": role, "ts": iso, "text": text})
    return msgs


def delete_session_artifacts(session_id):
    """Remove snapshot files for session_id and filter events.jsonl. Leaves projects/ alone."""
    removed = {"snapshot_files": [], "events_dropped": 0}

    if SESSIONS_DIR.exists() and "/" not in session_id and "\\" not in session_id:
        for ext in (".json", ".jsonl"):
            p = SESSIONS_DIR / (session_id + ext)
            if p.exists():
                try:
                    p.unlink()
                    removed["snapshot_files"].append(p.name)
                except Exception:
                    pass

    if EVENTS_FILE.exists():
        kept = []
        dropped = 0
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                raw = line.rstrip("\n")
                try:
                    d = json.loads(raw)
                except Exception:
                    kept.append(raw)
                    continue
                if d.get("session_id") == session_id:
                    dropped += 1
                    continue
                kept.append(raw)
        if dropped:
            tmp = EVENTS_FILE.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for line in kept:
                    f.write(line + "\n")
            os.replace(tmp, EVENTS_FILE)
        removed["events_dropped"] = dropped

    return removed


# ─────────────────────── legacy snapshot file listing ─────────────────────────

def list_snapshots():
    if not SESSIONS_DIR.exists():
        return []
    files = sorted(
        list(SESSIONS_DIR.glob("*.json")) + list(SESSIONS_DIR.glob("*.jsonl")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [{
        "id": f.stem,
        "filename": f.name,
        "size": f.stat().st_size,
        "mtime": f.stat().st_mtime,
    } for f in files[:100]]


# ──────────────────────────────── workspace API ───────────────────────────────

def _load_workspace():
    if not WORKSPACE_FILE.exists():
        return None
    try:
        return json.loads(WORKSPACE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def workspace_status():
    ws = _load_workspace()
    if ws is None:
        return {"initialized": False, "project_count": 0, "session_count": 0}
    projects = ws.get("projects", {})
    session_count = sum(
        len(p.get("analyzed_sessions", {})) for p in projects.values()
    )
    return {
        "initialized": bool(ws.get("last_full_init")),
        "project_count": len(projects),
        "session_count": session_count,
    }


# ──────────────────────────── search & launch ─────────────────────────────────

def search_sessions(query, limit=20):
    """Full-text search across all session transcripts."""
    if not PROJECTS_DIR.exists():
        return []
    query_lower = query.lower()
    results = []
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            try:
                meta = _analyze_transcript(jsonl)
                if not meta:
                    continue
                matches = []
                cwd = meta.get("cwd", "")
                with open(jsonl, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        t = d.get("type", "")
                        if t not in ("user", "assistant"):
                            continue
                        msg = d.get("message") or {}
                        content = msg.get("content", [])
                        if isinstance(content, str):
                            content = [{"type": "text", "text": content}]
                        if not isinstance(content, list):
                            continue
                        for c in content:
                            if not isinstance(c, dict) or c.get("type") != "text":
                                continue
                            text = (c.get("text") or "")
                            # Sanitize: replace surrogates and control chars
                            text = text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
                            if query_lower in text.lower():
                                idx = text.lower().find(query_lower)
                                start = max(0, idx - 40)
                                end = min(len(text), idx + len(query) + 40)
                                snippet = ("..." if start > 0 else "") + text[start:end] + ("..." if end < len(text) else "")
                                matches.append({"role": t, "snippet": snippet})
                                if len(matches) >= 3:
                                    break
                        if len(matches) >= 3:
                            break
                if matches:
                    results.append({
                        "session_id": meta["session_id"],
                        "cwd": cwd,
                        "project_name": meta.get("project_name", ""),
                        "last_ts": meta.get("last_ts", ""),
                        "match_count": len(matches),
                        "matches": matches,
                    })
            except Exception:
                continue
    results.sort(key=lambda r: r["match_count"], reverse=True)
    return results[:limit]


def get_compact_conversation(session_id):
    """Return filtered conversation: user messages + assistant text only.
    Long assistant messages (>100 chars) are summarized via Haiku and cached."""
    # Check cache in workspace.json
    ws = _load_workspace()
    cache_key = f"compact_{session_id}"
    if ws:
        cached = ws.get("_compact_cache", {}).get(cache_key)
        if cached:
            return cached

    tpath = _find_transcript_path(session_id)
    if not tpath:
        return []

    # Filter to user + assistant text only
    messages = []
    try:
        with open(tpath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type", "")
                if t not in ("user", "assistant"):
                    continue
                if _is_noise_user(d) if t == "user" else False:
                    continue
                msg = d.get("message") or {}
                content = msg.get("content", [])
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                if not isinstance(content, list):
                    continue
                texts = [c.get("text", "").strip() for c in content
                         if isinstance(c, dict) and c.get("type") == "text" and (c.get("text") or "").strip()]
                if texts:
                    combined = "\n".join(texts)
                    messages.append({"role": t, "text": combined})
    except Exception:
        return []

    # Summarize long assistant messages via Haiku
    long_msgs = [(i, m) for i, m in enumerate(messages)
                 if m["role"] == "assistant" and len(m["text"]) > 100]

    if long_msgs:
        try:
            import anthropic
            client = anthropic.Anthropic()
            for idx, m in long_msgs:
                try:
                    resp = client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=256,
                        messages=[{"role": "user", "content":
                            f"Summarize this Claude Code reply in 1-2 sentences in the same language as the original:\n\n{m['text'][:1500]}"}],
                    )
                    summary = ""
                    for block in resp.content:
                        if hasattr(block, "text"):
                            summary = block.text.strip()
                            break
                    if summary:
                        messages[idx]["summary"] = summary
                except Exception:
                    pass
        except ImportError:
            pass

    # Cache result
    if ws is not None:
        if "_compact_cache" not in ws:
            ws["_compact_cache"] = {}
        ws["_compact_cache"][cache_key] = messages
        # Only keep last 50 cached conversations
        cache = ws["_compact_cache"]
        if len(cache) > 50:
            oldest = sorted(cache.keys())[:len(cache) - 50]
            for k in oldest:
                del cache[k]
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = WORKSPACE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(ws, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, WORKSPACE_FILE)

    return messages


def _launch_in_terminal(command):
    """Launch a command in a new terminal window/tab.

    Delegates to platform_utils for actual OS-specific behaviour. Kept
    as a thin wrapper so existing call sites in this file don't change.
    """
    launch_in_terminal(command)


# ──────────────────────────────── HTTP handler ────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/":
            if DASHBOARD_FILE.exists():
                body = DASHBOARD_FILE.read_bytes()
            else:
                body = b"<h1>Apocalypse</h1><p>dashboard.html not found</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/events":
            self.send_json(read_events())

        elif path == "/api/sessions":
            self.send_json(list_snapshots())

        elif path.startswith("/api/sessions/"):
            name = path[len("/api/sessions/"):]
            if "/" in name or "\\" in name or name.startswith("."):
                self.send_json({"error": "bad name"}, 400)
                return
            fpath = SESSIONS_DIR / name
            if fpath.exists() and fpath.suffix in (".json", ".jsonl"):
                body = fpath.read_bytes()
                self.send_response(200)
                ctype = "application/json" if fpath.suffix == ".json" else "application/x-ndjson"
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json({"error": "not found"}, 404)

        elif path == "/api/sessions2":
            # limit=None -> results[:None] returns the full list (no 50 cap);
            # pagination is handled client-side in dashboard.html.
            self.send_json(scan_transcripts(limit=None))

        elif path == "/api/codex/sessions":
            self.send_json(scan_codex_transcripts())

        elif path.startswith("/api/codex/sessions/"):
            session_id = path[len("/api/codex/sessions/"):]
            if "/" in session_id or "\\" in session_id or not session_id or session_id.startswith("."):
                self.send_json({"error": "bad id"}, 400)
                return
            tpath = _find_codex_transcript(session_id)
            if not tpath:
                self.send_json({"error": "not found"}, 404)
                return
            self.send_json(parse_codex_conversation(tpath))

        elif path == "/api/pi/sessions":
            self.send_json(scan_pi_transcripts())

        elif path == "/api/openclaw/sessions":
            self.send_json(scan_openclaw_transcripts())

        elif path == "/api/hermes/sessions":
            self.send_json(scan_hermes_transcripts())

        elif path.startswith("/api/pi/sessions/") or path.startswith("/api/openclaw/sessions/"):
            provider = "pi" if path.startswith("/api/pi/") else "openclaw"
            session_id = path[len("/api/" + provider + "/sessions/"):]
            if "/" in session_id or "\\" in session_id or not session_id or session_id.startswith("."):
                self.send_json({"error": "bad id"}, 400)
                return
            root = PI_SESSIONS_DIR if provider == "pi" else OPENCLAW_AGENTS_DIR
            tpath = _find_pi_style_transcript(root, session_id)
            if not tpath:
                self.send_json({"error": "not found"}, 404)
                return
            self.send_json(parse_pi_style_conversation(tpath))

        elif path.startswith("/api/hermes/sessions/"):
            session_id = path[len("/api/hermes/sessions/"):]
            if "/" in session_id or "\\" in session_id or not session_id or session_id.startswith("."):
                self.send_json({"error": "bad id"}, 400)
                return
            self.send_json(parse_hermes_conversation(session_id))

        elif path.startswith("/api/sessions2/search"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            query = (qs.get("q") or [""])[0].strip()
            if not query:
                self.send_json({"error": "missing q parameter"}, 400)
                return
            limit = int((qs.get("limit") or ["20"])[0])
            self.send_json(search_sessions(query, limit))

        elif path == "/api/workspace/status":
            self.send_json(workspace_status())

        elif path == "/api/workspace":
            ws = _load_workspace()
            if ws is None:
                self.send_json({})
            else:
                self.send_json(ws)

        elif path == "/workspace.html":
            if WORKSPACE_HTML.exists():
                body = WORKSPACE_HTML.read_bytes()
            else:
                body = b"<h1>workspace.html not found</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path in ("/three.module.min.js", "/OrbitControls.js"):
            fpath = Path(__file__).parent / path.lstrip("/")
            if fpath.exists():
                body = fpath.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json({"error": "not found"}, 404)

        elif path.startswith("/nebula_") and path.endswith(".png"):
            fpath = Path(__file__).parent / path.lstrip("/")
            if fpath.exists():
                body = fpath.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json({"error": "not found"}, 404)

        elif path in ("/mesh1.png", "/mesh2.png", "/mesh3.png"):
            fpath = Path(__file__).parent / path.lstrip("/")
            if fpath.exists():
                body = fpath.read_bytes()
                ct = "image/png" if path.endswith(".png") else "image/jpeg"
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json({"error": "not found"}, 404)

        elif path.endswith("/repair/preview") and path.startswith("/api/sessions2/"):
            session_id = path[len("/api/sessions2/"):-len("/repair/preview")]
            if not session_id or "/" in session_id or "\\" in session_id or session_id.startswith("."):
                self.send_json({"ok": False, "error": "bad id"}, 400)
                return
            tpath = _find_transcript_path(session_id)
            if not tpath:
                self.send_json({"ok": False, "error": "transcript not found"}, 404)
                return
            try:
                size = os.stat(tpath).st_size
            except OSError:
                self.send_json({"ok": False, "error": "stat failed"}, 500)
                return
            if size > apoc_repair.MAX_BYTES:
                self.send_json({"ok": False, "error": "file too large", "size": size}, 413)
                return
            try:
                res = apoc_repair.scan_for_unsupported(str(tpath), strip_images=True)
            except Exception as e:  # noqa: BLE001
                self.send_json({"ok": False, "error": f"scan failed: {e}"}, 500)
                return
            res["ok"] = True
            self.send_json(res)

        elif path.endswith("/compact") and path.startswith("/api/sessions2/"):
            # GET /api/sessions2/<id>/compact — filtered + summarized conversation
            session_id = path[len("/api/sessions2/"):-len("/compact")]
            if not session_id:
                self.send_json({"error": "bad id"}, 400)
                return
            self.send_json(get_compact_conversation(session_id))

        elif path.startswith("/api/sessions2/"):
            session_id = path[len("/api/sessions2/"):]
            tpath = _find_transcript_path(session_id)
            if not tpath:
                self.send_json({"error": "not found"}, 404)
                return
            self.send_json(parse_conversation(tpath))

        elif path == "/events/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            q = queue.Queue(maxsize=1024)
            with _sse_lock:
                _sse_clients.append(q)

            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                last_heartbeat = time.time()
                while True:
                    try:
                        payload = q.get(timeout=5)
                        self.wfile.write(payload)
                        self.wfile.flush()
                    except queue.Empty:
                        pass
                    if time.time() - last_heartbeat > 15:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        last_heartbeat = time.time()
            except Exception:
                pass
            finally:
                with _sse_lock:
                    if q in _sse_clients:
                        _sse_clients.remove(q)

        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/workspace/update":
            # Run incremental update and return results
            init_script = Path(__file__).parent / "workspace_init.py"
            try:
                import subprocess
                result = subprocess.run(
                    [sys.executable, str(init_script), "--incremental"],
                    capture_output=True, text=True, timeout=600,
                )
                events = []
                for line in result.stdout.strip().split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        continue

                done_evt = None
                project_evts = []
                for e in events:
                    if e.get("type") == "project_done":
                        project_evts.append(e)
                    elif e.get("type") == "done":
                        done_evt = e

                total_new = done_evt.get("total_sessions", 0) if done_evt else 0

                # Load workspace to get session details for updated projects
                ws = _load_workspace()
                project_details = []
                for evt in project_evts:
                    proj_name = evt.get("project", "")
                    new_count = evt.get("sessions", 0)
                    proj_record = None
                    for key, p in ws.get("projects", {}).items():
                        if p.get("name") == proj_name:
                            proj_record = p
                            break
                    sessions_detail = []
                    if proj_record:
                        analyzed = proj_record.get("analyzed_sessions", {})
                        sorted_sids = sorted(analyzed.keys(),
                                             key=lambda s: analyzed[s].get("ts", ""), reverse=True)
                        for sid in sorted_sids[:new_count]:
                            s = analyzed[sid]
                            sessions_detail.append({
                                "goal": s.get("user_goal", ""),
                                "summary": s.get("summary", ""),
                                "category": s.get("category", "other"),
                            })
                    project_details.append({
                        "name": proj_name,
                        "title": proj_record.get("title", "") if proj_record else "",
                        "new_sessions": new_count,
                        "sessions": sessions_detail,
                    })

                self.send_json({
                    "ok": True,
                    "total_new": total_new,
                    "projects_updated": len(project_evts),
                    "projects": project_details,
                })
            except subprocess.TimeoutExpired:
                self.send_json({"ok": False, "error": "Update timed out"}, 500)
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)

        elif path.startswith("/api/sessions2/launch/"):
            session_id = path[len("/api/sessions2/launch/"):]
            if "/" in session_id or "\\" in session_id or not session_id:
                self.send_json({"ok": False, "error": "bad id"}, 400)
                return
            # Find cwd from workspace.json or transcript
            ws = _load_workspace()
            cwd = ""
            if ws:
                for key, p in ws.get("projects", {}).items():
                    if session_id in p.get("analyzed_sessions", {}):
                        cwd = p.get("cwd", "")
                        break
            if not cwd:
                tpath = _find_transcript_path(session_id)
                if tpath:
                    try:
                        with open(tpath, "r", encoding="utf-8", errors="replace") as f:
                            for line in f:
                                try:
                                    d = json.loads(line)
                                except Exception:
                                    continue
                                if d.get("cwd"):
                                    cwd = d["cwd"]
                                    break
                    except Exception:
                        pass
            # Determine command: claude (default)
            import shutil
            claude_path = shutil.which("claude") or "claude"
            cmd = f'"{claude_path}" --resume {session_id} --dangerously-skip-permissions'
            if cwd:
                cmd = f'cd /d "{cwd}" && {cmd}'
            try:
                _launch_in_terminal(cmd)
                self.send_json({"ok": True, "cwd": cwd, "cmd": cmd})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)

        elif path.startswith("/api/auth/login/"):
            provider = path[len("/api/auth/login/"):]
            if provider not in ("codex", "grok"):
                self.send_json({"ok": False, "error": "unknown provider"}, 400)
                return
            import shutil
            if provider == "codex":
                exe = shutil.which("codex")
                if not exe:
                    self.send_json({"ok": False, "error": "codex CLI not found on PATH"}, 500)
                    return
                cmd = f'"{exe}" login'
            else:  # grok — binary at ~/.grok/bin/grok or PATH
                exe = shutil.which("grok") or str(Path.home() / ".grok" / "bin" / "grok.exe")
                if not Path(exe).exists():
                    self.send_json({"ok": False, "error": "grok CLI not found"}, 500)
                    return
                cmd = f'"{exe}" login'
            try:
                _launch_in_terminal(cmd)
                self.send_json({"ok": True, "provider": provider, "cmd": cmd})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)

        elif path.endswith("/repair") and path.startswith("/api/sessions2/"):
            session_id = path[len("/api/sessions2/"):-len("/repair")]
            if not session_id or "/" in session_id or "\\" in session_id or session_id.startswith("."):
                self.send_json({"ok": False, "error": "bad id"}, 400)
                return
            tpath = _find_transcript_path(session_id)
            if not tpath:
                self.send_json({"ok": False, "error": "transcript not found"}, 404)
                return
            try:
                body_len = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(body_len).decode("utf-8") if body_len else ""
                payload = json.loads(raw) if raw.strip() else {}
            except Exception:
                self.send_json({"ok": False, "error": "bad json body"}, 400)
                return
            strip_images = bool(payload.get("strip_images", True))
            mtime_before = payload.get("mtime")
            if mtime_before is None:
                self.send_json({"ok": False, "error": "mtime required"}, 400)
                return
            lock = apoc_repair.repair_lock_for(session_id)
            try:
                res = apoc_repair.repair_transcript(
                    str(tpath),
                    strip_images=strip_images,
                    mtime_before=mtime_before,
                    lock=lock,
                )
            except Exception as e:  # noqa: BLE001
                self.send_json({"ok": False, "error": f"repair failed: {e}"}, 500)
                return
            status = 200 if res.get("ok") else 500
            if res.get("error") == "mtime_changed":
                status = 409
            elif res.get("error") == "file too large":
                status = 413
            elif res.get("error") == "transcript not found":
                status = 404
            self.send_json(res, status=status)

        elif path.startswith("/api/sessions2/") and path.endswith("/export"):
            self._handle_export_post(path, "/api/sessions2/", "/export", "claude")

        elif path.startswith("/api/codex/sessions/") and path.endswith("/export"):
            self._handle_export_post(path, "/api/codex/sessions/", "/export", "codex")

        elif path.startswith("/api/pi/sessions/") and path.endswith("/export"):
            self._handle_export_post(path, "/api/pi/sessions/", "/export", "pi")

        elif path.startswith("/api/openclaw/sessions/") and path.endswith("/export"):
            self._handle_export_post(path, "/api/openclaw/sessions/", "/export", "openclaw")

        else:
            self.send_json({"error": "not found"}, 404)

    def _handle_export_post(self, path, prefix, suffix, kind):
        """POST handler for /api/{sessions2,codex/sessions}/<id>/export.
        Writes the request body (text/plain) to <session_cwd>/<timestamp>_<sid>.txt."""
        sid = path[len(prefix):-len(suffix)] if path.endswith(suffix) else ""
        if "/" in sid or "\\" in sid or sid.startswith(".") or not sid:
            self.send_json({"ok": False, "error": "bad id"}, 400)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            text = self.rfile.read(length).decode("utf-8") if length else ""
        except Exception:
            self.send_json({"ok": False, "error": "bad body"}, 400)
            return
        written, err = _export_to_cwd(sid, text, kind)
        if not written:
            self.send_json({"ok": False, "error": err or "export failed"}, 400)
            return
        self.send_json({"ok": True, "path": written})

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/sessions2/"):
            session_id = path[len("/api/sessions2/"):]
            if "/" in session_id or "\\" in session_id or not session_id:
                self.send_json({"error": "bad id"}, 400)
                return
            result = delete_session_artifacts(session_id)
            self.send_json({"ok": True, **result})
        else:
            self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    pid_file = DATA_DIR / "server.pid"
    pid_file.write_text(str(os.getpid()))

    t = threading.Thread(target=broadcast_thread, daemon=True)
    t.start()

    http.server.ThreadingHTTPServer.allow_reuse_address=False
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Apocalypse running at http://localhost:{PORT}", flush=True)
    try:
        server.serve_forever()
    finally:
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass
