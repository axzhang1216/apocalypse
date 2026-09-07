#!/usr/bin/env python3
"""Interactive first-run setup for Apocalypse.

Flow:
1. Discover installed agents and provider/model configurations.
2. Check provider availability without spending an LLM turn when possible.
3. Ask which detected Plans should appear in LLM QUOTA.
4. Ask which detected provider/model Apocalypse should use for its own analysis.
5. Generate harness.json + quota_sources.json owned by Apocalypse.

The wizard never edits Claude/Codex/Pi/Hermes/OpenClaw configuration.
"""
from __future__ import annotations

import getpass
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from agent_discovery import PLAN_NAMES, discover_internal

DATA_DIR = Path.home() / ".claude" / "apocalypse"
HARNESS_FILE = DATA_DIR / "harness.json"
QUOTA_FILE = DATA_DIR / "quota_sources.json"
SECRETS_FILE = DATA_DIR / "secrets.json"
SETUP_FILE = DATA_DIR / "setup.json"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, value: Any, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    if private:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(prompt + suffix + ": ").strip()
    except EOFError:
        return default
    return value or default


def _yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    v = _ask(f"{prompt} ({d})", "").lower()
    if not v:
        return default
    return v in ("y", "yes", "1", "true", "是", "好")


def _multi_select(prompt: str, items: list[str], default_all: bool = True) -> list[int]:
    print("\n" + prompt)
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item}")
    default = "all" if default_all else ""
    raw = _ask("输入编号，可逗号分隔；all=全部；none=不显示", default).lower()
    if raw in ("all", "a", "*"):
        return list(range(len(items)))
    if raw in ("none", "n", "0", ""):
        return []
    chosen = []
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            idx = int(part) - 1
        except ValueError:
            continue
        if 0 <= idx < len(items) and idx not in chosen:
            chosen.append(idx)
    return chosen


def _one_select(prompt: str, items: list[str], default_idx: int = 0) -> int:
    print("\n" + prompt)
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item}")
    while True:
        raw = _ask("输入编号", str(default_idx + 1))
        try:
            idx = int(raw) - 1
        except ValueError:
            idx = -1
        if 0 <= idx < len(items):
            return idx
        print("  无效编号，请重试。")


def _safe_auth(candidate: dict[str, Any]) -> dict[str, Any] | None:
    auth = candidate.get("_auth")
    if not isinstance(auth, dict):
        return None
    if auth.get("kind") == "env":
        return {"kind": "env", "name": auth.get("name")}
    if auth.get("kind") == "literal" and auth.get("value"):
        secrets = _read_json(SECRETS_FILE)
        if not isinstance(secrets, dict):
            secrets = {}
        key = "analysis_api_key"
        secrets[key] = str(auth["value"])
        _write_json(SECRETS_FILE, secrets, private=True)
        return {"kind": "file", "path": str(SECRETS_FILE), "json_path": key}
    return None


def _display_candidate(c: dict[str, Any]) -> str:
    models = c.get("models") or []
    model_text = ", ".join(models[:3]) + (" …" if len(models) > 3 else "")
    src = c.get("source_agent") or "config"
    plan = f" · {c['plan']}" if c.get("plan") else ""
    return f"{c.get('provider')} [{src}{plan}] · {c.get('transport')} · {c.get('availability')}" + (f" · {model_text}" if model_text else "")


def _candidate_rank(c: dict[str, Any]) -> tuple[int, int, int]:
    avail = {"verified": 3, "configured": 2, "unavailable": 0}.get(c.get("availability"), 1)
    # Prefer direct HTTP transports for an Apocalypse-owned harness, but a
    # logged-in native CLI is an excellent zero-secret fallback.
    direct = 2 if c.get("transport") in ("anthropic_messages", "openai_responses", "openai_chat") else 1
    return avail, direct, len(c.get("models") or [])


def _analysis_config(candidate: dict[str, Any], model: str) -> dict[str, Any]:
    out = {
        "provider": candidate.get("provider"),
        "plan": candidate.get("plan"),
        "source_agent": candidate.get("source_agent"),
        "transport": candidate.get("transport"),
        "model": model,
    }
    if candidate.get("base_url"):
        out["base_url"] = candidate["base_url"]
    if candidate.get("executable"):
        out["executable"] = candidate["executable"]
    auth = _safe_auth(candidate)
    if auth:
        out["auth"] = auth
    return out


def _detect_gproxy_base(candidates: list[dict[str, Any]]) -> str:
    existing = _read_json(QUOTA_FILE)
    if isinstance(existing, dict):
        gp = existing.get("gproxy")
        if isinstance(gp, dict) and gp.get("base_url"):
            return str(gp["base_url"])
    # GProxy often appears as a custom model gateway in an agent config. This
    # is only a default suggestion; the user confirms it before admin login is stored.
    for c in candidates:
        base = str(c.get("base_url") or "")
        text = (str(c.get("provider") or "") + " " + base).lower()
        if "gproxy" in text or "ao-xing-zhang.com" in text:
            return base.rstrip("/")
    return ""


def _configure_quota(selected: list[str], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    old = _read_json(QUOTA_FILE)
    cfg = old if isinstance(old, dict) else {}
    cfg["enabled_plans"] = selected
    if not selected:
        return cfg

    gp = cfg.get("gproxy") if isinstance(cfg.get("gproxy"), dict) else {}
    cfg["gproxy"] = gp
    base_default = str(gp.get("base_url") or _detect_gproxy_base(candidates))
    print("\nPlan usage source")
    print("  Apocalypse 当前优先从你的 GProxy quota-cycle API 读取 5H / WEEKLY。")
    print("  模型 API key 与 GProxy admin 登录不是同一个凭据。")
    if base_default or _yes("配置 GProxy 作为 Plan usage source？", True):
        base = _ask("GProxy base URL", base_default)
        if base:
            gp["base_url"] = base.rstrip("/")
            gp["username"] = _ask("GProxy admin username", str(gp.get("username") or "admin"))
            # Keep an existing password reference if present. Never echo it.
            existing_secret = None
            if gp.get("password_secret"):
                existing_secret = str(gp["password_secret"])
            need_password = not existing_secret or _yes("更新已保存的 GProxy admin password？", False)
            if need_password:
                try:
                    pw = getpass.getpass("GProxy admin password: ").strip()
                except Exception:
                    pw = ""
                if pw:
                    secrets = _read_json(SECRETS_FILE)
                    if not isinstance(secrets, dict):
                        secrets = {}
                    secrets["gproxy_admin_password"] = pw
                    _write_json(SECRETS_FILE, secrets, private=True)
                    gp["password_secret"] = "gproxy_admin_password"
                    gp.pop("password", None)
    return cfg


def run(force: bool = False, non_interactive: bool = False) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SETUP_FILE.exists() and not force and non_interactive:
        return {"ok": True, "skipped": True, "reason": "already initialized"}

    print("\nAPOCALYPSE · FIRST-RUN SETUP")
    print("Scanning installed agents and their configured providers…\n")
    d = discover_internal(probe=True)
    agents = d["agents"]
    candidates = d["candidates"]

    if agents:
        print("Detected agents:")
        for a in agents:
            print(f"  ✓ {a['name']} · {a['version']} · {a['executable']}")
    else:
        print("Detected agents: none on PATH")

    print("\nDetected provider paths:")
    for c in sorted(candidates, key=_candidate_rank, reverse=True):
        mark = "✓" if c.get("availability") == "verified" else "~" if c.get("availability") == "configured" else "×"
        print(f"  {mark} {_display_candidate(c)}")

    usable_plans = []
    for name in PLAN_NAMES:
        p = d["plans"].get(name)
        if p and p.get("availability") in ("verified", "configured"):
            usable_plans.append(name)

    if non_interactive:
        selected_plans = usable_plans
    else:
        indices = _multi_select(
            "你希望显示哪些 Plan 的 usage 在 Apocalypse 仪表盘？",
            [f"{p} · {d['plans'][p].get('availability')}" for p in usable_plans],
            default_all=True,
        ) if usable_plans else []
        selected_plans = [usable_plans[i] for i in indices]

    quota_cfg = _configure_quota(selected_plans, candidates) if not non_interactive else {"enabled_plans": selected_plans}
    if not non_interactive or selected_plans:
        _write_json(QUOTA_FILE, quota_cfg, private=True)

    eligible = [c for c in candidates if c.get("availability") in ("verified", "configured") and c.get("transport") in (
        "anthropic_messages", "openai_responses", "openai_chat", "claude_cli", "codex_cli"
    )]
    eligible.sort(key=_candidate_rank, reverse=True)
    if not eligible:
        raise RuntimeError("No usable analysis provider was detected. Configure/login to an agent or model provider, then run apocalypse-ui init again.")

    if non_interactive:
        chosen = eligible[0]
    else:
        ci = _one_select(
            "你希望哪个 provider 作为 Apocalypse 的分析模型来源？",
            [_display_candidate(c) for c in eligible],
            0,
        )
        chosen = eligible[ci]

    models = [m for m in (chosen.get("models") or []) if m]
    if not models:
        model = _ask("该 provider 未列出模型，请输入 analysis model ID") if not non_interactive else ""
        if not model:
            raise RuntimeError("Analysis model ID is required")
    elif len(models) == 1:
        model = models[0]
    elif non_interactive:
        model = models[0]
    else:
        mi = _one_select(
            f"你希望 {chosen.get('provider')} 的哪个模型作为 Apocalypse 的分析模型？",
            models,
            0,
        )
        model = models[mi]

    harness = {
        "version": 1,
        "analysis_model": _analysis_config(chosen, model),
        "jobs": {
            "workspace_session_analysis": True,
            "discussion_decision_analysis": True,
            "compact_conversation_analysis": True,
            "schedule_analysis": True,
            "agent_worklog_analysis": True,
        },
    }
    _write_json(HARNESS_FILE, harness, private=True)
    setup = {
        "version": 1,
        "initialized": True,
        "agents": [{"id": a["id"], "name": a["name"], "version": a["version"]} for a in agents],
        "quota_plans": selected_plans,
        "analysis_provider": chosen.get("provider"),
        "analysis_model": model,
        "analysis_transport": chosen.get("transport"),
    }
    _write_json(SETUP_FILE, setup, private=True)

    print("\nConfigured:")
    print("  LLM QUOTA     → " + (", ".join(selected_plans) if selected_plans else "none"))
    print(f"  ANALYSIS MODEL → {chosen.get('provider')} / {model} [{chosen.get('transport')}]")
    print(f"  Harness        → {HARNESS_FILE}")
    return {"ok": True, **setup}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Configure Apocalypse from installed agents")
    p.add_argument("--force", action="store_true", help="run setup again even when already initialized")
    p.add_argument("--non-interactive", action="store_true", help="choose the highest-ranked detected defaults")
    args = p.parse_args()
    try:
        result = run(force=args.force, non_interactive=args.non_interactive)
        if args.non_interactive:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"Setup failed: {e}", file=sys.stderr)
        raise SystemExit(1)
