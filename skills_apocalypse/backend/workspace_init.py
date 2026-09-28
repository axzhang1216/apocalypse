#!/usr/bin/env python3
"""Workspace initializer for Apocalypse — scans all Claude Code session transcripts,
summarizes each session via Claude Haiku, writes ~/.claude/apocalypse/workspace.json.

Usage:
    python workspace_init.py             # full init
    python workspace_init.py --incremental  # only analyze new sessions
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
DATA_DIR = Path.home() / ".claude" / "apocalypse"
WORKSPACE_FILE = DATA_DIR / "workspace.json"
HAIKU_MODEL = "claude-haiku-4-5-20251001"
# Grouping (discussion-decision pairs) needs stronger reasoning + stability than
# Haiku gives — under the same prompt Haiku's pair count swings wildly (e.g. 3→9
# on one session). Summarization (_summarize_long_assistant) stays on Haiku: it is
# simple and cheap.
GROUPING_MODEL = "claude-sonnet-4-5-20250929"

CONFIG_FILE = DATA_DIR / "config.json"

DEFAULT_CONFIG = {
    "export_history": True,
    "max_md_kb": 64,
    "old_kept": 3,
}


def _load_anthropic_creds():
    """Make sure ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL are set. If not, try to
    read them from ~/.claude/settings.json (Claude Code's own config)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    settings = Path.home() / ".claude" / "settings.json"
    if not settings.exists():
        return
    try:
        with open(settings, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        env = cfg.get("env", {})
        if env.get("ANTHROPIC_AUTH_TOKEN"):
            os.environ["ANTHROPIC_API_KEY"] = env["ANTHROPIC_AUTH_TOKEN"]
        if env.get("ANTHROPIC_BASE_URL"):
            os.environ["ANTHROPIC_BASE_URL"] = env["ANTHROPIC_BASE_URL"]
    except Exception:
        pass


_load_anthropic_creds()

SKIP_TYPES = {
    "attachment", "file-history-snapshot", "last-prompt", "permission-mode",
    "ai-title", "queue-operation", "hook_success", "hook_failure", "system",
}


def _project_name(cwd: str) -> str:
    if not cwd:
        return "unknown"
    s = str(cwd).replace("\\", "/").rstrip("/")
    return s.rsplit("/", 1)[-1] if s else "unknown"


def _ts_to_dt(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _fmt_dt(dt) -> str:
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOISE_PREFIXES = (
    "<local-command-caveat>", "<local-command-stdout>",
    "<command-message>", "<command-name>", "<command-args>",
    "<bash-input>", "<bash-stdout>", "<bash-stderr>",
    "You are running as a local coding agent for a Multica",
    "You are running as a chat assistant for a Multica",
    "<persisted-output>",
)


def _is_noise_user_record(d):
    """Return True if this user record is noise (not natural language from a human)."""
    if d.get("isMeta"):
        return True
    msg = d.get("message") or {}
    content = msg.get("content", [])
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return True
    # Tool results are noise (not natural language)
    if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
        return True
    # Check text content for noise prefixes
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            text = (c.get("text") or "").strip()
            if text and any(text.startswith(p) for p in NOISE_PREFIXES):
                return True
    return False


def _extract_texts(content):
    """Extract non-empty text strings from a content list."""
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    return [c.get("text", "").strip() for c in content
            if isinstance(c, dict) and c.get("type") == "text" and (c.get("text") or "").strip()]


def parse_session(path: Path):
    """Parse a .jsonl transcript, filtering noise.

    Returns dict with:
      cwd, first_user_msg, last_assistant_msg, conversation_trace,
      tools_used, msg_count, tool_call_count, first_ts, last_ts
    """
    cwd = None
    user_msgs = []       # All real user text messages (chronological)
    last_assistant_msg = ""
    tools_used = []
    msg_count = 0        # Count of natural language exchanges only
    tool_call_count = 0
    first_ts = None
    last_ts = None

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

                ts = d.get("timestamp", "")
                ts_dt = _ts_to_dt(ts)
                if ts_dt:
                    if first_ts is None:
                        first_ts = ts_dt
                    last_ts = ts_dt

                if t == "user":
                    # Skip noise records
                    if _is_noise_user_record(d):
                        continue
                    msg = d.get("message") or {}
                    content = msg.get("content", [])
                    texts = _extract_texts(content)
                    if texts:
                        combined = "\n".join(texts)
                        user_msgs.append(combined)
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

    # Build conversation trace for Haiku context
    trace_parts = []
    for i, um in enumerate(user_msgs[:3]):
        label = "User" if i == 0 else f"User[{i+1}]"
        trace_parts.append(f"[{label}] {um[:400]}")
    if last_assistant_msg:
        trace_parts.append(f"[Assistant] {last_assistant_msg[:400]}")
    conversation_trace = "\n".join(trace_parts)

    return {
        "cwd": cwd or "",
        "first_user_msg": user_msgs[0][:2000] if user_msgs else "",
        "last_assistant_msg": last_assistant_msg[:2000],
        "conversation_trace": conversation_trace,
        "tools_used": tools_used,
        "msg_count": msg_count,
        "tool_call_count": tool_call_count,
        "first_ts": _fmt_dt(first_ts),
        "last_ts": _fmt_dt(last_ts),
    }


SESSION_CATEGORIES = [
    "frontend", "backend", "devops", "debugging", "refactoring",
    "data", "docs", "config", "exploration", "ai_tools", "other",
]

CATEGORY_LABELS = {
    "frontend": "前端开发",
    "backend": "后端开发",
    "devops": "部署运维",
    "debugging": "调试修复",
    "refactoring": "重构优化",
    "data": "数据处理",
    "docs": "文档写作",
    "config": "配置环境",
    "exploration": "探索研究",
    "ai_tools": "AI工具开发",
    "other": "其他",
}

CATEGORY_PROMPT = "\n".join(
    f"- {k}: {v}" for k, v in CATEGORY_LABELS.items()
)


def summarize_session(client, session_data: dict) -> dict:
    """Call Claude Haiku to extract structured info from a session."""
    trace = session_data.get("conversation_trace", "")
    if not trace:
        trace = (session_data.get("first_user_msg", "")[:500] or "(empty session)")

    prompt = f"""You are analyzing a Claude Code session. Classify it.

Conversation:
{trace}

Tools used: {', '.join(session_data['tools_used']) or 'none'}
Total messages: {session_data['msg_count']}

Pick the best category from:
{CATEGORY_PROMPT}

Reply in JSON only (no markdown fences):
{{
  "user_goal": "one sentence, what the user wanted",
  "summary": "one sentence, what was accomplished",
  "outcome": "completed|partial|abandoned",
  "category": "one of the category keys above",
  "key_tools": ["Tool1", "Tool2"]
}}"""

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text = block.text.strip()
                break
        # Strip markdown fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
        result = json.loads(text)
        # Validate outcome
        if result.get("outcome") not in ("completed", "partial", "abandoned"):
            result["outcome"] = "partial"
        if not isinstance(result.get("key_tools"), list):
            result["key_tools"] = session_data["tools_used"][:5]
        # Validate category
        if result.get("category") not in SESSION_CATEGORIES:
            result["category"] = "other"
        return result
    except Exception as e:
        return {
            "user_goal": session_data["first_user_msg"][:80] or "unknown",
            "summary": "Analysis failed",
            "outcome": "partial",
            "category": "other",
            "key_tools": session_data["tools_used"][:5],
        }


def scan_projects():
    """Return {project_key: {cwd, name, sessions: [Path]}}."""
    projects = {}
    if not PROJECTS_DIR.exists():
        return projects
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            session_id = jsonl.stem
            # Peek at cwd
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
            key = cwd or proj_dir.name
            if key not in projects:
                projects[key] = {"cwd": cwd, "name": name, "sessions": []}
            projects[key]["sessions"].append(jsonl)
    return projects


def load_workspace() -> dict:
    if WORKSPACE_FILE.exists():
        try:
            return json.loads(WORKSPACE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "last_full_init": "", "projects": {}, "analyzed_session_ids": []}


def save_workspace(ws: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = WORKSPACE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ws, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, WORKSPACE_FILE)


def load_config() -> dict:
    """Load ~/.claude/apocalypse/config.json, falling back to defaults.

    On first call, creates the file with DEFAULT_CONFIG so users can
    discover and edit it.
    """
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_CONFIG)
    # Merge with defaults so new keys added in future versions get sensible values
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


def save_config(cfg: dict) -> None:
    """Atomically write ~/.claude/apocalypse/config.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)


def _slugify(text: str, max_len: int = 40) -> str:
    """Convert a topic title into a filename-safe slug, max 40 chars."""
    import re as _re
    s = (text or "").lower()
    s = _re.sub(r"[^a-z0-9一-鿿]+", "-", s)
    s = s.strip("-")
    if len(s) > max_len:
        s = s[:max_len].rsplit("-", 1)[0] or s[:max_len]
    return s or "untitled"


def _render_session_md(proj_title: str, session_id: str, session_meta: dict, points: list) -> str:
    """Render one session's points as a Markdown document.

    points: list of point dicts with keys topic, decision, ts, related_to, messages, id.
    """
    goal = session_meta.get("user_goal") or "(no recorded goal)"
    msg_count = session_meta.get("msg_count", 0)
    # NOTE: previously rendered an `Exported: ...` timestamp here. Removed
    # because that made every render produce different bytes, defeating the
    # hash-based rolling dedup. The file mtime is the export timestamp.

    # Title uses session goal (semantic, lets Claude find by meaning)
    title_first = goal.strip().split("\n", 1)[0].strip() or "(untitled session)"
    if len(title_first) > 120:
        title_first = title_first[:117] + "..."

    lines = [
        f"# {title_first}",
        "",
        f"**Project**: {proj_title}",
        f"**Session ID**: `{session_id}`",
        f"**Goal**: {goal}",
        f"**Messages**: {msg_count}",
        "",
        "---",
        "",
    ]

    # Map point id -> "Discussion N" for related_to cross-refs
    pid_to_n = {p.get("id", ""): i + 1 for i, p in enumerate(points)}

    for i, p in enumerate(points, 1):
        topic = (p.get("topic") or "(no topic)").strip()
        decision = (p.get("decision") or "").strip()
        related_ids = p.get("related_to") or []
        related_labels = [f"Discussion {pid_to_n[rid]}" for rid in related_ids if rid in pid_to_n]

        lines.append(f"## Discussion {i}: {topic}")
        lines.append("")
        if decision:
            lines.append(f"**Decision**: {decision}")
            lines.append("")
        if related_labels:
            lines.append(f"**Related**: {', '.join(related_labels)}")
        else:
            lines.append("**Related**: (none)")
        lines.append("")

        # Group consecutive messages by role so a long assistant monologue
        # doesn't repeat the "### Assistant" header on every paragraph.
        msg_groups = []
        current_role = None
        current_texts = []
        for m in (p.get("messages") or []):
            role = (m.get("role", "user") or "user").capitalize()
            text = (m.get("summary") or m.get("text") or "").strip()
            if not text:
                continue
            if role == current_role:
                current_texts.append(text)
            else:
                if current_role is not None:
                    msg_groups.append((current_role, current_texts))
                current_role = role
                current_texts = [text]
        if current_role is not None:
            msg_groups.append((current_role, current_texts))

        for role, texts in msg_groups:
            lines.append(f"### {role}")
            lines.append("")
            for text in texts:
                for ml in text.split("\n"):
                    ml = ml.strip()
                    if ml:
                        lines.append(f"> {ml}")
                lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_apocalypse_readme() -> str:
    """The .apocalypse/README.md describing the directory contents."""
    return """\
# Apocalypse 项目历史

此目录由 [Apocalypse](https://...) 自动维护。每个 `.md` 文件是一次
Claude Code session 的讨论归档，按"讨论/决策"分段。

**用途**：让未来的 Claude 看到本项目之前讨论过什么、做了什么决策。

**注意事项**：
- 主 md（不带 `.old` 后缀）是当前活跃的，会被 Apocalypse 自动重写
- `.old.*.md` 是历史滚动文件，不会被 Claude 默认读到
- 每次 `update workspace` 时此目录会被重新生成

**不要**手动编辑此目录——下次更新会覆盖。
"""


def _roll_session_md(main_path: Path, content: str, max_kb: int, old_kept: int) -> None:
    """Write a session md, rolling old content into .old.N.md siblings if oversized.

    `main_path` is the file we want to write (e.g. `…/2026-06-15-abc12345-init.md`).
    `content` is the newly-rendered md text.
    `max_kb` is the soft cap; `old_kept` is how many `.old.N.md` siblings to keep.

    Key invariants:
      1. We only ROLL when the new content would not fit AND the new content
         differs from the existing main.  Re-running export with unchanged
         points must not churn the .old.N.md files.
      2. On Windows, `Path.exists()` returns stale True for files that have
         just been renamed (a quirk of the OS's open-file handle caching).
         We use `iterdir()` instead — it reflects on-disk reality reliably.
    """
    import hashlib
    cap_bytes = max_kb * 1024
    base = main_path.stem  # `foo.md` -> `foo`, so old.N siblings are `foo.old.1.md`
    parent = main_path.parent
    new_size = len(content.encode("utf-8"))
    new_hash = hashlib.sha1(content.encode("utf-8")).hexdigest()

    # Snapshot what's actually on disk (avoid Windows Path.exists() staleness).
    siblings = set(f.name for f in parent.iterdir()) if parent.exists() else set()

    # If main already exists with the same content, no-op entirely.
    main_name = main_path.name
    if main_name in siblings:
        try:
            existing_hash = hashlib.sha1(main_path.read_bytes()).hexdigest()
            if existing_hash == new_hash:
                return  # no change — don't churn .old.* files
        except Exception:
            pass

    if new_size <= cap_bytes:
        # Fits — plain write, no rolling. Use binary mode to avoid Python's
        # text-mode CRLF normalisation on Windows, which would corrupt the
        # hash check above and silently change file sizes.
        tmp = main_path.with_suffix(main_path.suffix + ".tmp")
        tmp.write_bytes(content.encode("utf-8"))
        os.replace(tmp, main_path)
        return

    # Oversized — roll.
    # Step 1: delete any .old.N.md beyond old_kept range
    for n in range(old_kept + 1, old_kept + 10):  # bounded scan; no infinite loop
        name = f"{base}.old.{n}.md"
        if name not in siblings:
            break
        try:
            (parent / name).unlink()
            siblings.discard(name)
        except Exception as e:
            print(f"[history-export] could not delete {parent / name}: {e}", file=sys.stderr)

    # Step 2: shift kept ones up: .old.(N) -> .old.(N+1), reverse order to avoid clobbering
    for n in range(old_kept, 0, -1):
        src_name = f"{base}.old.{n}.md"
        dst_name = f"{base}.old.{n + 1}.md"
        if src_name in siblings:
            try:
                os.replace(parent / src_name, parent / dst_name)
                siblings.discard(src_name)
                siblings.add(dst_name)
            except Exception as e:
                print(f"[history-export] could not roll {parent / src_name} -> {parent / dst_name}: {e}", file=sys.stderr)

    # Step 3: move current main to .old.1 (only if main truly exists on disk)
    if main_name in siblings:
        dst_name = f"{base}.old.1.md"
        try:
            os.replace(main_path, parent / dst_name)
            siblings.discard(main_name)
            siblings.add(dst_name)
        except Exception as e:
            print(f"[history-export] could not move {main_path} -> {parent / dst_name}: {e}", file=sys.stderr)

    # Step 4: write new main
    tmp = main_path.with_suffix(main_path.suffix + ".tmp")
    tmp.write_bytes(content.encode("utf-8"))
    os.replace(tmp, main_path)


APOCALYPSE_HISTORY_MARKER_START = "<!-- APOCALYPSE-HISTORY:START -->"
APOCALYPSE_HISTORY_MARKER_END = "<!-- APOCALYPSE-HISTORY:END -->"

APOCALYPSE_HISTORY_BLOCK = """\
<!-- APOCALYPSE-HISTORY:START -->
## Project History (Apocalypse)

此项目过往的 Claude Code session 已自动归档到 `.apocalypse/`，按"讨论/决策"
分章节组织（每 session 一个 md）。当用户问到本项目之前做过什么、决定过什么、
讨论过什么时，**先读** `.apocalypse/*.md`（不带 `.old` 后缀的）再回答。

- 主文件（活跃）= `.apocalypse/*.md`（无 `.old` 后缀）
- 历史滚动文件 = `.apocalypse/*.old.*.md`（默认不读，必要时翻）
- 目录说明 = `.apocalypse/README.md`

**不要修改** `.apocalypse/` 下的文件——下次 `update workspace` 会被覆盖。
<!-- APOCALYPSE-HISTORY:END -->"""


def _update_claude_md(cwd: Path) -> None:
    """Idempotently insert or replace the APOCALYPSE-HISTORY block in cwd/CLAUDE.md.

    - File missing: create with just the block.
    - Block present (START/END markers found): replace that range only.
    - Block absent: append at end with a leading blank line.
    Writes atomically; silent skip on permission errors.
    """
    import re as _re
    target = cwd / "CLAUDE.md"
    try:
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
    except Exception:
        return  # unreadable — skip silently

    if APOCALYPSE_HISTORY_MARKER_START in existing and APOCALYPSE_HISTORY_MARKER_END in existing:
        # Replace the block in place; preserve everything else verbatim
        pattern = _re.compile(
            _re.escape(APOCALYPSE_HISTORY_MARKER_START) + r".*?" + _re.escape(APOCALYPSE_HISTORY_MARKER_END),
            _re.DOTALL,
        )
        new_content = pattern.sub(APOCALYPSE_HISTORY_BLOCK, existing)
    else:
        # Append with leading blank line
        if existing and not existing.endswith("\n"):
            existing += "\n"
        new_content = existing + ("\n" if existing else "") + APOCALYPSE_HISTORY_BLOCK + "\n"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(new_content, encoding="utf-8")
        os.replace(tmp, target)
    except Exception as e:
        print(f"[history-export] could not write {target}: {e}", file=sys.stderr)


def _update_gitignore(cwd: Path) -> None:
    """Ensure cwd/.gitignore contains a line for `.apocalypse/`.

    Idempotent: uses regex to detect existing entry in multi-line mode.
    Silent skip on permission errors.
    """
    import re as _re
    target = cwd / ".gitignore"
    pattern = _re.compile(r"^\.apocalypse/?$", _re.M)
    try:
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
    except Exception:
        return

    if pattern.search(existing):
        return  # already covered

    addition = ".apocalypse/\n"
    if existing and not existing.endswith("\n"):
        existing += "\n"
    new_content = existing + addition

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(new_content, encoding="utf-8")
        os.replace(tmp, target)
    except Exception as e:
        print(f"[history-export] could not write {target}: {e}", file=sys.stderr)


def export_history() -> dict:
    """Export each project's discussion-decision points as markdown files
    under `<project-cwd>/.apocalypse/`, update per-project CLAUDE.md marked
    block and .gitignore. Idempotent — safe to re-run.

    Returns a summary dict {projects_processed, sessions_exported, skipped}.
    """
    cfg = load_config()
    if not cfg.get("export_history", True):
        print("[history-export] disabled in config.json (export_history=false), skipping")
        return {"projects_processed": 0, "sessions_exported": 0, "skipped": 0}

    max_kb = int(cfg.get("max_md_kb", 64))
    old_kept = int(cfg.get("old_kept", 3))

    ws = load_workspace()
    summary = {"projects_processed": 0, "sessions_exported": 0, "skipped": 0}

    for proj_key, proj in ws.get("projects", {}).items():
        cwd_str = proj.get("cwd", "")
        if not cwd_str:
            summary["skipped"] += 1
            continue
        cwd = Path(cwd_str)
        if not cwd.exists() or not cwd.is_dir():
            print(f"[history-export] skip {proj.get('title', proj_key)}: cwd not accessible ({cwd})", file=sys.stderr)
            summary["skipped"] += 1
            continue

        # Always update .gitignore first so any later crash doesn't leave md untracked.
        _update_gitignore(cwd)

        apo_dir = cwd / ".apocalypse"
        try:
            apo_dir.mkdir(parents=True, exist_ok=True)
            (apo_dir / "README.md").write_text(_render_apocalypse_readme(), encoding="utf-8")
        except Exception as e:
            print(f"[history-export] could not create {apo_dir}: {e}", file=sys.stderr)
            summary["skipped"] += 1
            continue

        proj_title = proj.get("title") or proj.get("name") or proj_key
        analyzed = proj.get("analyzed_sessions", {})
        points_by_sid = {}
        for p in proj.get("points", []) or []:
            sid = p.get("session_id")
            if sid:
                points_by_sid.setdefault(sid, []).append(p)

        n_sessions = 0
        for sid, s_meta in analyzed.items():
            session_points = points_by_sid.get(sid, [])
            if not session_points:
                continue
            # Skip sessions whose points have no messages (extract-points hasn't run)
            if not all(p.get("messages") for p in session_points):
                continue

            # Build md
            md_text = _render_session_md(proj_title, sid, s_meta, session_points)
            # Filename: <date>-<sid8>-<slug>.md (date from first point's ts)
            first_ts = session_points[0].get("ts") or s_meta.get("ts") or ""
            date_part = first_ts[:10] if first_ts else "unknown-date"
            slug = _slugify(session_points[0].get("topic", "") or s_meta.get("user_goal", ""))
            md_path = apo_dir / f"{date_part}-{sid[:8]}-{slug}.md"

            try:
                _roll_session_md(md_path, md_text, max_kb, old_kept)
                n_sessions += 1
            except Exception as e:
                print(f"[history-export] could not write {md_path}: {e}", file=sys.stderr)

        # Update CLAUDE.md marked block last (only if we managed to write something
        # or if the directory exists — i.e. always when we got this far)
        try:
            _update_claude_md(cwd)
        except Exception as e:
            print(f"[history-export] could not update CLAUDE.md in {cwd}: {e}", file=sys.stderr)

        summary["projects_processed"] += 1
        summary["sessions_exported"] += n_sessions
        if n_sessions:
            print(f"[history-export] {proj_title}: {n_sessions} session(s) → {apo_dir}")

    return summary


def run(incremental: bool = False):
    try:
        import anthropic
    except ImportError:
        print(json.dumps({"type": "error", "message": "anthropic SDK not installed. Run: pip install anthropic"}), flush=True)
        sys.exit(1)

    client = anthropic.Anthropic()
    ws = load_workspace()
    already_analyzed = set(ws.get("analyzed_session_ids", []))

    raw_projects = scan_projects()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    total_new_sessions = 0
    total_projects_done = 0

    for proj_key, proj_info in raw_projects.items():
        sessions = proj_info["sessions"]
        cwd = proj_info["cwd"]
        name = proj_info["name"]

        # Initialize project record if missing
        if proj_key not in ws["projects"]:
            ws["projects"][proj_key] = {
                "name": name,
                "cwd": cwd,
                "title": "",
                "tags": [],
                "sessions": [],
                "total_messages": 0,
                "total_tool_calls": 0,
                "first_seen": "",
                "last_active": "",
                "analyzed_sessions": {},
            }

        proj_record = ws["projects"][proj_key]
        proj_record["sessions"] = [s.stem for s in sessions]

        new_sessions_in_project = 0
        category_counts = {}

        for jsonl in sessions:
            session_id = jsonl.stem
            if incremental and session_id in already_analyzed:
                continue

            data = parse_session(jsonl)

            # Update project aggregates
            proj_record["total_messages"] = proj_record.get("total_messages", 0) + data["msg_count"]
            proj_record["total_tool_calls"] = proj_record.get("total_tool_calls", 0) + data["tool_call_count"]

            # Track timestamps
            if data["first_ts"]:
                if not proj_record["first_seen"] or data["first_ts"] < proj_record["first_seen"]:
                    proj_record["first_seen"] = data["first_ts"]
            if data["last_ts"]:
                if not proj_record["last_active"] or data["last_ts"] > proj_record["last_active"]:
                    proj_record["last_active"] = data["last_ts"]

            # Summarize via Claude Haiku
            analysis = summarize_session(client, data)
            proj_record["analyzed_sessions"][session_id] = {
                "summary": analysis["summary"],
                "user_goal": analysis["user_goal"],
                "outcome": analysis["outcome"],
                "category": analysis.get("category", "other"),
                "key_tools": analysis["key_tools"],
                "msg_count": data["msg_count"],
                "ts": data["last_ts"] or data["first_ts"] or now_str,
            }

            if session_id not in ws["analyzed_session_ids"]:
                ws["analyzed_session_ids"].append(session_id)
            already_analyzed.add(session_id)

            new_sessions_in_project += 1
            total_new_sessions += 1

            # Count categories for project-level themes
            cat = analysis.get("category", "other")
            category_counts[cat] = category_counts.get(cat, 0) + 1

        if new_sessions_in_project > 0:
            # Build category-based themes for this project
            total_categorized = sum(category_counts.values())
            top_cats = sorted(category_counts.items(), key=lambda x: x[1], reverse=True)
            project_themes = [CATEGORY_LABELS.get(c, c) for c, _ in top_cats[:4]]

            # Merge with existing top_themes (for incremental runs)
            existing = set(proj_record.get("top_themes") or [])
            existing.update(project_themes)
            proj_record["top_themes"] = list(existing)[:5]

            total_projects_done += 1
            print(json.dumps({
                "type": "project_done",
                "project": name,
                "sessions": new_sessions_in_project,
                "top_themes": project_themes,
            }, ensure_ascii=False), flush=True)

        # Save after each project so progress is durable
        save_workspace(ws)

    if not incremental:
        ws["last_full_init"] = now_str

    ws["last_analysis"] = now_str
    save_workspace(ws)

    print(json.dumps({
        "type": "done",
        "total_projects": total_projects_done,
        "total_sessions": total_new_sessions,
    }, ensure_ascii=False), flush=True)


def dump_summaries():
    """Output compact project summaries for harness analysis."""
    ws = load_workspace()
    projects = []
    for proj_key, proj in ws.get("projects", {}).items():
        analyzed = proj.get("analyzed_sessions", {})
        if not analyzed:
            continue
        sample_goals = []
        cat_breakdown = {}
        all_tools = set()
        for sid, s in analyzed.items():
            goal = s.get("user_goal", "")
            if goal and len(sample_goals) < 8:
                sample_goals.append(goal)
            cat = s.get("category", "other")
            cat_breakdown[cat] = cat_breakdown.get(cat, 0) + 1
            for t in (s.get("key_tools") or []):
                all_tools.add(t)
        projects.append({
            "key": proj_key,
            "folder_name": proj.get("name", ""),
            "title": proj.get("title", ""),
            "tags": proj.get("tags", []),
            "session_count": len(analyzed),
            "date_range": f"{proj.get('first_seen', '?')[:10]} ~ {proj.get('last_active', '?')[:10]}",
            "sample_goals": sample_goals,
            "category_breakdown": cat_breakdown,
            "common_tools": sorted(all_tools)[:10],
            "total_messages": proj.get("total_messages", 0),
            "current_tags": proj.get("tags", []),
        })
    projects.sort(key=lambda p: p["session_count"], reverse=True)
    print(json.dumps(
        {"type": "project_summaries", "total_projects": len(projects), "projects": projects},
        ensure_ascii=False, indent=2,
    ), flush=True)


def set_themes():
    """Read project metadata from stdin and update workspace.json.

    Input format:
    {
      "projects": {
        "/path/to/proj": {
          "title": "项目标题",
          "tags": ["标签1", "标签2"]
        }
      }
    }
    """
    data = json.loads(sys.stdin.read())
    ws = load_workspace()
    count = 0
    projects_map = data.get("projects", {})
    for proj_key, meta in projects_map.items():
        if proj_key in ws.get("projects", {}):
            if "title" in meta:
                ws["projects"][proj_key]["title"] = meta["title"]
            if "tags" in meta:
                ws["projects"][proj_key]["tags"] = meta["tags"]
                ws["projects"][proj_key]["top_themes"] = meta["tags"]  # compat
            count += 1
    save_workspace(ws)
    print(json.dumps({"type": "projects_updated", "count": count}, ensure_ascii=False), flush=True)


def _parse_transcript_for_points(path: Path):
    """Parse a transcript into user_msgs (filtered user messages) and transcript
    (non-noise user + assistant text entries, preserving order).
    Each entry has {role, ts, text, line_no}; user_msgs also has {idx} for its
    position in the filtered user-only list.
    """
    user_msgs = []
    transcript = []
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
                if t == "user" and _is_noise_user_record(d):
                    continue
                msg = d.get("message") or {}
                cb = msg.get("content", [])
                if isinstance(cb, str):
                    cb = [{"type": "text", "text": cb}]
                if not isinstance(cb, list):
                    continue
                texts = [c.get("text", "").strip() for c in cb
                         if isinstance(c, dict) and c.get("type") == "text" and (c.get("text") or "").strip()]
                if not texts:
                    continue
                combined = "\n".join(texts)
                ts = d.get("timestamp", "") or ""
                entry = {"role": t, "ts": ts, "text": combined, "line_no": line_no}
                transcript.append(entry)
                if t == "user":
                    user_msgs.append({**entry, "idx": len(user_msgs)})
    except Exception:
        return [], []
    return user_msgs, transcript


def _slice_messages(transcript, user_msgs, first_user_idx, next_user_idx):
    """Slice transcript entries from user_msgs[first_user_idx] (inclusive) up to
    user_msgs[next_user_idx] (exclusive), preserving original order.
    Returns [{role, ts, text}, ...] suitable for point.messages.
    """
    if first_user_idx >= len(user_msgs):
        return []
    first_line = user_msgs[first_user_idx]["line_no"]
    last_line = user_msgs[next_user_idx]["line_no"] if next_user_idx < len(user_msgs) else float("inf")
    out = []
    for e in transcript:
        if e["line_no"] < first_line:
            continue
        if e["line_no"] >= last_line:
            break
        out.append({"role": e["role"], "ts": e["ts"], "text": e["text"]})
    return out


def _summarize_long_assistant(client, text):
    """Summarize a long assistant message via Haiku (1-2 sentences, same language)."""
    try:
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content":
                "Summarize this Claude Code reply in 1-2 sentences in the same language as the original:\n\n" + text[:1500]}],
        )
        for block in resp.content:
            if hasattr(block, "text"):
                return block.text.strip()
    except Exception:
        return None
    return None


def _summarize_messages(client, messages, threshold=200):
    """Pre-summarize long assistant messages. Mutates in place, adding a `summary`
    field to entries that are summarized (full `text` is preserved)."""
    for i, m in enumerate(messages):
        if m["role"] == "assistant" and len(m["text"]) > threshold:
            s = _summarize_long_assistant(client, m["text"])
            if s:
                messages[i]["summary"] = s


def _haiku_group_user_messages(client, user_msgs, session_meta, transcript=None):
    """Ask Haiku to group user messages into discussion-decision pairs.

    If `transcript` is provided (list of {role, ts, text, line_no} entries in
    chronological order, including both user and assistant text), the prompt
    shows the interleaved U/A stream so Haiku can judge when a topic truly
    ended (decision reached, code ran cleanly, user moved on). Without
    transcript, falls back to the legacy user-only prompt.

    Returns list of {user_indices, topic, decision} on success, [] on failure.
    """
    session_goal = session_meta.get("user_goal", "unknown")

    if transcript is None:
        # Legacy user-only path (kept for back-compat; extract_points always
        # passes transcript now).
        user_list = "\n".join(
            "[U" + str(u["idx"]) + "] " + u["text"][:300] for u in user_msgs
        )
        prompt = (
            "You are analyzing a Claude Code session's user messages in order.\n"
            "The user talks about several different topics across the conversation. "
            "Each time the user SWITCHES to a new topic, that starts a NEW "
            "discussion-decision pair.\n"
            "Rules:\n"
            "- Consecutive user messages on the SAME topic = one discussion-decision pair (merge them).\n"
            "- The moment the user asks about a different topic, cut. Previous messages belong to the previous pair; the new message starts a new pair.\n"
            "- If the entire session is one continuous topic, return a single group with all user message indices.\n"
            "Session goal: " + session_goal + "\n"
            "User messages in order:\n" + user_list + "\n"
            "Reply in JSON only (no markdown fences):\n"
            "{\n"
            '  "groups": [\n'
            "    {\n"
            '      "user_indices": [0, 1, 2],\n'
            '      "topic": "short topic label (max 25 chars)",\n'
            '      "decision": "what was decided or done in this exchange (one sentence)",\n'
            '      "related_topics": ["topic label of another group in this list that is causally/topically related"]\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "Rules:\n"
            "- user_indices are zero-based, in ascending order.\n"
            "- Every user index must appear in exactly one group (cover all indices).\n"
            "- topic should be short and specific.\n"
            "- decision is concrete: what tool, what approach, what was changed, or current state.\n"
            "- related_topics links groups within this session that are related; leave [] if none."
        )
    else:
        # Build interleaved U/A stream: user messages get their stable user_idx;
        # assistant messages get sequential A0..An markers.
        user_list = "\n".join(
            "[U" + str(u["idx"]) + "] " + u["text"][:500] for u in user_msgs
        )

        prompt = (
            "You are analyzing a Claude Code session's USER messages in order. "
            "Group them into \"discussion-decision pairs\".\n"
            "\n"
            "GROUPING RULES:\n"
            "\n"
            "STRONG DEFAULT — MERGE. When unsure whether to cut, DO NOT cut (keep merging). "
            "A 30-50 message session typically yields only 2-4 pairs. Most user messages are "
            "research steps, sub-tasks, or iterations that STAY in the current pair.\n"
            "\n"
            "Key concept — THEME GOAL: the user's current focus — ONE feature being built/refined, "
            "OR ONE research topic being analyzed, OR ONE work phase (e.g. paper-writing). "
            "A discussion-decision pair = ALL consecutive work on ONE theme goal.\n"
            "\n"
            "1. SAME theme goal → ONE pair. FORCE-MERGE everything below into the current pair; do NOT cut between sub-steps just because the sub-task label changes:\n"
            "   - Sub-stages / sub-questions / debugging iterations / retries of one feature.\n"
            "   - Research sub-stages of ONE topic: define → train → visualize → debug → quantitative analysis → compare with old results. ALL one pair, even across 25+ messages.\n"
            "   - Plotting a figure + 6 messages fixing its latitude orientation. ONE pair.\n"
            "   - \"写文章 ABC 一步步来\" repeated many times. ONE pair.\n"
            "   - Wrap-up actions (\"提交github吧\"/\"commit it\") at the end of a feature. Same pair.\n"
            "\n"
            "2. NOISE / TRANSITION messages — attach to the surrounding theme goal; never create a boundary:\n"
            "   - System continuation banners (\"This session is being continued from a previous conversation...\") — attach to whatever theme goal they sit inside (usually the one that follows). Every user message, INCLUDING banners/noise, MUST appear in exactly one group — never drop an index.\n"
            "   - Short acknowledgements (\"好\"/\"ok\"/\"好的\"/\"继续\"/\"嗯\"/\"haode\").\n"
            "   - Context/availability queries (\"我还能看到之前的聊天记录吗\"/\"你能读图吗\").\n"
            "   - Tool-interrupt markers (\"[Request interrupted by user]\").\n"
            "   - Pure status messages (\"连好了\"/\"太乙连上了\").\n"
            "\n"
            "3. NEW pair starts at Ui ONLY when Ui begins a DIFFERENT theme goal. Distinguish two cases:\n"
            "   - NEW FEATURE REQUEST (\"我要做一个X\"/\"加一个X功能\"/\"add a heatmap\"/\"fix sidebar\") or NEW RESEARCH TOPIC/PHASE (MWP analysis → WPSH analysis → paper writing) → CUT, new pair.\n"
            "   - RESEARCH STEP / implementation sub-task of the current topic (\"训练\"/\"画图\"/\"debug\"/\"定量分析\"/\"核查\"/\"再remind我\") → STAY in current pair. NEVER cut between research sub-steps of one topic, even if the sub-task name changes (define → train → plot → analyze are ONE pair).\n"
            "   - A standalone independent DECISION (\"先短期吧...\") or a CORRECTION/REDEFINITION (\"你理解错了...\") → its own single-message pair.\n"
            "   - A short independent feature the user raises and resolves in one message (\"add a heatmap\"/\"fix sidebar click\"/\"show chat log\") → own pair.\n"
            "   NOTE: \"下面做下一件事\" / \"下面做X\" only cuts if X is a DIFFERENT theme goal. If X continues the same tool/phase (e.g. launcher → workspace-update → notifications are all one CLI-tooling phase), do NOT cut.\n"
            "\n"
            "WORKED EXAMPLE A — research session. NOTE how CHECKING/VERIFICATION messages and research STEPS all MERGE into one pair:\n"
            "  [U0] 用CMIP数据重做SOM分类  [U1] 训练n=5看DBI  [U2] 我还能看到之前的聊天记录吗  [U3] 画出5类气象场图  [U4] 图纬度反了修一下  [U5] 核查一下，画的是mean还是centroid  [U6] 做定量分析按thesis格式  [U7] 下面搜文献，副高未来怎么变  [U8] 好，开始写文章\n"
            "  Correct pairs: {[0,1,2,3,4,5,6] \"MWP analysis\"} {[7] \"WPSH analysis\"} {[8] \"paper drafting\"}\n"
            "  Why: U2 (checking history) and U5 (verifying method) are part of the MWP research → MERGE. U1/U3/U4/U6 are research steps → MERGE. ALL of U0-U6 = ONE pair (7 messages). U7 = NEW topic. U8 = NEW phase.\n"
            "\n"
            "WORKED EXAMPLE B — feature-dev session:\n"
            "  [U0] 打磨init功能  [U1] 加报告  [U2] 继续调  [U3] 更新github  [U4] 下面开发启动器  [U5] 调试启动器  [U6] 好下面做下一件事，加workspace更新逻辑  [U7] 加活跃度热力图  [U8] 做搜索框  [U9] 先短期吧  [U10] 改成点击星云出侧边栏  [U11] 侧边栏展示聊天记录\n"
            "  Correct pairs: {[0-3] \"init refinement\"} {[4,5,6] \"CLI tooling (launcher+update)\"} {[7] \"heatmap\"} {[8,9] \"search\"} {[10] \"sidebar interaction\"} {[11] \"chat-log display\"}\n"
            "  Why: U6 says \"下面做下一件事\" but workspace-update is still the SAME CLI-tooling phase as launcher → MERGED (do NOT cut on a transition phrase alone when it is the same phase). heatmap/search/sidebar/chat are DIFFERENT UI features → separate pairs.\n"
            "\n"
            "Apply this same logic to the session below.\n"
            "\n"
            "FINAL GUIDANCE — bias toward FEWER, LARGER pairs. A whole research arc (define+train+visualize+analyze one topic) is ONE pair. A whole CLI/tooling phase (launcher+update+notifications) is ONE pair. Cut separate pairs ONLY for genuinely different product features (heatmap/search/sidebar/chat) or different research topics/phases.\n"
            "\n"
            f"Session goal: {session_goal}\n"
            "\n"
            f"User messages in order ({len(user_msgs)} total; "
            f"indices 0..{len(user_msgs)-1}):\n"
            f"{user_list}\n"
            "\n"
            "Reply in JSON only (no markdown fences):\n"
            "{\n"
            '  "groups": [\n'
            "    {\n"
            '      "user_indices": [0, 1, 2],\n'
            '      "topic": "short topic label of the LARGE TASK (max 25 chars)",\n'
            '      "decision": "what was ultimately decided or built for this whole task (1 sentence)"\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "Rules:\n"
            "- user_indices are zero-based, in ascending order.\n"
            "- Every user index must appear in exactly one group (cover all indices).\n"
            "- topic labels the LARGE TASK, not a sub-step.\n"
            "- decision summarises the final state of the whole task, not a mid-task observation."
        )
    try:
        resp = client.messages.create(
            model=GROUPING_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = ""
        for block in resp.content:
            if hasattr(block, "text"):
                text = block.text.strip()
                break
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
        result = json.loads(text)
    except Exception:
        return None
    return result.get("groups") or []


def extract_points():
    """Extract discussion-decision pairs from sessions using Haiku.

    Per-session flow:
      1. Parse transcript into user_msgs + transcript entries (text-only, no noise).
      2. If no user messages, skip.
      3. Ask Haiku to group user messages by topic (consecutive same-topic merges).
      4. For each group, slice the transcript (user + assistant text) between the
         first user_msg of the group and the first user_msg of the next group.
      5. Pre-summarize long assistant messages in each slice (so runtime display
         is fast — no Haiku calls on the hot path).
      6. Save as point.messages on the project's points array.

    Re-runs only re-process sessions whose existing points lack the `messages` field.
    """
    try:
        import anthropic
    except ImportError:
        print(json.dumps({"type": "error", "message": "anthropic SDK not installed"}, ensure_ascii=False), flush=True)
        sys.exit(1)

    client = anthropic.Anthropic()
    ws = load_workspace()
    total_extracted = 0
    total_summarized = 0

    for proj_key, proj in ws.get("projects", {}).items():
        analyzed = proj.get("analyzed_sessions", {})
        all_points = proj.get("points", [])
        points_by_sid = {}
        for p in all_points:
            points_by_sid.setdefault(p.get("session_id"), []).append(p)

        for sid, s in analyzed.items():
            sess_points = points_by_sid.get(sid, [])
            # Skip if every existing point for this session already has messages
            if sess_points and all(p.get("messages") for p in sess_points):
                continue

            msg_count = s.get("msg_count", 0)
            if msg_count < 4:
                continue

            # Read transcript
            path = None
            for jf in PROJECTS_DIR.glob("*/" + sid + ".jsonl"):
                path = jf
                break
            if not path:
                continue

            user_msgs, transcript = _parse_transcript_for_points(path)
            if not user_msgs:
                continue

            groups = _haiku_group_user_messages(client, user_msgs, s, transcript=transcript)
            if not groups:
                continue

            # Build new points for this session, replacing any existing ones
            new_points_for_sid = []
            topic_to_id = {}
            for g_idx, g in enumerate(groups):
                idxs = g.get("user_indices") or []
                idxs = [i for i in idxs if 0 <= i < len(user_msgs)]
                if not idxs:
                    continue
                first_idx = idxs[0]
                if g_idx + 1 < len(groups):
                    next_first = (groups[g_idx + 1].get("user_indices") or [len(user_msgs)])[0]
                else:
                    next_first = len(user_msgs)
                if next_first <= first_idx:
                    next_first = len(user_msgs)
                msgs = _slice_messages(transcript, user_msgs, first_idx, next_first)
                _summarize_messages(client, msgs)
                total_summarized += sum(1 for m in msgs if m.get("summary"))
                pid = sid[:8] + "_" + str(total_extracted)
                total_extracted += 1
                topic = (g.get("topic") or "").strip()[:60]
                decision = (g.get("decision") or "").strip()
                ts = user_msgs[first_idx].get("ts") or s.get("ts", "")
                point = {
                    "id": pid,
                    "session_id": sid,
                    "topic": topic,
                    "decision": decision,
                    "ts": ts,
                    "related_to": [],
                    "messages": msgs,
                }
                new_points_for_sid.append(point)
                topic_to_id[topic] = pid

            # Cross-references within this session
            for g, point in zip(groups, new_points_for_sid):
                related_ids = []
                for rt in (g.get("related_topics") or []):
                    if rt in topic_to_id:
                        related_ids.append(topic_to_id[rt])
                point["related_to"] = related_ids

            # Replace this session's points with the newly built ones
            all_points = [p for p in all_points if p.get("session_id") != sid]
            all_points.extend(new_points_for_sid)

            print(json.dumps({
                "type": "points_extracted",
                "session": sid[:8],
                "project": proj.get("name", ""),
                "points": len(new_points_for_sid),
            }, ensure_ascii=False), flush=True)

        proj["points"] = all_points
        save_workspace(ws)

    print(json.dumps({
        "type": "done",
        "total_points": total_extracted,
        "total_summarized": total_summarized,
    }, ensure_ascii=False), flush=True)




if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialize Apocalypse workspace data")
    parser.add_argument("--incremental", action="store_true",
                        help="Only analyze sessions not yet in workspace.json")
    parser.add_argument("--dump-summaries", action="store_true",
                        help="Output compact project summaries for harness analysis")
    parser.add_argument("--set-themes", action="store_true",
                        help="Read theme mapping from stdin and update workspace.json")
    parser.add_argument("--extract-points", action="store_true",
                        help="Extract discussion-decision pairs from sessions")
    parser.add_argument("--export-history", action="store_true",
                        help="Export discussion-decision points as .apocalypse/ markdown per project")
    args = parser.parse_args()
    if args.dump_summaries:
        dump_summaries()
    elif args.set_themes:
        set_themes()
    elif args.export_history:
        result = export_history()
        print(json.dumps({"type": "done", **result}, ensure_ascii=False))
    elif args.extract_points:
        extract_points()
    else:
        run(incremental=args.incremental)
