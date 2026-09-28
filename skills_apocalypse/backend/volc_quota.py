#!/usr/bin/env python3
"""Volcengine Ark Agent Plan quota adapter.

Uses the official Ark CLI as the credential/auth boundary. Apocalypse never
reads or copies Volcengine SSO, AK/SK, API keys, or profile secrets.

Official flow:
  arkcli --format json usage plan --product agent-plan

The returned Agent Plan periods are mapped onto Apocalypse's existing
{five_hour, weekly} UI contract. Monthly is intentionally ignored for now.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from typing import Any

TIMEOUT_SECONDS = 15


def _empty_window() -> dict[str, Any]:
    return {"remaining": 0.0, "reset_in_min": 0, "available": False}


def _creationflags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "ARKCLI_NO_UPDATE_NOTIFIER": "1",
        "ARKCLI_CALLER_TYPE": "ai_agent",
        "ARKCLI_CALLER_NAME": "apocalypse",
        "ARKCLI_SKILL_NAME": "arkcli-usage",
    })
    return env


def _run(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    exe = shutil.which("arkcli")
    if not exe:
        return None
    return subprocess.run(
        [exe, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_SECONDS,
        env=_env(),
        creationflags=_creationflags(),
    )


def _json_stdout(proc: subprocess.CompletedProcess[str] | None) -> Any:
    if proc is None or proc.returncode != 0:
        return None
    text = (proc.stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        # Be tolerant of a harmless leading informational line in older builds.
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
        if start >= 0:
            try:
                return json.loads(text[start:])
            except Exception:
                pass
    return None


def _auth_problem(proc: subprocess.CompletedProcess[str] | None) -> str | None:
    if proc is None:
        return None
    text = ((proc.stderr or "") + "\n" + (proc.stdout or "")).lower()
    if any(x in text for x in ("notlogin", "not login", "not logged", "请登录", "未登录")):
        return "missing"
    if any(x in text for x in ("unauthorized", "authentication", "token expired", "expired token", "登录态过期")):
        return "expired"
    return None


def _reset_minutes(value: Any) -> int:
    if not value:
        return 0
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        return max(0, int((ts - time.time()) / 60))
    except Exception:
        return 0


def _period_window(period: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(period, dict):
        return _empty_window()
    pct = period.get("percent")
    if pct is None:
        used, total = period.get("used"), period.get("total")
        try:
            pct = float(used) / float(total) * 100.0 if float(total) > 0 else None
        except Exception:
            pct = None
    try:
        used_pct = max(0.0, min(100.0, float(pct)))
    except Exception:
        return _empty_window()
    return {
        "remaining": max(0.0, 1.0 - used_pct / 100.0),
        "reset_in_min": _reset_minutes(period.get("reset_at")),
        "available": True,
        "used_percent": str(int(round(used_pct))),
        "window_key": str(period.get("label") or ""),
        "period_end": period.get("reset_at"),
    }


def _item(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    items = data.get("items")
    if not isinstance(items, list):
        return None
    # Prefer a currently subscribed personal Agent Plan.
    for row in items:
        if isinstance(row, dict) and row.get("product") == "agent-plan" and row.get("subscribed") is True:
            return row
    for row in items:
        if isinstance(row, dict) and row.get("product") == "agent-plan":
            return row
    return None


def fetch_volc_agent_quota() -> dict[str, Any] | None:
    """Return a Volc Agent row, or None so GProxy can remain the fallback."""
    proc = _run(["--format", "json", "usage", "plan", "--product", "agent-plan"])
    if proc is None:
        return None

    auth = _auth_problem(proc)
    if auth:
        return {"auth": auth}

    data = _json_stdout(proc)
    if data is None:
        return None

    row = _item(data)
    if not row:
        return None
    if row.get("error"):
        return None
    if row.get("subscribed") is False:
        return {
            "provider": "Volc Agent",
            "status": "NO SUBSCRIPTION",
            "auth": "ok",
            "five_hour": _empty_window(),
            "weekly": _empty_window(),
            "source": "arkcli",
        }

    periods = {
        str(p.get("label") or "").lower(): p
        for p in (row.get("periods") or [])
        if isinstance(p, dict)
    }
    five = _period_window(periods.get("5h"))
    weekly = _period_window(periods.get("weekly"))
    if not five.get("available") and not weekly.get("available"):
        return None

    return {
        "provider": "Volc Agent",
        "status": "OFFICIAL",
        "auth": "ok",
        "plan": row.get("tier"),
        "five_hour": five,
        "weekly": weekly,
        "source": "arkcli",
    }


def diagnose_volc_agent() -> dict[str, Any]:
    exe = shutil.which("arkcli")
    out: dict[str, Any] = {
        "provider": "Volc Agent",
        "arkcli_installed": bool(exe),
        "arkcli_path": exe,
        "auth_status": None,
        "subscribed": None,
        "tier": None,
        "five_hour_available": False,
        "weekly_available": False,
        "conclusion": None,
    }
    if not exe:
        out["conclusion"] = "ARKCLI NOT INSTALLED"
        return out

    proc = _run(["--format", "json", "usage", "plan", "--product", "agent-plan"])
    auth = _auth_problem(proc)
    if auth:
        out["auth_status"] = auth
        out["conclusion"] = "ARKCLI LOGIN REQUIRED"
        return out

    data = _json_stdout(proc)
    if data is None:
        out["auth_status"] = "unknown"
        out["exit_code"] = proc.returncode if proc else None
        out["conclusion"] = "USAGE QUERY FAILED"
        return out

    out["auth_status"] = "ok"
    row = _item(data)
    if not row:
        out["conclusion"] = "AGENT PLAN NOT FOUND"
        return out
    out["subscribed"] = bool(row.get("subscribed"))
    out["tier"] = row.get("tier")
    labels = {str(p.get("label") or "").lower() for p in (row.get("periods") or []) if isinstance(p, dict)}
    out["five_hour_available"] = "5h" in labels
    out["weekly_available"] = "weekly" in labels
    out["conclusion"] = "AGENT PLAN QUOTA AVAILABLE" if out["subscribed"] else "NO AGENT PLAN SUBSCRIPTION"
    return out


if __name__ == "__main__":
    print(json.dumps(diagnose_volc_agent(), ensure_ascii=False, indent=2))
