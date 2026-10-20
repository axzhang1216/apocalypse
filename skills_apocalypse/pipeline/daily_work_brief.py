#!/usr/bin/env python3
"""Write a one-day brief of work done with AI agents, plus what was left open.

Evidence comes from conversations the existing pipeline already produced:

  live:  ~/.claude/apocalypse/conversations/*.conversations.jsonl
  batch: skills_apocalypse/data/conversations_output/  (or APOCALYPSE_BATCH_CONVERSATIONS_DIR)
  IR:    skills_apocalypse/data/knowledge_ir/          (optional; open questions and tasks)

A conversation counts for a day when any of its messages falls on that local
calendar day. Live files win over batch files for the same session. The
analysis model is the one configured in the Apocalypse harness. It may only
use the evidence this script supplies.

Usage:
  python daily_work_brief.py                  # yesterday, local time
  python daily_work_brief.py --date 2026-10-10
  python daily_work_brief.py --dry-run        # print the evidence, no model call
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
import analysis_harness  # noqa: E402

SKILL = Path(__file__).resolve().parents[1]
LIVE_DIR = Path.home() / ".claude" / "apocalypse" / "conversations"
DEFAULT_BATCH = SKILL / "data" / "conversations_output"
DEFAULT_IR = SKILL / "data" / "knowledge_ir"
OUT_DIR = Path.home() / ".claude" / "apocalypse" / "briefs"

USER_CHARS = 400
CONCLUSION_CHARS = 500
IR_ITEM_CHARS = 240
MAX_EPISODES = 40


def local_zone() -> ZoneInfo:
    name = (os.environ.get("APOCALYPSE_LOCAL_TZ") or "").strip()
    if name:
        return ZoneInfo(name)
    tz = datetime.now().astimezone().tzinfo
    if isinstance(tz, ZoneInfo):
        return tz
    key = getattr(tz, "key", None)
    if key:
        return ZoneInfo(key)
    return ZoneInfo("Asia/Shanghai")


def parse_ts(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def clip(text, limit: int) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def split_lines(text) -> list[str]:
    """One bullet per sentence/clause; drop trailing semicolons."""
    parts = re.split(r"(?<=[。！？!?；;])\s*", str(text or ""))
    return [p.strip().rstrip("；;").strip() for p in parts if p.strip()]


def progress_items(value) -> list[str]:
    """New briefs return a list; old ones returned one prose string."""
    if isinstance(value, list):
        items = [clip(v, 120) for v in value if isinstance(v, (str, int, float)) and str(v).strip()]
    else:
        items = split_lines(value)
    return items[:6]


def batch_dir() -> Path:
    env = os.environ.get("APOCALYPSE_BATCH_CONVERSATIONS_DIR", "").strip()
    return Path(env).expanduser() if env else DEFAULT_BATCH


def ir_dir() -> Path:
    env = os.environ.get("APOCALYPSE_KNOWLEDGE_IR_DIR", "").strip()
    return Path(env).expanduser() if env else DEFAULT_IR


def session_key(path: Path, row: dict) -> str:
    sid = str(row.get("session_id") or "").strip()
    if sid:
        return sid
    name = path.name
    suffix = ".conversations.jsonl"
    return name[: -len(suffix)] if name.endswith(suffix) else path.stem


def iter_jsonl(path: Path):
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def episode_times(row: dict) -> list[datetime]:
    times = []
    for key in ("first_ts", "last_ts"):
        dt = parse_ts(row.get(key))
        if dt:
            times.append(dt)
    for msg in row.get("messages") or []:
        if isinstance(msg, dict):
            dt = parse_ts(msg.get("ts"))
            if dt:
                times.append(dt)
    return times


def on_day(times: list[datetime], day, zone: ZoneInfo) -> bool:
    return any(dt.astimezone(zone).date() == day for dt in times)


def _texts(messages: list, role: str, conclusion: bool | None = None) -> list[str]:
    out = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != role:
            continue
        if conclusion is True and not msg.get("conclusion"):
            continue
        text = str(msg.get("text") or "").strip()
        if text:
            out.append(text)
    return out


def compact_episode(row: dict, zone: ZoneInfo) -> dict:
    messages = [m for m in (row.get("messages") or []) if isinstance(m, dict)]
    head = messages[0] if messages else {}
    times = episode_times(row)
    local = [dt.astimezone(zone) for dt in times]
    users = _texts(messages, "user")
    conclusions = _texts(messages, "assistant", conclusion=True)
    if not conclusions:
        conclusions = _texts(messages, "assistant")[-1:]
    return {
        "conversation_id": row.get("conversation_id") or "",
        "session_id": row.get("session_id") or "",
        "title": row.get("title") or "",
        "project": head.get("project") or row.get("project") or "unknown",
        "agent": head.get("agent") or row.get("agent") or "",
        "start": local[0].isoformat(timespec="minutes") if local else "",
        "end": local[-1].isoformat(timespec="minutes") if local else "",
        "user_requests": [clip(t, USER_CHARS) for t in users[:4]],
        "conclusions": [clip(t, CONCLUSION_CHARS) for t in conclusions[-2:]],
    }


def load_episodes(day, zone: ZoneInfo) -> tuple[list[dict], dict]:
    """Live session files replace batch files with the same session id."""
    chosen: dict[str, tuple[int, dict]] = {}
    sources = ((LIVE_DIR, 1), (batch_dir(), 0))
    files_seen = 0
    for directory, rank in sources:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.conversations.jsonl")):
            files_seen += 1
            for row in iter_jsonl(path):
                times = episode_times(row)
                if not on_day(times, day, zone):
                    continue
                key = session_key(path, row) + "\n" + str(row.get("conversation_id") or "")
                prev = chosen.get(key)
                if prev and prev[0] >= rank:
                    continue
                item = compact_episode(row, zone)
                item["source"] = "live" if rank else "batch"
                chosen[key] = (rank, item)
    episodes = [item for _, item in chosen.values()]
    episodes.sort(key=lambda e: (e.get("start") or "", e.get("project") or "", e.get("title") or ""))
    stats = {
        "files_seen": files_seen,
        "episodes": len(episodes),
        "live": sum(1 for e in episodes if e["source"] == "live"),
        "batch": sum(1 for e in episodes if e["source"] == "batch"),
    }
    return episodes, stats


def _ir_items(row: dict, key: str) -> list[str]:
    items = []
    for item in row.get(key) or []:
        if isinstance(item, str) and item.strip():
            items.append(clip(item, IR_ITEM_CHARS))
        elif isinstance(item, dict) and str(item.get("text") or "").strip():
            items.append(clip(item.get("text"), IR_ITEM_CHARS))
    return items


def load_knowledge(episodes: list[dict]) -> dict[str, dict]:
    directory = ir_dir()
    if not directory.is_dir() or not episodes:
        return {}
    wanted = {e["conversation_id"] for e in episodes if e.get("conversation_id")}
    found: dict[str, dict] = {}
    for path in directory.glob("*.knowledge.jsonl"):
        if len(found) >= len(wanted):
            break
        for row in iter_jsonl(path):
            cid = str(row.get("conversation_id") or "")
            if cid not in wanted or cid in found:
                continue
            outcome = row.get("outcome") if isinstance(row.get("outcome"), dict) else {}
            found[cid] = {
                "goal": clip(row.get("goal"), 300),
                "summary": clip(row.get("summary"), 500),
                "outcome_status": str(outcome.get("status") or ""),
                "outcome_result": clip(outcome.get("result"), 400),
                "open_questions": _ir_items(row, "open_questions")[:4],
                "tasks": _ir_items(row, "tasks")[:4],
            }
    return found


def attach_knowledge(episodes: list[dict], knowledge: dict[str, dict]) -> list[dict]:
    out = []
    for episode in episodes:
        item = dict(episode)
        ir = knowledge.get(item.get("conversation_id") or "")
        if ir:
            item["knowledge"] = ir
        out.append(item)
    return out


def evidence_payload(day, zone: ZoneInfo, episodes: list[dict], *, through_now: bool = False) -> dict:
    shown = episodes[:MAX_EPISODES]
    omitted = max(0, len(episodes) - len(shown))
    return {
        "date": day.isoformat(),
        "timezone": str(zone),
        "through_now": through_now,
        "episode_count": len(episodes),
        "omitted_episodes": omitted,
        "episodes": shown,
    }


def brief_prompt(payload: dict) -> str:
    window = "from the start of today until now. Work may still be in progress." if payload.get("through_now") else "on this calendar day."
    return f"""You are writing a work brief for the person who spent this window working with AI coding agents ({window}).

Read user_requests as the user's needs. Read conclusions only to judge whether those needs were met.
The reader is the user. Every line is about what the user asked for and whether it happened.

Rules:
- Group by project. Use the conversation title as the project heading when project is unknown.
- Refer to the brief's day as "当日" (or the date).
- Use the user's vocabulary. No implementation detail: job IDs, PIDs, command lines, file paths, directory names, error names, debugging steps, counts, percentages, or model metrics, unless the user explicitly asked for that number.
- Derived the item from the users requests, derive the state (finished or not) from the agents final response.
- "done":  Array of 1-5 short noun phrases for the user's requests that are finished by agents. 
- "pending": array of 1-5 short noun phrases for the user's requests that are still unfinished. Never repeat finished work here.
- "user_action": empty unless the user personally must do something to unblock a pending item. Then write one short sentence.
- Never list a problem the agent discovered or ran into on its own; only the user's own needs. Do not invent work. If evidence is thin, say so.

Return JSON only:
{{
  "headline": "one sentence: what the user got done this window, in plain language",
  "projects": [
    {{"project": "project heading", "done": ["short item 已完成"], "pending": ["short item 尚未完成"], "user_action": "what the user must do, or empty"}}
  ],
  "notes": "empty, or one sentence if evidence was missing or truncated"
}}
At most 6 projects. Write in the configured output language.

EVIDENCE:
{json.dumps(payload, ensure_ascii=False)}
"""


def normalize_brief(obj: dict, day, stats: dict) -> dict:
    if not isinstance(obj, dict):
        raise RuntimeError("brief analysis returned non-object JSON")

    projects = []
    for item in obj.get("projects") or []:
        if not isinstance(item, dict):
            continue
        done = progress_items(item.get("done"))
        pending = progress_items(item.get("pending"))
        if not done and not pending and item.get("progress"):
            # Earlier prompt versions returned one progress field.
            if str(item.get("status") or "").strip().lower() == "done":
                done = progress_items(item.get("progress"))
            else:
                pending = progress_items(item.get("progress"))
        row = {
            "project": str(item.get("project") or "").strip(),
            "done": done,
            "pending": pending,
            "user_action": str(item.get("user_action") or "").strip(),
        }
        if any(row.values()):
            projects.append(row)
        if len(projects) >= 6:
            break
    return {
        "date": day.isoformat(),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "headline": str(obj.get("headline") or "").strip(),
        "projects": projects,
        "notes": str(obj.get("notes") or "").strip(),
        "evidence": stats,
    }


def render_markdown(brief: dict) -> str:
    projects = brief.get("projects") or []
    done = [p for p in projects if p.get("done")]
    pending = [p for p in projects if p.get("pending") or p.get("user_action")]
    lines = [f"# {brief['date']}", ""]
    if brief.get("headline"):
        lines += [brief["headline"], ""]
    lines.append("## 已完成")
    if done:
        for item in done:
            project = item.get("project") or "未标明项目"
            lines.append(f"### {project}")
            lines += [f"- {line}" for line in item.get("done") or []]
            lines.append("")
    else:
        lines += ["- 没有已完成的工作。", ""]
    lines.append("## 潜在问题")
    if pending:
        for item in pending:
            project = item.get("project") or "未标明项目"
            lines.append(f"### {project}")
            lines += [f"- {line}" for line in item.get("pending") or []]
            if item.get("user_action"):
                lines.append(f"- 需要你做：{item['user_action']}")
            lines.append("")
    else:
        lines.append("- 没有未完成的事。")
    if brief.get("notes"):
        lines += ["", f"> {brief['notes']}"]
    ev = brief.get("evidence") or {}
    lines += [
        "",
        f"证据：{ev.get('episodes', 0)} 段对话（实时 {ev.get('live', 0)}，批量 {ev.get('batch', 0)}），"
        f"其中 {ev.get('with_knowledge', 0)} 段有知识记录。",
        "",
    ]
    return "\n".join(lines)


def empty_brief(day, stats: dict) -> dict:
    return {
        "date": day.isoformat(),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "headline": "没有找到和 AI 的工作对话。",
        "projects": [],
        "notes": "没有调用分析模型。",
        "evidence": stats,
    }


def build(day, zone: ZoneInfo, *, dry_run: bool = False, timeout: float = 180.0, through_now: bool = False) -> dict:
    episodes, stats = load_episodes(day, zone)
    knowledge = load_knowledge(episodes)
    episodes = attach_knowledge(episodes, knowledge)
    stats["with_knowledge"] = len(knowledge)
    stats["timezone"] = str(zone)
    stats["through_now"] = through_now
    payload = evidence_payload(day, zone, episodes, through_now=through_now)
    if dry_run:
        return {"dry_run": True, "evidence": payload, "stats": stats}
    if not episodes:
        return empty_brief(day, stats)
    raw = analysis_harness.complete_json(brief_prompt(payload), max_tokens=16000, timeout=timeout)
    return normalize_brief(raw, day, stats)


def write_brief(brief: dict, out_dir: Path, *, today: bool = False) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = brief["date"] + (".today" if today else "")
    json_path = out_dir / f"{name}.json"
    md_path = out_dir / f"{name}.md"
    json_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(brief), encoding="utf-8")
    return json_path, md_path


def _stdout():
    """Windows consoles often use GBK; briefs are UTF-8."""
    out = sys.stdout
    if hasattr(out, "reconfigure"):
        try:
            out.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return out


def main(argv: list[str] | None = None) -> int:
    out = _stdout()
    parser = argparse.ArgumentParser(description="Brief one local day of work with AI agents.")
    parser.add_argument("--date", help="YYYY-MM-DD, local calendar day. Default: yesterday.")
    parser.add_argument("--today", action="store_true", help="Brief today up to now, instead of yesterday.")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--dry-run", action="store_true", help="Collect evidence only; do not call the analysis model.")
    parser.add_argument("--stdout", action="store_true", help="Print JSON instead of only the file paths.")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)

    zone = local_zone()
    if args.today:
        day = datetime.now(zone).date()
    elif args.date:
        day = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        day = (datetime.now(zone) - timedelta(days=1)).date()

    brief = build(day, zone, dry_run=args.dry_run, timeout=args.timeout, through_now=args.today)
    if args.dry_run:
        print(json.dumps(brief, ensure_ascii=False, indent=2), file=out)
        return 0
    json_path, md_path = write_brief(brief, args.out_dir, today=args.today)
    if args.stdout:
        print(json.dumps(brief, ensure_ascii=False, indent=2), file=out)
    else:
        print(md_path, file=out)
        print(json_path, file=out)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
