#!/usr/bin/env python3
"""Multi-agent transcript parser and scanner for Apocalypse.

Normalizes 6 agent formats (claude/codex/grok/hermes/openclaw/pi) into the shape
workspace_init.py already consumes. Pure parsing, no LLM calls.
"""
from __future__ import annotations
import json, os, re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SKIP_TYPES = {
    "attachment", "file-history-snapshot", "last-prompt", "permission-mode",
    "ai-title", "queue-operation", "hook_success", "hook_failure", "system",
}
NOISE_PREFIXES = (
    "<local-command-caveat>", "<local-command-stdout>",
    "<command-message>", "<command-name>", "<command-args>",
    "<bash-input>", "<bash-stdout>", "<bash-stderr>",
    "You are running as a local coding agent for a Multica",
    "You are running as a chat assistant for a Multica",
    "<persisted-output>",
)

def _ts_to_dt(ts: Any):
    if not ts:
        return None
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None

def _fmt_dt(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""

def _project_name(cwd: str) -> str:
    if not cwd:
        return "unknown"
    s = str(cwd).replace("\\", "/").rstrip("/")
    return s.rsplit("/", 1)[-1] if s else "unknown"

def _is_noise_user_text(text: str) -> bool:
    return any(text.strip().startswith(p) for p in NOISE_PREFIXES)

def _extract_text_blocks(content) -> list[str]:
    """Extract non-empty text from content (string or list of blocks)."""
    if isinstance(content, str):
        return [content.strip()] if content.strip() else []
    if not isinstance(content, list):
        return []
    out = []
    for c in content:
        if isinstance(c, dict):
            # Claude/Anthropic shape: {type:"text", text:"..."}
            if c.get("type") == "text" and (c.get("text") or "").strip():
                out.append(c["text"].strip())
            # Codex shape: {type:"input_text"|"output_text", ...} — text is the block itself
            elif c.get("type") in ("input_text", "output_text") and isinstance(c, str):
                out.append(c.strip())
        elif isinstance(c, str) and c.strip():
            out.append(c.strip())
    return out

# ponytail: parsers share ~70% structure but differ in enough detail that factoring out
# a single normalizer adds more branches than it saves; six 30-line functions < one 200-line router.

def _parse_claude(path: Path) -> dict[str, Any]:
    cwd, user_msgs, last_assistant_msg, tools_used = None, [], "", []
    msg_count, tool_call_count, first_ts, last_ts = 0, 0, None, None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                if cwd is None and d.get("cwd"):
                    cwd = d["cwd"]
                t = d.get("type", "")
                if t in SKIP_TYPES:
                    continue
                ts_dt = _ts_to_dt(d.get("timestamp"))
                if ts_dt:
                    if first_ts is None:
                        first_ts = ts_dt
                    last_ts = ts_dt
                if t == "user":
                    if d.get("isMeta"):
                        continue
                    msg = d.get("message") or {}
                    content = msg.get("content", [])
                    # Skip tool_result blocks
                    if isinstance(content, list) and any(
                        isinstance(c, dict) and c.get("type") == "tool_result" for c in content
                    ):
                        continue
                    texts = _extract_text_blocks(content)
                    if texts and not any(_is_noise_user_text(t) for t in texts):
                        user_msgs.append("\n".join(texts))
                        msg_count += 1
                elif t == "assistant":
                    msg = d.get("message") or {}
                    content = msg.get("content", [])
                    if isinstance(content, str):
                        content = [{"type": "text", "text": content}]
                    if not isinstance(content, list):
                        continue
                    texts = [c.get("text", "") for c in content
                             if isinstance(c, dict) and c.get("type") == "text" and (c.get("text") or "").strip()]
                    tool_uses = [c for c in content
                                 if isinstance(c, dict) and c.get("type") == "tool_use"]
                    if texts:
                        last_assistant_msg = texts[-1]
                        msg_count += 1
                    for tu in tool_uses:
                        tool_call_count += 1
                        name = tu.get("name", "")
                        if name and name not in tools_used:
                            tools_used.append(name)
    except Exception:
        pass
    trace_parts = []
    for i, um in enumerate(user_msgs[:3]):
        label = "User" if i == 0 else f"User[{i+1}]"
        trace_parts.append(f"[{label}] {um[:400]}")
    if last_assistant_msg:
        trace_parts.append(f"[Assistant] {last_assistant_msg[:400]}")
    return {
        "cwd": cwd or "",
        "first_user_msg": user_msgs[0][:2000] if user_msgs else "",
        "last_assistant_msg": last_assistant_msg[:2000],
        "conversation_trace": "\n".join(trace_parts),
        "tools_used": tools_used,
        "msg_count": msg_count,
        "tool_call_count": tool_call_count,
        "first_ts": _fmt_dt(first_ts),
        "last_ts": _fmt_dt(last_ts),
    }

def _parse_codex(path: Path) -> dict[str, Any]:
    cwd, user_msgs, last_assistant_msg, tools_used = None, [], "", []
    msg_count, tool_call_count, first_ts, last_ts = 0, 0, None, None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                if d.get("type") == "session_meta" and d.get("payload", {}).get("cwd"):
                    cwd = d["payload"]["cwd"]
                ts_dt = _ts_to_dt(d.get("timestamp"))
                if ts_dt:
                    if first_ts is None:
                        first_ts = ts_dt
                    last_ts = ts_dt
                if d.get("type") == "response_item":
                    payload = d.get("payload") or {}
                    if payload.get("type") != "message":
                        continue
                    role = payload.get("role", "")
                    if role in ("developer", "system"):
                        continue
                    content = payload.get("content", [])
                    texts = []
                    for c in content if isinstance(content, list) else []:
                        if isinstance(c, dict):
                            ct = c.get("type", "")
                            if ct == "input_text" and (c.get("text") or "").strip():
                                texts.append(c["text"].strip())
                            elif ct == "output_text" and (c.get("text") or "").strip():
                                texts.append(c["text"].strip())
                    if not texts:
                        continue
                    combined = "\n".join(texts)
                    if role == "user":
                        if not _is_noise_user_text(combined):
                            user_msgs.append(combined)
                            msg_count += 1
                    elif role == "assistant":
                        last_assistant_msg = combined
                        msg_count += 1
                # Codex tool tracking: event_msg.task_started
                if d.get("type") == "event_msg" and d.get("payload", {}).get("type") == "task_started":
                    tool = d["payload"].get("task_type", "")
                    if tool and tool not in tools_used:
                        tools_used.append(tool)
                        tool_call_count += 1
    except Exception:
        pass
    trace_parts = []
    for i, um in enumerate(user_msgs[:3]):
        label = "User" if i == 0 else f"User[{i+1}]"
        trace_parts.append(f"[{label}] {um[:400]}")
    if last_assistant_msg:
        trace_parts.append(f"[Assistant] {last_assistant_msg[:400]}")
    return {
        "cwd": cwd or "",
        "first_user_msg": user_msgs[0][:2000] if user_msgs else "",
        "last_assistant_msg": last_assistant_msg[:2000],
        "conversation_trace": "\n".join(trace_parts),
        "tools_used": tools_used,
        "msg_count": msg_count,
        "tool_call_count": tool_call_count,
        "first_ts": _fmt_dt(first_ts),
        "last_ts": _fmt_dt(last_ts),
    }

def _parse_grok(path: Path) -> dict[str, Any]:
    cwd, user_msgs, last_assistant_msg, tools_used = None, [], "", []
    msg_count, tool_call_count, first_ts, last_ts = 0, 0, None, None
    try:
        # Grok sessions are under sessions/<cwd-encoded>/chat_history.jsonl
        # Extract cwd from parent dir name (e.g. C%3A%5CUsers%5C... → C:\Users\...)
        parent = path.parent.name
        if parent and "%" in parent:
            import urllib.parse
            cwd = urllib.parse.unquote(parent)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                t = d.get("type", "")
                if t == "system":
                    continue
                ts_dt = _ts_to_dt(d.get("timestamp"))
                if ts_dt:
                    if first_ts is None:
                        first_ts = ts_dt
                    last_ts = ts_dt
                content = d.get("content", "")
                if not isinstance(content, str) or not content.strip():
                    continue
                if t == "user":
                    if not _is_noise_user_text(content):
                        user_msgs.append(content.strip())
                        msg_count += 1
                elif t == "assistant":
                    last_assistant_msg = content.strip()
                    msg_count += 1
                elif t == "tool_result":
                    tool_call_count += 1
    except Exception:
        pass
    trace_parts = []
    for i, um in enumerate(user_msgs[:3]):
        label = "User" if i == 0 else f"User[{i+1}]"
        trace_parts.append(f"[{label}] {um[:400]}")
    if last_assistant_msg:
        trace_parts.append(f"[Assistant] {last_assistant_msg[:400]}")
    return {
        "cwd": cwd or "",
        "first_user_msg": user_msgs[0][:2000] if user_msgs else "",
        "last_assistant_msg": last_assistant_msg[:2000],
        "conversation_trace": "\n".join(trace_parts),
        "tools_used": tools_used,
        "msg_count": msg_count,
        "tool_call_count": tool_call_count,
        "first_ts": _fmt_dt(first_ts),
        "last_ts": _fmt_dt(last_ts),
    }

def _parse_hermes(path: Path) -> dict[str, Any]:
    cwd, user_msgs, last_assistant_msg, tools_used = None, [], "", []
    msg_count, tool_call_count, first_ts, last_ts = 0, 0, None, None
    try:
        # Hermes messages.jsonl is sqlite export, one session per row
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                role = d.get("role", "")
                content = d.get("content", "")
                if not isinstance(content, str) or not content.strip():
                    continue
                ts_dt = _ts_to_dt(d.get("timestamp"))
                if ts_dt:
                    if first_ts is None:
                        first_ts = ts_dt
                    last_ts = ts_dt
                if role == "user":
                    if not _is_noise_user_text(content):
                        user_msgs.append(content.strip())
                        msg_count += 1
                elif role == "assistant":
                    last_assistant_msg = content.strip()
                    msg_count += 1
                if d.get("tool_calls"):
                    tool_call_count += 1
    except Exception:
        pass
    trace_parts = []
    for i, um in enumerate(user_msgs[:3]):
        label = "User" if i == 0 else f"User[{i+1}]"
        trace_parts.append(f"[{label}] {um[:400]}")
    if last_assistant_msg:
        trace_parts.append(f"[Assistant] {last_assistant_msg[:400]}")
    return {
        "cwd": cwd or "",
        "first_user_msg": user_msgs[0][:2000] if user_msgs else "",
        "last_assistant_msg": last_assistant_msg[:2000],
        "conversation_trace": "\n".join(trace_parts),
        "tools_used": tools_used,
        "msg_count": msg_count,
        "tool_call_count": tool_call_count,
        "first_ts": _fmt_dt(first_ts),
        "last_ts": _fmt_dt(last_ts),
    }

def _parse_openclaw_pi(path: Path) -> dict[str, Any]:
    cwd, user_msgs, last_assistant_msg, tools_used = None, [], "", []
    msg_count, tool_call_count, first_ts, last_ts = 0, 0, None, None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                if d.get("type") == "session" and d.get("cwd"):
                    cwd = d["cwd"]
                if d.get("type") != "message":
                    continue
                msg = d.get("message") or {}
                role = msg.get("role", "")
                content = msg.get("content")
                if not content:
                    continue
                ts_dt = _ts_to_dt(d.get("timestamp"))
                if ts_dt:
                    if first_ts is None:
                        first_ts = ts_dt
                    last_ts = ts_dt
                texts = _extract_text_blocks(content)
                if not texts:
                    continue
                combined = "\n".join(texts)
                if role == "user":
                    if not _is_noise_user_text(combined):
                        user_msgs.append(combined)
                        msg_count += 1
                elif role == "assistant":
                    last_assistant_msg = combined
                    msg_count += 1
                # Tool tracking: message.content blocks with type="tool_use"
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_use":
                            tool_call_count += 1
                            name = c.get("name", "")
                            if name and name not in tools_used:
                                tools_used.append(name)
    except Exception:
        pass
    trace_parts = []
    for i, um in enumerate(user_msgs[:3]):
        label = "User" if i == 0 else f"User[{i+1}]"
        trace_parts.append(f"[{label}] {um[:400]}")
    if last_assistant_msg:
        trace_parts.append(f"[Assistant] {last_assistant_msg[:400]}")
    return {
        "cwd": cwd or "",
        "first_user_msg": user_msgs[0][:2000] if user_msgs else "",
        "last_assistant_msg": last_assistant_msg[:2000],
        "conversation_trace": "\n".join(trace_parts),
        "tools_used": tools_used,
        "msg_count": msg_count,
        "tool_call_count": tool_call_count,
        "first_ts": _fmt_dt(first_ts),
        "last_ts": _fmt_dt(last_ts),
    }

def parse_transcript(path: Path, agent: str) -> dict[str, Any]:
    """Parse a transcript into workspace_init's expected shape.

    Returns dict with: cwd, first_user_msg, last_assistant_msg, conversation_trace,
    tools_used, msg_count, tool_call_count, first_ts, last_ts.
    """
    if agent == "claude":
        return _parse_claude(path)
    elif agent == "codex":
        return _parse_codex(path)
    elif agent == "grok":
        return _parse_grok(path)
    elif agent == "hermes":
        return _parse_hermes(path)
    elif agent in ("openclaw", "pi"):
        return _parse_openclaw_pi(path)
    else:
        raise ValueError(f"unsupported agent: {agent}")

def _parse_transcript_points_claude(path: Path) -> tuple[list, list]:
    user_msgs, transcript = [], []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line_no, raw in enumerate(f):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                t = d.get("type", "")
                if t not in ("user", "assistant"):
                    continue
                if t == "user" and d.get("isMeta"):
                    continue
                msg = d.get("message") or {}
                cb = msg.get("content", [])
                if isinstance(cb, str):
                    cb = [{"type": "text", "text": cb}]
                if not isinstance(cb, list):
                    continue
                if t == "user" and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in cb):
                    continue
                texts = [c.get("text", "").strip() for c in cb
                         if isinstance(c, dict) and c.get("type") == "text" and (c.get("text") or "").strip()]
                if not texts:
                    continue
                combined = "\n".join(texts)
                ts = d.get("timestamp", "") or ""
                entry = {"role": t, "ts": ts, "text": combined, "line_no": line_no}
                transcript.append(entry)
                if t == "user" and not _is_noise_user_text(combined):
                    user_msgs.append({**entry, "idx": len(user_msgs)})
    except Exception:
        return [], []
    return user_msgs, transcript

def _parse_transcript_points_codex(path: Path) -> tuple[list, list]:
    user_msgs, transcript = [], []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line_no, raw in enumerate(f):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                if d.get("type") != "response_item" or d.get("payload", {}).get("type") != "message":
                    continue
                payload = d["payload"]
                role = payload.get("role", "")
                if role in ("developer", "system"):
                    continue
                content = payload.get("content", [])
                texts = []
                for c in content if isinstance(content, list) else []:
                    if isinstance(c, dict):
                        ct = c.get("type", "")
                        if ct == "input_text" and (c.get("text") or "").strip():
                            texts.append(c["text"].strip())
                        elif ct == "output_text" and (c.get("text") or "").strip():
                            texts.append(c["text"].strip())
                if not texts:
                    continue
                combined = "\n".join(texts)
                ts = d.get("timestamp", "") or ""
                entry = {"role": role, "ts": ts, "text": combined, "line_no": line_no}
                transcript.append(entry)
                if role == "user" and not _is_noise_user_text(combined):
                    user_msgs.append({**entry, "idx": len(user_msgs)})
    except Exception:
        return [], []
    return user_msgs, transcript

def _parse_transcript_points_grok(path: Path) -> tuple[list, list]:
    user_msgs, transcript = [], []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line_no, raw in enumerate(f):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                t = d.get("type", "")
                if t not in ("user", "assistant"):
                    continue
                content = d.get("content", "")
                if not isinstance(content, str) or not content.strip():
                    continue
                ts = d.get("timestamp", "") or ""
                entry = {"role": t, "ts": ts, "text": content.strip(), "line_no": line_no}
                transcript.append(entry)
                if t == "user" and not _is_noise_user_text(content):
                    user_msgs.append({**entry, "idx": len(user_msgs)})
    except Exception:
        return [], []
    return user_msgs, transcript

def _parse_transcript_points_hermes(path: Path) -> tuple[list, list]:
    user_msgs, transcript = [], []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line_no, raw in enumerate(f):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                role = d.get("role", "")
                content = d.get("content", "")
                if not isinstance(content, str) or not content.strip():
                    continue
                ts = str(d.get("timestamp", ""))
                entry = {"role": role, "ts": ts, "text": content.strip(), "line_no": line_no}
                transcript.append(entry)
                if role == "user" and not _is_noise_user_text(content):
                    user_msgs.append({**entry, "idx": len(user_msgs)})
    except Exception:
        return [], []
    return user_msgs, transcript

def _parse_transcript_points_openclaw_pi(path: Path) -> tuple[list, list]:
    user_msgs, transcript = [], []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line_no, raw in enumerate(f):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                if d.get("type") != "message":
                    continue
                msg = d.get("message") or {}
                role = msg.get("role", "")
                content = msg.get("content")
                if not content:
                    continue
                texts = _extract_text_blocks(content)
                if not texts:
                    continue
                combined = "\n".join(texts)
                ts = d.get("timestamp", "") or ""
                entry = {"role": role, "ts": ts, "text": combined, "line_no": line_no}
                transcript.append(entry)
                if role == "user" and not _is_noise_user_text(combined):
                    user_msgs.append({**entry, "idx": len(user_msgs)})
    except Exception:
        return [], []
    return user_msgs, transcript

def parse_transcript_points(path: Path, agent: str) -> tuple[list, list]:
    """Parse a transcript for extract_points.

    Returns (user_msgs, transcript):
      - user_msgs: [{role, ts, text, line_no, idx}, ...]
      - transcript: [{role, ts, text, line_no}, ...] (includes assistant)
    """
    if agent == "claude":
        return _parse_transcript_points_claude(path)
    elif agent == "codex":
        return _parse_transcript_points_codex(path)
    elif agent == "grok":
        return _parse_transcript_points_grok(path)
    elif agent == "hermes":
        return _parse_transcript_points_hermes(path)
    elif agent in ("openclaw", "pi"):
        return _parse_transcript_points_openclaw_pi(path)
    else:
        raise ValueError(f"unsupported agent: {agent}")

def extract_transcript_line(agent: str, d: dict) -> dict | None:
    """Extract one normalized message {role, ts, text} from a raw JSONL record.

    Per-line companion to the whole-file _parse_transcript_points_* parsers.
    Extraction rules are identical; used by the live stream watcher so a single
    newly appended line can be normalized without re-parsing the whole file.
    Returns None for records that are not user/assistant text messages.
    """
    if agent == "claude":
        t = d.get("type", "")
        if t not in ("user", "assistant"):
            return None
        if t == "user" and d.get("isMeta"):
            return None
        msg = d.get("message") or {}
        cb = msg.get("content", [])
        if isinstance(cb, str):
            cb = [{"type": "text", "text": cb}]
        if not isinstance(cb, list):
            return None
        if t == "user" and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in cb):
            return None
        texts = [c.get("text", "").strip() for c in cb
                 if isinstance(c, dict) and c.get("type") == "text" and (c.get("text") or "").strip()]
        if not texts:
            return None
        return {"role": t, "ts": d.get("timestamp", "") or "", "text": "\n".join(texts)}
    if agent == "codex":
        if d.get("type") != "response_item" or (d.get("payload") or {}).get("type") != "message":
            return None
        payload = d["payload"]
        role = payload.get("role", "")
        if role in ("developer", "system"):
            return None
        content = payload.get("content", [])
        texts = []
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict):
                ct = c.get("type", "")
                if ct in ("input_text", "output_text") and (c.get("text") or "").strip():
                    texts.append(c["text"].strip())
        if not texts:
            return None
        return {"role": role, "ts": d.get("timestamp", "") or "", "text": "\n".join(texts)}
    if agent == "grok":
        t = d.get("type", "")
        if t not in ("user", "assistant"):
            return None
        content = d.get("content", "")
        if not isinstance(content, str) or not content.strip():
            return None
        return {"role": t, "ts": d.get("timestamp", "") or "", "text": content.strip()}
    if agent == "hermes":
        role = d.get("role", "")
        content = d.get("content", "")
        if not isinstance(content, str) or not content.strip():
            return None
        return {"role": role, "ts": str(d.get("timestamp", "")), "text": content.strip()}
    if agent in ("openclaw", "pi"):
        if d.get("type") != "message":
            return None
        msg = d.get("message") or {}
        role = msg.get("role", "")
        content = msg.get("content")
        if not content:
            return None
        texts = _extract_text_blocks(content)
        if not texts:
            return None
        return {"role": role, "ts": d.get("timestamp", "") or "", "text": "\n".join(texts)}
    return None


def scan(archive_root: Path | None = None, include_live: bool = True) -> dict[str, dict]:
    """Scan archive + live roots for all agents.

    Returns {project_key: {agent, cwd, name, sessions:[{id, path, agent}]}}.
    """
    projects = {}
    home = Path.home()

    # Live roots (same env-var/root logic as chat_archive)
    live_roots = {}
    if include_live:
        live_roots["claude"] = Path(os.environ.get("CLAUDE_CONFIG_DIR", "") or (home / ".claude")).expanduser() / "projects"
        live_roots["codex"] = Path(os.environ.get("CODEX_HOME", "") or (home / ".codex")).expanduser() / "sessions"
        pi_override = os.environ.get("PI_CODING_AGENT_SESSION_DIR", "").strip()
        live_roots["pi"] = Path(pi_override).expanduser() if pi_override else home / ".pi" / "agent" / "sessions"
        # Hermes: read from first root only (chat_archive exports all)
        hermes_explicit = os.environ.get("HERMES_HOME", "").strip()
        if hermes_explicit:
            live_roots["hermes"] = Path(hermes_explicit).expanduser()
        else:
            h = home / ".hermes"
            if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
                h = Path(os.environ["LOCALAPPDATA"]) / "hermes"
            live_roots["hermes"] = h
        live_roots["openclaw"] = Path(os.environ.get("OPENCLAW_STATE_DIR", "") or (home / ".openclaw")).expanduser()
        live_roots["grok"] = Path(os.environ.get("GROK_HOME", "") or (home / ".grok")).expanduser()

    # Scan archive
    if archive_root and archive_root.exists():
        chat_root = archive_root / "agent-chats"
        for agent in ["claude", "codex", "grok", "hermes", "openclaw", "pi"]:
            agent_dir = chat_root / agent
            if not agent_dir.exists():
                continue
            if agent == "claude":
                pattern = "projects/**/*.jsonl"
            elif agent == "codex":
                pattern = "sessions/**/*.jsonl"
            elif agent == "grok":
                pattern = "sessions/**/chat_history.jsonl"
            elif agent == "hermes":
                pattern = "**/messages.jsonl"
            elif agent in ("openclaw", "pi"):
                pattern = "**/*.jsonl"
            else:
                continue
            for p in agent_dir.glob(pattern):
                if not p.is_file():
                    continue
                # Skip trajectory files
                if agent == "openclaw" and "trajectory" in p.name:
                    continue
                session_id = p.stem if agent != "grok" else p.parent.name
                # Peek cwd from first record
                cwd = ""
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as f:
                        for raw in f:
                            try:
                                d = json.loads(raw.strip())
                                if d.get("cwd"):
                                    cwd = d["cwd"]
                                    break
                                if d.get("type") == "session" and d.get("cwd"):
                                    cwd = d["cwd"]
                                    break
                                if d.get("type") == "session_meta" and d.get("payload", {}).get("cwd"):
                                    cwd = d["payload"]["cwd"]
                                    break
                            except Exception:
                                continue
                except Exception:
                    pass
                # For grok, try URL-decoding parent dir
                if agent == "grok" and not cwd and p.parent.name and "%" in p.parent.name:
                    import urllib.parse
                    cwd = urllib.parse.unquote(p.parent.name)
                name = _project_name(cwd)
                key = f"{agent}:{cwd or session_id}"
                if key not in projects:
                    projects[key] = {"agent": agent, "cwd": cwd, "name": name, "sessions": []}
                projects[key]["sessions"].append({"id": session_id, "path": p, "agent": agent})

    # Scan live roots
    for agent, root in live_roots.items():
        if not root.exists():
            continue
        if agent == "claude":
            for proj_dir in root.iterdir():
                if not proj_dir.is_dir():
                    continue
                for jsonl in proj_dir.glob("*.jsonl"):
                    session_id = jsonl.stem
                    cwd = ""
                    try:
                        with open(jsonl, "r", encoding="utf-8", errors="replace") as f:
                            for raw in f:
                                try:
                                    d = json.loads(raw.strip())
                                    if d.get("cwd"):
                                        cwd = d["cwd"]
                                        break
                                except Exception:
                                    continue
                    except Exception:
                        pass
                    name = _project_name(cwd) if cwd else proj_dir.name
                    key = f"{agent}:{cwd or proj_dir.name}"
                    if key not in projects:
                        projects[key] = {"agent": agent, "cwd": cwd, "name": name, "sessions": []}
                    projects[key]["sessions"].append({"id": session_id, "path": jsonl, "agent": agent})
        elif agent == "codex":
            for jsonl in root.glob("**/*.jsonl"):
                if not jsonl.is_file():
                    continue
                session_id = jsonl.stem
                cwd = ""
                try:
                    with open(jsonl, "r", encoding="utf-8", errors="replace") as f:
                        for raw in f:
                            try:
                                d = json.loads(raw.strip())
                                if d.get("type") == "session_meta" and d.get("payload", {}).get("cwd"):
                                    cwd = d["payload"]["cwd"]
                                    break
                            except Exception:
                                continue
                except Exception:
                    pass
                name = _project_name(cwd)
                key = f"{agent}:{cwd or session_id}"
                if key not in projects:
                    projects[key] = {"agent": agent, "cwd": cwd, "name": name, "sessions": []}
                projects[key]["sessions"].append({"id": session_id, "path": jsonl, "agent": agent})
        elif agent == "grok":
            for chat in root.glob("sessions/**/chat_history.jsonl"):
                session_id = chat.parent.name
                cwd = ""
                if "%" in session_id:
                    import urllib.parse
                    cwd = urllib.parse.unquote(session_id)
                name = _project_name(cwd)
                key = f"{agent}:{cwd or session_id}"
                if key not in projects:
                    projects[key] = {"agent": agent, "cwd": cwd, "name": name, "sessions": []}
                projects[key]["sessions"].append({"id": session_id, "path": chat, "agent": agent})
        elif agent == "hermes":
            # Hermes: default/messages.jsonl and profiles/*/state.db exports
            for msgs in root.glob("**/messages.jsonl"):
                session_id = msgs.parent.name
                cwd = ""
                name = _project_name(cwd) if cwd else session_id
                key = f"{agent}:{cwd or session_id}"
                if key not in projects:
                    projects[key] = {"agent": agent, "cwd": cwd, "name": name, "sessions": []}
                projects[key]["sessions"].append({"id": session_id, "path": msgs, "agent": agent})
        elif agent in ("openclaw", "pi"):
            for jsonl in root.glob("**/*.jsonl"):
                if not jsonl.is_file():
                    continue
                if agent == "openclaw" and "trajectory" in jsonl.name:
                    continue
                session_id = jsonl.stem
                cwd = ""
                try:
                    with open(jsonl, "r", encoding="utf-8", errors="replace") as f:
                        for raw in f:
                            try:
                                d = json.loads(raw.strip())
                                if d.get("type") == "session" and d.get("cwd"):
                                    cwd = d["cwd"]
                                    break
                            except Exception:
                                continue
                except Exception:
                    pass
                name = _project_name(cwd)
                key = f"{agent}:{cwd or session_id}"
                if key not in projects:
                    projects[key] = {"agent": agent, "cwd": cwd, "name": name, "sessions": []}
                projects[key]["sessions"].append({"id": session_id, "path": jsonl, "agent": agent})

    return projects

def export_unified(archive_root: Path, output_path: Path, agent_filter: set[str] | None = None,
                   limit_per_agent: int | None = None):
    """Export all agent transcripts to a single unified JSONL file.

    Each line: {"agent": "...", "session_id": "...", "project": "...", "role": "user"|"assistant",
                "text": "...", "ts": "...", "line_no": N}
    """
    import sys
    projects = scan(archive_root, include_live=False)
    total = 0
    agent_counts = {}

    with open(output_path, "w", encoding="utf-8", newline="\n") as out:
        for proj_key, proj_info in projects.items():
            agent = proj_info["agent"]
            if agent_filter and agent not in agent_filter:
                continue
            project_name = proj_info["name"]
            for sess in proj_info["sessions"]:
                session_id = sess["id"]
                session_path = sess["path"]
                session_agent = sess["agent"]

                # Apply per-agent limit
                if limit_per_agent:
                    agent_counts.setdefault(session_agent, 0)
                    if agent_counts[session_agent] >= limit_per_agent:
                        continue
                    agent_counts[session_agent] += 1

                try:
                    user_msgs, transcript = parse_transcript_points(session_path, session_agent)
                    for entry in transcript:
                        record = {
                            "agent": session_agent,
                            "session_id": session_id,
                            "project": project_name,
                            "role": entry["role"],
                            "text": entry["text"],
                            "ts": entry["ts"],
                            "line_no": entry["line_no"],
                        }
                        out.write(json.dumps(record, ensure_ascii=False) + "\n")
                        total += 1
                except Exception as e:
                    print(f"[export] SKIP {session_agent}:{session_id} — {type(e).__name__}", file=sys.stderr)
                    continue

    print(f"Exported {total} messages to {output_path}")
    for agent, count in sorted(agent_counts.items()):
        print(f"  {agent}: {count} sessions")


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Multi-agent transcript parser")
    parser.add_argument("--export-unified", type=str, help="Export all transcripts to unified JSONL")
    parser.add_argument("--archive", type=str, help="Path to apocalypse_archive root")
    parser.add_argument("--agents", type=str, help="Comma-separated agent names")
    parser.add_argument("--limit", type=int, help="Max sessions per agent")
    args = parser.parse_args()

    if args.export_unified:
        archive = Path(args.archive) if args.archive else Path(r"E:\BaiduSyncdisk\ClaudeCode_Workspace\apocalypse_archive")
        if not archive.exists():
            print(f"archive not found: {archive}")
            sys.exit(1)
        agent_filter = set(a.strip() for a in args.agents.split(",") if a.strip()) if args.agents else None
        export_unified(archive, Path(args.export_unified), agent_filter, args.limit)
        sys.exit(0)

    # Self-check: parse one sample per agent from archive, assert msg_count ≥ 1
    archive = Path(r"E:\BaiduSyncdisk\ClaudeCode_Workspace\apocalypse_archive")
    if not archive.exists():
        print(f"archive not found: {archive}")
        sys.exit(1)
    chat_root = archive / "agent-chats"
    samples = {
        "claude": next(chat_root.glob("claude/projects/**/*.jsonl"), None),
        "codex": next(chat_root.glob("codex/sessions/**/*.jsonl"), None),
        "grok": next(chat_root.glob("grok/sessions/**/chat_history.jsonl"), None),
        "hermes": next(chat_root.glob("hermes/**/messages.jsonl"), None),
        "openclaw": next((p for p in chat_root.glob("openclaw/**/*.jsonl") if "trajectory" not in p.name), None),
        "pi": next(chat_root.glob("pi/**/*.jsonl"), None),
    }
    for agent, path in samples.items():
        if not path:
            print(f"{agent:10} SKIP (no sample)")
            continue
        try:
            parsed = parse_transcript(path, agent)
            print(f"{agent:10} msg_count={parsed['msg_count']:3} first_user={parsed['first_user_msg'][:60]}")
            assert parsed["msg_count"] >= 1, f"{agent} has no messages"
        except Exception as e:
            print(f"{agent:10} FAIL: {e}")
            raise
    print("\nAll agents parsed successfully.")
