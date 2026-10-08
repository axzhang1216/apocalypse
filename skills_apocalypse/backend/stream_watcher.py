#!/usr/bin/env python3
"""Live conversation stream watcher for Apocalypse Spatial OS.

Tails every agent chat source every APOCALYPSE_STREAM_POLL_SECONDS (default 5),
normalizes each new line (pipeline/transcripts.extract_transcript_line), judges
it (Jev, with the configured analysis model as fallback), and attaches it to
the right project and conversation in real time.

Storage layout
  ~/.claude/apocalypse/stream_state.json   file/sqlite cursors only
  ~/.claude/apocalypse/conversations/      one jsonl per session:
      {agent}_{project}_{session_id}.conversations.jsonl
      one JSON record per conversation (same shape as the batch pipeline's
      conversations_output, plus first_ts/last_ts/msg_count/source:"live")
  batch conversations_output dirs are indexed read-only for history.

Ownership rules
  - History is NOT re-judged: on first sight a raw file's cursor starts at EOF
    unless the session has conversations (then the last 256 KB is re-scanned,
    skipping messages already covered by a timestamp watermark) or the file is
    brand new (mtime < 1 h, no conversations → process from line 0).
  - When the watcher first attaches to a session that only has a batch
    conversations file, it copies that file into the live dir and works on the
    copy.  The batch dir is never written to.  Re-running the batch pipeline
    later does NOT refresh an adopted live file; delete the live file to
    re-adopt from batch.
  - Only the watcher writes live conversation files.  If a live file changes
    under it anyway, the watcher reloads that session wholesale from the file.

Judgment (agreed design)
  - Every message: cheap prefilter, then Jev "meaningful" (noul >= 0.5).
  - User messages only: Jev "new_topic" vs the immediately previous message
    in the same session (assistant replies included).
  - Assistant messages attach to the current conversation, no topic call.
  - Jev failure -> analysis model via backend/analysis_harness; if that also
    fails, keep the message (meaningful, same topic) rather than drop it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path.home() / ".claude" / "apocalypse"
STATE_FILE = DATA_DIR / "stream_state.json"
LIVE_CONV_DIR = DATA_DIR / "conversations"
EVENTS_FILE = DATA_DIR / "events.jsonl"
POLL_SECONDS = float(os.environ.get("APOCALYPSE_STREAM_POLL_SECONDS", "5"))
JEV_TIMEOUT = float(os.environ.get("APOCALYPSE_JEV_TIMEOUT", "20"))
THRESHOLD = 0.5
TAIL_SCAN_BYTES = 256 * 1024      # first-sight catch-up window for adopted sessions
FRESH_CONVERSATION_SECONDS = 2 * 3600   # conversations written within this are "active"
FRESH_FILE_SECONDS = 3600        # raw file mtime within this = brand-new session
HERMES_LOOKBACK_ROWS = 200       # first-sight sqlite catch-up window

# pipeline/ lives beside backend/ in both repo and installed layouts.
_PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
import sys  # noqa: E402
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

import transcripts  # noqa: E402  (pipeline; stdlib-only, bundles cleanly)

MEANINGFUL_INSTRUCTIONS = (
    "The `text` field is one message from a human-agent chat session. "
    "Does it carry meaningful human intent or a substantive agent reply that "
    "advances a real task? Meaningful: a real request, question, instruction, "
    "correction, or a reply that moves the task forward. Not meaningful: ping "
    "text like 'test'/'继续', slash-command UI chrome, tool or terminal dumps, "
    "JSON blobs, model-switch notices, mechanical acknowledgements. "
    "noul near 1 = meaningful, near 0 = not meaningful."
)

NEW_TOPIC_INSTRUCTIONS = (
    "`text` is a new user message; `prev_text` is the immediately previous "
    "message in the same session (often the assistant's last reply). Decide "
    "whether `text` starts a NEW conversation topic. Same topic (noul near 0): "
    "follow-up, clarification, correction, retry, debugging iteration, next "
    "step of the current goal, short acknowledgement. New topic (noul near 1): "
    "an unrelated new question or a different standalone goal that would still "
    "make sense if all previous messages were removed."
)

_TITLE_LANG = os.environ.get("APOCALYPSE_OUTPUT_LANGUAGE", "简体中文")

_state_lock = threading.RLock()
_state: dict = {"version": 1, "files": {}, "hermes": {}}
_sessions: dict[str, dict] = {}          # sid -> session index (in-memory, rebuilt from files)
_sid_info: dict[str, dict] = {}          # sid -> {project, cwd, agent} from transcripts.scan()
_bg_thread: threading.Thread | None = None
_stop = threading.Event()
_meta_cache: dict[str, dict] = {}        # raw file path -> {session_id, project, cwd}


def _batch_dirs() -> list[Path]:
    """Batch conversations_output dirs (read-only history). Env override replaces
    the repo default entirely (used by tests to isolate from real data)."""
    env = os.environ.get("APOCALYPSE_BATCH_CONVERSATIONS_DIR", "").strip()
    if env:
        return [Path(env).expanduser()]
    return [Path(__file__).resolve().parents[1] / "data" / "conversations_output"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(v):
    try:
        return transcripts._ts_to_dt(v)
    except Exception:
        return None


# ────────────────────────── cursors (stream_state.json) ──────────────────────

def _load_state() -> None:
    global _state
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == 1:
            _state = {"version": 1,
                      "files": data.get("files") or {},
                      "hermes": data.get("hermes") or {}}
            return
    except Exception:
        pass
    _state = {"version": 1, "files": {}, "hermes": {}}


def _save_state() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


# ───────────────────────── conversations file handling ───────────────────────

def _read_conversations(path: Path) -> tuple[list[dict], int]:
    """Parse a conversations jsonl. Returns (records, byte offset of last line)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return [], 0
    records, offsets, pos = [], [], 0
    for raw in data.split(b"\n"):
        line_len = len(raw) + 1
        s = raw.decode("utf-8", errors="replace").strip()
        if s:
            try:
                d = json.loads(s)
                if isinstance(d, dict) and d.get("conversation_id"):
                    records.append(d)
                    offsets.append(pos)
            except Exception:
                pass
        pos += line_len
    return records, (offsets[-1] if offsets else 0)


def _row_from_record(rec: dict) -> dict:
    msgs = rec.get("messages") or []
    return {
        "cid": str(rec.get("conversation_id", "")),
        "session_id": str(rec.get("session_id", "")),
        "title": str(rec.get("title") or "conversation"),
        "first_ts": rec.get("first_ts") or (msgs[0].get("ts") if msgs else ""),
        "last_ts": rec.get("last_ts") or (msgs[-1].get("ts") if msgs else ""),
        "msg_count": rec.get("msg_count", len(msgs)),
        "start_line_no": rec.get("start_line_no"),
        "end_line_no": rec.get("end_line_no"),
        "source": str(rec.get("source") or "batch"),
    }


def _agent_from_name(name: str) -> str:
    for a in ("claude", "codex", "grok", "hermes", "openclaw", "pi"):
        if name.startswith(a + "_"):
            return a
    return ""


def _next_line_no(records: list[dict]) -> int:
    mx = 0
    for c in records:
        for v in (c.get("start_line_no"), c.get("end_line_no")):
            try:
                mx = max(mx, int(v))
            except (TypeError, ValueError):
                pass
        for m in c.get("messages") or []:
            try:
                mx = max(mx, int(m.get("line_no") or 0))
            except (TypeError, ValueError):
                pass
    return mx + 1


def _next_cid(sid: str, records: list[dict]) -> int:
    mx = 0
    prefix = sid + "::c"
    for c in records:
        cid = str(c.get("conversation_id") or "")
        if cid.startswith(prefix):
            try:
                mx = max(mx, int(cid[len(prefix):]))
            except ValueError:
                pass
    return mx + 1


def _conv_file_key(path: Path) -> dict:
    st = path.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _load_session_from_file(agent: str, conv_file: Path) -> tuple[str, dict] | None:
    """Build a session index entry from a conversations file."""
    records, last_offset = _read_conversations(conv_file)
    if not records:
        return None
    sid = str(records[0].get("session_id") or "")
    if not sid:
        return None
    first_msg = (records[0].get("messages") or [{}])[0]
    ag = str(first_msg.get("agent") or "") or agent or _agent_from_name(conv_file.name)
    project = str(first_msg.get("project") or "") or "unknown"
    rows = [_row_from_record(r) for r in records[:-1]]
    open_row = _row_from_record(records[-1])
    open_row["line_offset"] = last_offset        # messages loaded lazily
    sess = {
        "session_id": sid, "agent": ag, "project": project, "cwd": "",
        "conv_file": str(conv_file), "conversations": rows, "open": open_row,
        "next_line_no": _next_line_no(records), "next_cid": _next_cid(sid, records),
        "prev_text": "", "updated_at": str(open_row.get("last_ts") or ""),
    }
    try:
        sess["conv_file_stat"] = _conv_file_key(conv_file)
    except OSError:
        sess["conv_file_stat"] = None
    return sid, sess


def _refresh_sid_projects() -> None:
    """Build sid -> {project, cwd, agent} by peeking each raw session file.
    Costs a few seconds (file opens); run once in the background thread.
    Grok/hermes ids don't match (path-/db-keyed); they resolve at attach time.
    """
    try:
        projects = transcripts.scan(include_live=True)
    except Exception:
        return
    info: dict[str, dict] = {}
    for p in (projects or {}).values():
        for s in p.get("sessions") or []:
            sid = str(s.get("id") or "")
            if sid and sid not in info:
                info[sid] = {"project": p.get("name") or "unknown",
                             "cwd": p.get("cwd") or "",
                             "agent": s.get("agent") or ""}
    _sid_info.clear()
    _sid_info.update(info)


def _startup_index() -> None:
    """Index live + batch conversations files (live dir wins on duplicates)."""
    _sessions.clear()
    seen: set[str] = set()
    for d in [LIVE_CONV_DIR, *_batch_dirs()]:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.conversations.jsonl")):
            loaded = _load_session_from_file("", f)
            if not loaded:
                continue
            sid, sess = loaded
            if sid in seen:
                continue
            seen.add(sid)
            info = _sid_info.get(sid)
            if info:
                if sess.get("project") in ("", "unknown"):
                    sess["project"] = info["project"]
                if not sess.get("cwd"):
                    sess["cwd"] = info["cwd"]
                if not sess.get("agent"):
                    sess["agent"] = info["agent"]
            _sessions[sid] = sess


def _find_conv_file(sid: str) -> Path | None:
    """Locate a conversations file for a session (index first, then glob)."""
    sess = _sessions.get(sid)
    if sess and sess.get("conv_file") and Path(sess["conv_file"]).exists():
        return Path(sess["conv_file"])
    suffix = f"{sid}.conversations.jsonl"
    for d in [LIVE_CONV_DIR, *_batch_dirs()]:
        if not d.is_dir():
            continue
        for f in d.glob(f"*{suffix}"):
            return f
    return None


def _session_watermark(sess: dict):
    """Max parsed last_ts across a session's conversations (datetime or None)."""
    best = None
    cands = [r.get("last_ts") for r in sess.get("conversations") or []]
    o = sess.get("open") or {}
    cands.append(o.get("last_ts"))
    for c in cands:
        dt = _parse_ts(c)
        if dt and (best is None or dt > best):
            best = dt
    return best


# ───────────────────────────── source discovery ───────────────────────────────

def _iter_file_sources():
    """Yield (agent, Path) for every live jsonl session file, all agents."""
    home = Path.home()
    specs = [
        ("claude", Path(os.environ.get("CLAUDE_CONFIG_DIR") or (home / ".claude")).expanduser(),
         ("projects/**/*.jsonl",)),
        ("codex", Path(os.environ.get("CODEX_HOME") or (home / ".codex")).expanduser(),
         ("sessions/**/*.jsonl", "archived_sessions/**/*.jsonl")),
        ("pi", Path(os.environ.get("PI_CODING_AGENT_SESSION_DIR") or (home / ".pi" / "agent" / "sessions")).expanduser(),
         ("**/*.jsonl",)),
        ("openclaw", Path(os.environ.get("OPENCLAW_STATE_DIR") or (home / ".openclaw")).expanduser(),
         ("agents/*/sessions/**/*.jsonl", "sessions/**/*.jsonl")),
        ("grok", Path(os.environ.get("GROK_HOME") or (home / ".grok")).expanduser(),
         ("sessions/**/chat_history.jsonl",)),
    ]
    for agent, root, patterns in specs:
        if not root.is_dir():
            continue
        for pattern in patterns:
            for p in root.glob(pattern):
                if p.is_file() and p.name != "session_index.jsonl":
                    yield agent, p


def _hermes_dbs() -> list[Path]:
    out: list[Path] = []
    seen: set = set()
    explicit = os.environ.get("HERMES_HOME", "").strip()
    roots = [Path(explicit).expanduser()] if explicit else [Path.home() / ".hermes"]
    if not explicit and os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        roots.insert(0, Path(os.environ["LOCALAPPDATA"]) / "hermes")
    for r in roots:
        for p in [r / "state.db", *r.glob("profiles/*/state.db")]:
            if not p.exists():
                continue
            try:
                st = p.stat()
                key = (st.st_dev, st.st_ino) if st.st_ino else str(p.resolve())
            except OSError:
                key = str(p)
            if key in seen:
                continue          # ~/.hermes and %LOCALAPPDATA%/hermes are the same file
            seen.add(key)
            out.append(p)
    return out


def _session_id_for_file(agent: str, path: Path) -> str:
    stem = path.stem
    if agent == "grok":
        # all grok sessions share the filename chat_history.jsonl; use the
        # encoded-cwd parent dir to disambiguate.
        crc = zlib.crc32(str(path.parent).encode("utf-8")) & 0xFFFF
        return f"{stem}-{crc:04x}"
    return stem


def _session_meta_for_file(agent: str, path: Path) -> dict:
    """{session_id, project, cwd} for a live session file (parsed once, cached)."""
    key = str(path)
    if key in _meta_cache:
        return _meta_cache[key]
    sid = _session_id_for_file(agent, path)
    cwd = ""
    try:
        data = transcripts.parse_transcript(path, agent)
        cwd = str(data.get("cwd") or "")
    except Exception:
        pass
    if not cwd and agent == "grok":
        cwd = urllib.parse.unquote(str(path.parent))
    meta = {"session_id": sid, "project": transcripts._project_name(cwd), "cwd": cwd}
    _meta_cache[key] = meta
    return meta


# ─────────────────────────────── judgment ─────────────────────────────────────

def _jev_config() -> tuple[str, str]:
    """Return (model, api_key) for Jev, or raise if unavailable."""
    ts: dict = {}
    try:
        cfg = json.loads((DATA_DIR / "harness.json").read_text(encoding="utf-8"))
        ts = cfg.get("typesafe") or {}
    except Exception:
        pass
    model = str(ts.get("model") or "jev-latest")
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        auth = ts.get("auth") or {}
        if auth.get("kind") == "file":
            try:
                p = Path(os.path.expandvars(os.path.expanduser(str(auth.get("path") or ""))))
                cur = json.loads(p.read_text(encoding="utf-8"))
                for part in str(auth.get("json_path") or "").split("."):
                    if part:
                        cur = cur[part]
                key = str(cur or "")
            except Exception:
                key = ""
        elif auth.get("kind") == "env":
            key = os.environ.get(str(auth.get("name") or ""), "")
    if not key:
        try:
            secrets = json.loads((DATA_DIR / "secrets.json").read_text(encoding="utf-8"))
            key = str(secrets.get("typesafe_api_key") or "")
        except Exception:
            key = ""
    if not key:
        raise RuntimeError("no typesafe api key configured")
    return model, key


def _ask_jev(text: str, prev_text: str, with_topic: bool) -> dict:
    model, key = _jev_config()
    state = {"text": text[:3000], "role": "user"}
    questions = {"meaningful": {"type": "noul", "instructions": MEANINGFUL_INSTRUCTIONS}}
    if with_topic:
        state["prev_text"] = (prev_text or "")[:3000]
        questions["new_topic"] = {"type": "noul", "instructions": NEW_TOPIC_INSTRUCTIONS}
    req = urllib.request.Request(
        "https://api.typesafe.ai/v1/systemone",
        data=json.dumps({"model": model, "state": state, "questions": questions},
                        ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json", "authorization": "Bearer " + key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=JEV_TIMEOUT) as r:
        d = json.loads(r.read().decode("utf-8", errors="replace"))
    answers = d.get("answers") or {}
    out = {"meaningful": float((answers.get("meaningful") or {}).get("noul", 0)) >= THRESHOLD,
           "new_topic": None, "via": "jev"}
    if with_topic:
        out["new_topic"] = float((answers.get("new_topic") or {}).get("noul", 0)) >= THRESHOLD
    return out


def _fallback_judge(text: str, prev_text: str, with_topic: bool) -> dict:
    import analysis_harness
    q = (
        "You are a message router in a chat archive system. Judge ONE message.\n"
        "1. meaningful: does it carry meaningful human intent or a substantive\n"
        "   reply advancing a real task? (not ping text, command chrome, tool\n"
        "   dumps, or mechanical acknowledgements)\n"
    )
    if with_topic:
        q += (
            "2. new_topic: does it start a NEW conversation topic relative to\n"
            "   the previous message? (follow-ups/clarifications/retries/next\n"
            "   steps are the SAME topic; an unrelated new goal is a NEW topic)\n"
        )
    q += (
        f"\nPREVIOUS MESSAGE (same session, may be assistant):\n{(prev_text or '(none)')[:2000]}\n\n"
        f"CURRENT MESSAGE:\n{text[:3000]}\n\n"
        'Reply with ONLY JSON: {"meaningful": true|false'
    )
    if with_topic:
        q += ', "new_topic": true|false'
    q += "}"
    d = analysis_harness.complete_json(q, max_tokens=200, timeout=60)
    return {"meaningful": bool(d.get("meaningful")),
            "new_topic": bool(d.get("new_topic")) if with_topic else None,
            "via": "analysis"}


def judge(text: str, prev_text: str, with_topic: bool) -> dict:
    """Jev → analysis model → keep-the-message default."""
    try:
        return _ask_jev(text, prev_text, with_topic)
    except Exception:
        pass
    try:
        return _fallback_judge(text, prev_text, with_topic)
    except Exception:
        return {"meaningful": True, "new_topic": False, "via": "default"}


def _make_title(text: str) -> str:
    try:
        import analysis_harness
        title = analysis_harness.complete(
            f"用{_TITLE_LANG}为下面这条用户消息代表的对话主题起一个不超过16字的简短标题，"
            f"只输出标题本身：\n{text[:1500]}",
            max_tokens=48, timeout=30,
        ).strip().strip('"“”').splitlines()[0]
        if title:
            return title[:32]
    except Exception:
        pass
    t = re.sub(r"\s+", " ", text or "").strip()
    return (t[:24] + "…") if len(t) > 24 else (t or "conversation")


def _should_skip(role: str, text: str) -> bool:
    """Cheap pre-model filter.

    Mirrors pipeline/clean_session.py's prefilter()/classify_user() rules —
    keep them in sync so live and batch data judge noise identically.
    (Not imported from clean_session because it pulls httpx, which the
    desktop bundle does not ship.)
    """
    if role not in ("user", "assistant"):
        return True
    if not text or len(text) < 3:
        return True
    if role in ("system", "developer", "tool", "toolResult", "function", "tool_call"):
        return True
    if text.startswith("# AGENTS.md") or text.startswith("<environment_context>"):
        return True
    if text.startswith("# Context from my IDE setup:") and len(text) < 200:
        return True
    if role == "user" and text.strip().lower() in ("test", "继续"):
        return True
    if transcripts._is_noise_user_text(text):
        return True
    return False


# ─────────────────────────── session state / adoption ────────────────────────

def _ensure_session(agent: str, meta: dict) -> dict:
    """Get or create the in-memory session record; adopt batch files to live."""
    sid = meta["session_id"]
    sess = _sessions.get(sid)
    if sess is None:
        found = _find_conv_file(sid)
        if found is not None:
            loaded = _load_session_from_file(agent, found)
            if loaded and loaded[0] == sid:
                sess = loaded[1]
        if sess is None:
            sess = {
                "session_id": sid, "agent": agent, "project": meta["project"],
                "cwd": meta["cwd"],
                "conv_file": str(LIVE_CONV_DIR / f"{agent}_{meta['project']}_{sid}.conversations.jsonl"),
                "conversations": [], "open": None, "next_line_no": 1,
                "next_cid": 1, "prev_text": "", "updated_at": "",
                "conv_file_stat": None,
            }
        _sessions[sid] = sess
    if not sess.get("cwd") and meta.get("cwd"):
        sess["cwd"] = meta["cwd"]
    if not sess.get("agent"):
        sess["agent"] = agent
    if sess.get("project") in ("", "unknown") and meta.get("project") not in ("", "unknown", None):
        sess["project"] = meta["project"]
    _adopt_to_live(sess)
    return sess


def _adopt_to_live(sess: dict) -> None:
    """Copy a batch conversations file into the live dir (once)."""
    cf = Path(sess.get("conv_file") or "")
    if not cf or cf.parent == LIVE_CONV_DIR or not cf.exists():
        return
    live = LIVE_CONV_DIR / f"{sess.get('agent') or 'agent'}_{sess.get('project') or 'unknown'}_{sess['session_id']}.conversations.jsonl"
    if live.exists():
        sess["conv_file"] = str(live)
    else:
        try:
            live.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cf, live)
            sess["conv_file"] = str(live)
        except OSError:
            return          # keep batch path; batch dir is read-only, so writes may be lost
    try:
        sess["conv_file_stat"] = _conv_file_key(Path(sess["conv_file"]))
    except OSError:
        sess["conv_file_stat"] = None


def _check_external_change(sess: dict) -> bool:
    rec = sess.get("conv_file_stat")
    if rec is None:
        return False
    try:
        return _conv_file_key(Path(sess["conv_file"])) != rec
    except OSError:
        return True


def _reload_session(sid: str) -> dict:
    """Wholesale reload of a session from its (or any) conversations file."""
    old = _sessions.get(sid) or {}
    f = Path(old.get("conv_file") or "")
    if not f.exists():
        f = _find_conv_file(sid) or f
    sess = None
    if f.exists():
        loaded = _load_session_from_file(old.get("agent", ""), f)
        if loaded and loaded[0] == sid:
            sess = loaded[1]
    if sess is None:
        sess = {
            "session_id": sid, "agent": old.get("agent", ""),
            "project": old.get("project", "unknown"), "cwd": old.get("cwd", ""),
            "conv_file": str(LIVE_CONV_DIR / f"{old.get('agent') or 'agent'}_{old.get('project') or 'unknown'}_{sid}.conversations.jsonl"),
            "conversations": [], "open": None, "next_line_no": 1, "next_cid": 1,
            "prev_text": "", "updated_at": "", "conv_file_stat": None,
        }
    _sessions[sid] = sess
    return sess


def _load_open(sess: dict) -> None:
    """Lazily load the open conversation's messages from its file record."""
    o = sess.get("open")
    if o is None or "messages" in o:
        return
    rec = None
    try:
        with open(Path(sess.get("conv_file") or ""), "rb") as fh:
            fh.seek(int(o.get("line_offset") or 0))
            rec = json.loads(fh.readline().decode("utf-8", errors="replace"))
    except Exception:
        rec = None
    if rec is None:
        # can't restore: keep the old record in the file untouched, close it
        sess.setdefault("conversations", []).append(
            {k: o.get(k) for k in ("cid", "session_id", "title", "first_ts",
                                    "last_ts", "msg_count", "start_line_no",
                                    "end_line_no", "source")})
        sess["open"] = None
        return
    msgs = [dict(m) for m in (rec.get("messages") or [])]
    o["messages"] = msgs
    o["msg_count"] = len(msgs)
    if msgs:
        sess["prev_text"] = str(msgs[-1].get("text") or "") or sess.get("prev_text", "")


# ─────────────────────────── conversations file writes ───────────────────────

def _record_from_open(o: dict) -> dict:
    msgs = o.get("messages") or []
    return {
        "conversation_id": o.get("cid", ""),
        "session_id": o.get("session_id", ""),
        "title": o.get("title", "conversation"),
        "start_line_no": msgs[0].get("line_no") if msgs else None,
        "end_line_no": msgs[-1].get("line_no") if msgs else None,
        "first_ts": msgs[0].get("ts") if msgs else "",
        "last_ts": msgs[-1].get("ts") if msgs else "",
        "msg_count": len(msgs),
        "messages": msgs,
        "source": o.get("source", "live"),
    }


def _row_from_open(o: dict) -> dict:
    msgs = o.get("messages") or []
    return {
        "cid": o.get("cid", ""), "session_id": o.get("session_id", ""),
        "title": o.get("title", "conversation"),
        "first_ts": o.get("first_ts") or (msgs[0].get("ts") if msgs else ""),
        "last_ts": o.get("last_ts") or (msgs[-1].get("ts") if msgs else ""),
        "msg_count": o.get("msg_count", len(msgs)),
        "start_line_no": o.get("start_line_no"),
        "end_line_no": o.get("end_line_no"),
        "source": o.get("source", "live"),
    }


def _rewrite_open(sess: dict) -> None:
    """Rewrite the open conversation's record in place (truncate + write)."""
    o = sess["open"]
    msgs = o.get("messages") or []
    if msgs:
        o["first_ts"] = msgs[0].get("ts", "")
        o["last_ts"] = msgs[-1].get("ts", "")
        o["start_line_no"] = msgs[0].get("line_no")
        o["end_line_no"] = msgs[-1].get("line_no")
    o["msg_count"] = len(msgs)
    data = (json.dumps(_record_from_open(o), ensure_ascii=False) + "\n").encode("utf-8")
    f = Path(sess["conv_file"])
    f.parent.mkdir(parents=True, exist_ok=True)
    offset = int(o.get("line_offset") or 0)
    if f.exists():
        with open(f, "r+b") as fh:
            fh.truncate(offset)
            fh.seek(offset)
            fh.write(data)
    else:
        with open(f, "wb") as fh:
            fh.write(data)
    try:
        sess["conv_file_stat"] = _conv_file_key(f)
    except OSError:
        pass


def _append_record(sess: dict, record: dict) -> int:
    """Append a new conversation record; returns its byte offset."""
    f = Path(sess["conv_file"])
    f.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    offset = f.stat().st_size if f.exists() else 0
    with open(f, "ab") as fh:
        fh.write(data)
    try:
        sess["conv_file_stat"] = _conv_file_key(f)
    except OSError:
        pass
    return offset


# ───────────────────────────── message routing ────────────────────────────────

def _emit_event(kind: str, sid: str, agent: str, project: str, extra: dict) -> None:
    ev = {"ts": _now_iso(), "session_id": sid, "agent": agent, "project": project,
          "project_name": project, "type": kind}
    ev.update(extra)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_FILE, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass


def process_message(agent: str, meta: dict, role: str, text: str, ts) -> None:
    """Prefilter → judge → route one message into its conversation."""
    sid = meta["session_id"]
    if _should_skip(role, text):
        return
    with _state_lock:
        sess = _ensure_session(agent, meta)
        if _check_external_change(sess):
            sess = _reload_session(sid)
            sess = _ensure_session(agent, meta)
        _load_open(sess)
        prev_open = sess.get("open")
        prev_text = sess.get("prev_text") or ""
    # model calls happen outside the lock so the read API stays responsive
    verdict = judge(text, prev_text, role == "user")
    starts_new_guess = prev_open is None or (role == "user" and bool(verdict.get("new_topic")))
    title = _make_title(text) if (starts_new_guess and role == "user") else None
    with _state_lock:
        sess = _sessions.get(sid)
        if sess is None:
            return
        if not verdict.get("meaningful"):
            sess["prev_text"] = text
            sess["updated_at"] = _now_iso()
            return
        line_no = int(sess.get("next_line_no") or 1)
        sess["next_line_no"] = line_no + 1
        msg = {"agent": agent, "session_id": sid, "project": sess.get("project", ""),
               "role": role, "text": text, "ts": ts, "line_no": line_no}
        open_c = sess.get("open")
        starts_new = open_c is None or (role == "user" and bool(verdict.get("new_topic")))
        if not starts_new:
            msgs = open_c.setdefault("messages", [])
            if msgs and msgs[-1].get("role") == "assistant":
                msgs[-1]["conclusion"] = (role == "user")
            msg["conclusion"] = (role == "assistant")   # last message so far
            msgs.append(msg)
            _rewrite_open(sess)
            _emit_event("message", sid, agent, sess.get("project", ""),
                        {"conversation_id": open_c["cid"], "msg_count": len(msgs)})
        else:
            if open_c is not None:
                sess["conversations"].append(_row_from_open(open_c))
            n = int(sess.get("next_cid") or 1)
            sess["next_cid"] = n + 1
            cid = f"{sid}::c{n:03d}"
            final_title = title or re.sub(r"\s+", " ", text)[:24] or "conversation"
            new_open = {"cid": cid, "session_id": sid, "title": final_title,
                        "messages": [msg], "source": "live", "first_ts": ts,
                        "last_ts": ts, "msg_count": 1, "start_line_no": line_no,
                        "end_line_no": line_no}
            new_open["line_offset"] = _append_record(sess, _record_from_open(new_open))
            sess["open"] = new_open
            _emit_event("conversation", sid, agent, sess.get("project", ""),
                        {"conversation_id": cid, "title": final_title, "reason": "new"})
        sess["prev_text"] = text
        sess["updated_at"] = _now_iso()


# ─────────────────────────────── tailing ──────────────────────────────────────

def _ts_covered(ts, watermark) -> bool:
    """True if ts <= watermark (both parsed leniently; ISO strings or datetimes)."""
    dt = _parse_ts(ts)
    wm = _parse_ts(watermark)
    return bool(dt and wm and dt <= wm)


def _first_sight_cursor(agent: str, path: Path, size: int, mtime: float) -> tuple[dict, object]:
    """Cursor decision for a raw file the watcher has never seen before.
    Returns (cursor, watermark) where watermark is an ISO string or None."""
    sid = _session_id_for_file(agent, path)
    sess = _sessions.get(sid)
    if sess is not None:
        # session already has conversations (batch or live)
        conv = Path(sess.get("conv_file") or "")
        try:
            fresh = time.time() - conv.stat().st_mtime <= FRESH_CONVERSATION_SECONDS
        except OSError:
            fresh = False
        if fresh:
            wm = _session_watermark(sess)
            return {"offset": max(0, size - TAIL_SCAN_BYTES)}, (wm.isoformat() if wm else None)
        return {"offset": size}, None
    if time.time() - mtime <= FRESH_FILE_SECONDS:
        return {"offset": 0}, None       # brand-new session: process from the start
    return {"offset": size}, None        # history: the batch pipeline's job


def _read_new_lines(agent: str, path: Path, cur: dict | None):
    """Read new complete lines. Returns (lines, cursor, watermark)."""
    try:
        st = path.stat()
    except OSError:
        return [], (cur or {"offset": 0, "size": 0}), None
    size, mtime = st.st_size, st.st_mtime
    watermark = None
    if cur is None:
        cur, watermark = _first_sight_cursor(agent, path, size, mtime)
    else:
        watermark = cur.get("watermark")
        if size < cur.get("offset", 0):
            cur = {"offset": 0}          # truncated/rotated: re-read
            watermark = None
    offset = int(cur.get("offset") or 0)
    if size <= offset:
        cur = {"offset": offset, "size": size}
        if watermark is not None:
            cur["watermark"] = watermark
        return [], cur, watermark
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read(size - offset)
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:                      # partial trailing line: wait for more
        cur = {"offset": offset, "size": size}
        if watermark is not None:
            cur["watermark"] = watermark
        return [], cur, watermark
    consumed = chunk[: last_nl + 1]
    cur = {"offset": offset + len(consumed), "size": size}
    if watermark is not None:
        cur["watermark"] = watermark
    lines = [l.decode("utf-8", errors="replace").strip()
             for l in consumed.split(b"\n") if l.strip()]
    return lines, cur, watermark


def _poll_files() -> None:
    for agent, path in _iter_file_sources():
        key = f"{agent}|{path}"
        with _state_lock:
            cur = _state["files"].get(key)
            lines, cur2, watermark = _read_new_lines(agent, path, cur)
            _state["files"][key] = cur2
        if not lines:
            continue
        meta = None
        for raw in lines:
            try:
                d = json.loads(raw)
            except Exception:
                continue
            entry = transcripts.extract_transcript_line(agent, d)
            if entry is None:
                continue
            if watermark is not None and _ts_covered(entry.get("ts"), watermark):
                continue                 # already covered by existing conversations
            if meta is None:
                meta = _session_meta_for_file(agent, path)
            process_message(agent, meta, entry["role"], entry["text"], entry["ts"])
        with _state_lock:
            _save_state()
    with _state_lock:
        _save_state()


def _poll_hermes() -> None:
    for db in _hermes_dbs():
        key = str(db)
        with _state_lock:
            cur = dict(_state["hermes"].get(key) or {"max_id": 0})
        if not cur.get("started"):
            # first sight: start 200 rows back; pre-existing rows are history
            # unless the session has fresh conversations (watermark catch-up).
            try:
                uri = "file:" + db.resolve().as_posix() + "?mode=ro"
                conn = sqlite3.connect(uri, uri=True, timeout=4)
                conn.execute("PRAGMA query_only=ON")
                mx = conn.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
                conn.close()
            except Exception:
                continue
            cur = {"max_id": max(0, int(mx) - HERMES_LOOKBACK_ROWS),
                   "started": True, "catchup_to": int(mx)}
        try:
            uri = "file:" + db.resolve().as_posix() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=4)
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute(
                "SELECT id, session_id, role, content, timestamp FROM messages "
                "WHERE id > ? ORDER BY id", (cur["max_id"],),
            ).fetchall()
            sids = {r[1] for r in rows if r[1]}
            metas = {s: _hermes_session_meta(conn, s) for s in sids}
            conn.close()
        except Exception:
            continue
        if not rows:
            with _state_lock:
                _state["hermes"][key] = cur
            continue
        catchup_to = int(cur.get("catchup_to") or 0)
        max_id = cur["max_id"]
        for rid, sid, role, content, ts in rows:
            max_id = max(max_id, rid)
            if not sid or content is None:
                continue
            entry = transcripts.extract_transcript_line(
                "hermes", {"role": role, "content": content, "timestamp": ts})
            if entry is None:
                continue
            if rid <= catchup_to:
                with _state_lock:
                    sess = _sessions.get(sid)
                if sess is None:
                    continue             # no conversations: history = batch
                conv = Path(sess.get("conv_file") or "")
                try:
                    fresh = conv.exists() and time.time() - conv.stat().st_mtime <= FRESH_CONVERSATION_SECONDS
                except OSError:
                    fresh = False
                if not fresh:
                    continue
                if _ts_covered(entry.get("ts"), _session_watermark(sess)):
                    continue
            meta = metas.get(sid)
            if meta is None:
                continue
            process_message("hermes", meta, entry["role"], entry["text"], entry["ts"])
        if max_id >= catchup_to:
            cur.pop("catchup_to", None)
        cur["max_id"] = max_id
        with _state_lock:
            _state["hermes"][key] = cur
            _save_state()


_hermes_meta_cache: dict[str, dict] = {}


def _hermes_session_meta(conn, sid: str) -> dict | None:
    if sid in _hermes_meta_cache:
        return _hermes_meta_cache[sid]
    try:
        row = conn.execute("SELECT cwd FROM sessions WHERE id = ?", (sid,)).fetchone()
    except Exception:
        return None
    cwd = str(row[0]) if row and row[0] else ""
    meta = {"session_id": sid, "project": transcripts._project_name(cwd), "cwd": cwd}
    _hermes_meta_cache[sid] = meta
    return meta


# ───────────────────────────── background loop ────────────────────────────────

def _pid_alive(pid: int) -> bool:
    """Cross-platform PID liveness probe (never kills, unlike os.kill on Windows)."""
    if not pid or pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return True


def _claim_watch_lock() -> bool:
    """Single-instance guard: two watchers would fight over the same live files
    (duplicate messages, colliding rewrites). One watcher per machine at a time."""
    lock = DATA_DIR / "stream_watcher.lock"
    try:
        if lock.exists():
            try:
                prev = json.loads(lock.read_text(encoding="utf-8") or "{}")
            except Exception:
                prev = {}
            fresh = time.time() - float(prev.get("ts") or 0) < 300
            if (prev.get("pid") not in (None, os.getpid())
                    and fresh and _pid_alive(int(prev.get("pid") or 0))):
                print("stream_watcher: another watcher (pid %s) is running; not starting"
                      % prev.get("pid"), file=sys.stderr)
                return False
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}),
                        encoding="utf-8")
        return True
    except Exception:
        return True  # can't use the lock (read-only dir?) → proceed unguarded


def _heartbeat() -> None:
    try:
        lock = DATA_DIR / "stream_watcher.lock"
        lock.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}),
                        encoding="utf-8")
    except Exception:
        pass


def _loop() -> None:
    try:
        with _state_lock:
            _refresh_sid_projects()   # a few seconds, once, in the background
            _startup_index()          # re-index now that projects are known
    except Exception:
        pass
    while not _stop.is_set():
        try:
            _poll_files()
            _poll_hermes()
            _heartbeat()
        except Exception:
            pass
        _stop.wait(POLL_SECONDS)


def start() -> None:
    """Start the watcher daemon (idempotent, single instance per machine)."""
    global _bg_thread
    if _bg_thread is not None and _bg_thread.is_alive():
        return
    if not _claim_watch_lock():
        return
    with _state_lock:
        _load_state()
        _startup_index()
    _stop.clear()
    _bg_thread = threading.Thread(target=_loop, name="stream-watcher", daemon=True)
    _bg_thread.start()


def stop() -> None:
    _stop.set()
    with _state_lock:
        _save_state()


# ─────────────────────────────── read API ─────────────────────────────────────

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _row_sort_key(r: dict):
    return _parse_ts(r.get("last_ts") or r.get("first_ts")) or _EPOCH


def list_conversations(limit: int = 200) -> list[dict]:
    """Conversation rows (metadata, no messages), newest activity first."""
    with _state_lock:
        rows = []
        for sid, sess in _sessions.items():
            base = {"session_id": sid, "agent": sess.get("agent", ""),
                    "project": sess.get("project", ""), "cwd": sess.get("cwd", ""),
                    "file": sess.get("conv_file", "")}
            for r in sess.get("conversations") or []:
                rows.append({**base, **r, "status": "closed"})
            o = sess.get("open")
            if o:
                rows.append({**base, **_row_from_open(o), "status": "open"})
        rows.sort(key=_row_sort_key, reverse=True)
        return rows[:limit]


def get_conversation(cid: str) -> dict | None:
    """Full conversation record (with messages) by conversation id."""
    with _state_lock:
        for sid, sess in _sessions.items():
            o = sess.get("open")
            if o and o.get("cid") == cid:
                _load_open(sess)
                o = sess.get("open") or {}
                return {
                    "conversation_id": cid, "session_id": sid,
                    "agent": sess.get("agent", ""), "project": sess.get("project", ""),
                    "cwd": sess.get("cwd", ""), "title": o.get("title", ""),
                    "messages": o.get("messages") or [],
                    "msg_count": o.get("msg_count", 0),
                    "source": o.get("source", "live"), "status": "open",
                }
            for r in sess.get("conversations") or []:
                if r.get("cid") == cid:
                    return _load_closed(sess, r)
    return None


def _load_closed(sess: dict, row: dict) -> dict:
    base = {
        "conversation_id": row.get("cid", ""),
        "session_id": sess.get("session_id", ""),
        "agent": sess.get("agent", ""), "project": sess.get("project", ""),
        "cwd": sess.get("cwd", ""), "title": row.get("title", ""),
        "msg_count": row.get("msg_count", 0), "source": row.get("source", "batch"),
        "status": "closed",
    }
    for rec in _read_conversations(Path(sess.get("conv_file") or ""))[0]:
        if rec.get("conversation_id") == row.get("cid"):
            return {**base, "messages": rec.get("messages") or [],
                    "msg_count": rec.get("msg_count", len(rec.get("messages") or []))}
    return {**base, "messages": []}


def project_conversations(project: str, limit: int = 10) -> list[dict]:
    """Most recent conversations for one project (workspace star map)."""
    return [r for r in list_conversations(2000)
            if r.get("project") == project][:limit]


def stats() -> dict:
    with _state_lock:
        convs = sum(len(s.get("conversations") or []) + (1 if s.get("open") else 0)
                    for s in _sessions.values())
        return {"sessions": len(_sessions), "conversations": convs,
                "file_cursors": len(_state.get("files", {})),
                "hermes_cursors": len(_state.get("hermes", {}))}


if __name__ == "__main__":
    # Manual smoke: load + index + one poll cycle, then print a summary.
    with _state_lock:
        _load_state()
        _startup_index()
    print(json.dumps(stats(), ensure_ascii=False, indent=1))
    rows = list_conversations(5)
    for r in rows:
        print(r.get("last_ts"), r.get("agent"), r.get("project"), r.get("cid"), r.get("title"))
