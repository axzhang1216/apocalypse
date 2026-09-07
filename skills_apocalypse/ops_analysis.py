#!/usr/bin/env python3
"""Cached OPS analysis using Apocalypse's own analysis harness.

LLM work is explicit/cached, never performed on each UI refresh. The results can
be refreshed by the server/API or from the command line and are stored under
~/.claude/apocalypse/ops_analysis.json.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import analysis_harness

DATA_DIR = Path.home() / ".claude" / "apocalypse"
SCHEDULE_FILE = DATA_DIR / "schedule.json"
EVENTS_FILE = DATA_DIR / "events.jsonl"
WORKSPACE_FILE = DATA_DIR / "workspace.json"
OUTPUT_FILE = DATA_DIR / "ops_analysis.json"


def _json(path: Path, default: Any):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _events(hours: int = 24, limit: int = 1200):
    if not EVENTS_FILE.exists():
        return []
    cut = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = []
    try:
        lines = EVENTS_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except Exception:
        return []
    for line in lines:
        try:
            e = json.loads(line)
            ts = datetime.fromisoformat(str(e.get("ts") or "").replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cut:
                rows.append(e)
        except Exception:
            continue
    return rows


def _work_context():
    ws = _json(WORKSPACE_FILE, {})
    projects = []
    for p in (ws.get("projects") or {}).values() if isinstance(ws, dict) else []:
        analyzed = p.get("analyzed_sessions") or {}
        recent = sorted(analyzed.values(), key=lambda x: x.get("ts") or "", reverse=True)[:8]
        projects.append({
            "name": p.get("title") or p.get("name") or "Unknown",
            "last_active": p.get("last_active") or "",
            "recent_sessions": [{"goal": s.get("user_goal"), "summary": s.get("summary"), "outcome": s.get("outcome"), "ts": s.get("ts")} for s in recent],
        })
    return projects[:30]


def analyze_schedule() -> dict[str, Any]:
    schedule = _json(SCHEDULE_FILE, {"events": [], "tasks": []})
    prompt = """You are the scheduling analyst inside Apocalypse, a personal agent-work operating system.
Review today's agenda/tasks together with recent project work. Do not invent calendar events.
Return JSON only:
{
  "summary": "one compact sentence",
  "suggested": [
    {"title":"short actionable suggestion","reason":"why it matters","project":"project name or PERSONAL","priority":"high|medium|low"}
  ]
}
Keep suggested to 0-4 items and only suggest work supported by the supplied context.

AGENDA:
""" + json.dumps(schedule, ensure_ascii=False) + "\n\nRECENT PROJECT WORK:\n" + json.dumps(_work_context(), ensure_ascii=False)
    out = analysis_harness.complete_json(prompt, max_tokens=900)
    if not isinstance(out, dict):
        raise RuntimeError("schedule analysis returned non-object JSON")
    out.setdefault("summary", "")
    out.setdefault("suggested", [])
    return out


def analyze_worklog(hours: int = 24) -> dict[str, Any]:
    events = _events(hours)
    compact = []
    for e in events:
        compact.append({k: e.get(k) for k in ("ts", "type", "session_id", "project", "agent", "tool", "text") if e.get(k) is not None})
    prompt = f"""You are the worklog analyst inside Apocalypse. Analyze the last {hours} hours of agent/coding work.
Use only the supplied event/project evidence. Return JSON only:
{{
  "summary":"one concise overall sentence",
  "projects":[{{"project":"name","work_done":"what materially happened","state":"active|waiting|done|unknown","evidence_count":0}}],
  "attention":["0-4 concrete items that may need follow-up"]
}}
Do not infer completion unless evidence supports it.

EVENTS:
{json.dumps(compact[-500:], ensure_ascii=False)}

PROJECT CONTEXT:
{json.dumps(_work_context(), ensure_ascii=False)}
"""
    out = analysis_harness.complete_json(prompt, max_tokens=1200)
    if not isinstance(out, dict):
        raise RuntimeError("worklog analysis returned non-object JSON")
    out.setdefault("summary", "")
    out.setdefault("projects", [])
    out.setdefault("attention", [])
    out["window_hours"] = hours
    out["event_count"] = len(events)
    return out


def refresh(schedule: bool = True, worklog: bool = True, hours: int = 24):
    old = _json(OUTPUT_FILE, {})
    out = old if isinstance(old, dict) else {}
    out["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cfg = analysis_harness.model_config()
    out["model"] = {k: cfg.get(k) for k in ("provider", "model", "transport")}
    if schedule:
        out["schedule"] = analyze_schedule()
    if worklog:
        out["worklog"] = analyze_worklog(hours)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, OUTPUT_FILE)
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--schedule", action="store_true")
    p.add_argument("--worklog", action="store_true")
    p.add_argument("--hours", type=int, default=24)
    args = p.parse_args()
    both = not args.schedule and not args.worklog
    try:
        print(json.dumps(refresh(schedule=args.schedule or both, worklog=args.worklog or both, hours=args.hours), ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        raise SystemExit(1)
