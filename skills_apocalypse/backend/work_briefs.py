#!/usr/bin/env python3
"""Serve cached yesterday/today work briefs and refresh them in the background."""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PIPELINE = Path(__file__).resolve().parents[1] / "pipeline"
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))
import daily_work_brief as dwb  # noqa: E402

TODAY_TTL = timedelta(hours=1)
_lock = threading.Lock()
_running: set[str] = set()


def _read(path: Path):
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _age(brief: dict | None) -> timedelta | None:
    if not brief:
        return None
    dt = dwb.parse_ts(brief.get("generated_at"))
    if not dt:
        return None
    return datetime.now(timezone.utc) - dt


def _needs_projects(brief: dict | None) -> bool:
    return not brief or not isinstance(brief.get("projects"), list)


def _kick(kind: str, day, *, through_now: bool):
    with _lock:
        if kind in _running:
            return
        _running.add(kind)

    def run():
        try:
            brief = dwb.build(day, dwb.local_zone(), through_now=through_now)
            dwb.write_brief(brief, dwb.OUT_DIR, today=through_now)
        except Exception:
            pass
        finally:
            with _lock:
                _running.discard(kind)

    threading.Thread(target=run, daemon=True, name=f"brief-{kind}").start()


def snapshot() -> dict:
    zone = dwb.local_zone()
    now = datetime.now(zone)
    today = now.date()
    yday = (now - timedelta(days=1)).date()
    yesterday = _read(dwb.OUT_DIR / f"{yday}.json")
    current = _read(dwb.OUT_DIR / f"{today}.today.json")
    if _needs_projects(yesterday):
        _kick("yesterday", yday, through_now=False)
    age = _age(current)
    if _needs_projects(current) or age is None or age >= TODAY_TTL:
        _kick("today", today, through_now=True)
    with _lock:
        running = sorted(_running)
    return {"yesterday": yesterday, "today": current, "refreshing": running}
