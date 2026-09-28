#!/usr/bin/env python3
"""Web-native first-run onboarding for Apocalypse Spatial OS.

The browser receives only sanitized discovery metadata. Secrets stay local to
this process and are written only after the user chooses a provider/model.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import agent_discovery
import analysis_harness

DATA_DIR = Path.home() / ".claude" / "apocalypse"
HARNESS_FILE = DATA_DIR / "harness.json"
QUOTA_FILE = DATA_DIR / "quota_sources.json"
SECRETS_FILE = DATA_DIR / "secrets.json"
SETUP_FILE = DATA_DIR / "setup.json"
SUPPORTED_TRANSPORTS = {
    "anthropic_messages", "openai_responses", "openai_chat",
    "claude_cli", "codex_cli", "hermes_cli",
}


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


def _restore(path: Path, raw: bytes | None) -> None:
    try:
        if raw is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".restore")
            tmp.write_bytes(raw)
            os.replace(tmp, path)
    except Exception:
        pass


def _normalize_url(value: Any) -> str:
    s = str(value or "").strip().rstrip("/")
    if s and "://" not in s:
        s = ("http://" if s.startswith(("localhost", "127.")) else "https://") + s
    return s


def _candidate_id(candidate: dict[str, Any]) -> str:
    raw = "|".join(str(candidate.get(k) or "") for k in (
        "source_agent", "provider", "transport", "base_url", "executable", "provider_override"
    ))
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _rank(candidate: dict[str, Any]) -> tuple[int, int, int]:
    availability = {"verified": 3, "configured": 2, "unavailable": 0}.get(candidate.get("availability"), 1)
    direct = 2 if candidate.get("transport") in ("anthropic_messages", "openai_responses", "openai_chat") else 1
    return availability, direct, len(candidate.get("models") or [])


def _public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _candidate_id(candidate),
        "source_agent": candidate.get("source_agent"),
        "provider": candidate.get("provider"),
        "plan": candidate.get("plan"),
        "transport": candidate.get("transport"),
        "models": list(candidate.get("models") or []),
        "base_url": candidate.get("base_url") or "",
        "provider_override": candidate.get("provider_override") or "",
        "availability": candidate.get("availability") or "configured",
        "detail": candidate.get("detail") or "",
    }


def _detect_gproxy(candidates: list[dict[str, Any]]) -> str:
    current = _read_json(QUOTA_FILE)
    if isinstance(current, dict):
        gp = current.get("gproxy")
        if isinstance(gp, dict) and gp.get("base_url"):
            return _normalize_url(gp.get("base_url"))
    for c in candidates:
        base = str(c.get("base_url") or "")
        text = (str(c.get("provider") or "") + " " + base).lower()
        if "gproxy" in text or "ao-xing-zhang.com" in text:
            return _normalize_url(base)
    return ""


def status() -> dict[str, Any]:
    setup = _read_json(SETUP_FILE)
    initialized = bool(isinstance(setup, dict) and setup.get("initialized"))
    safe = {}
    if initialized:
        for key in ("quota_plans", "analysis_provider", "analysis_model", "analysis_transport", "analysis_verified"):
            safe[key] = setup.get(key)
    return {"initialized": initialized, "setup": safe}


def discover() -> dict[str, Any]:
    data = agent_discovery.discover_internal(probe=True)
    candidates = [c for c in data.get("candidates", [])
                  if c.get("availability") in ("verified", "configured")
                  and c.get("transport") in SUPPORTED_TRANSPORTS]
    candidates.sort(key=_rank, reverse=True)

    plans = []
    for name in agent_discovery.PLAN_NAMES:
        p = (data.get("plans") or {}).get(name)
        if p and p.get("availability") in ("verified", "configured"):
            plans.append({
                "name": name,
                "availability": p.get("availability"),
                "sources": p.get("sources") or [],
            })

    setup = _read_json(SETUP_FILE)
    quota = _read_json(QUOTA_FILE)
    current = {
        "quota_plans": list(setup.get("quota_plans") or []) if isinstance(setup, dict) else [],
        "analysis_provider": setup.get("analysis_provider") if isinstance(setup, dict) else None,
        "analysis_model": setup.get("analysis_model") if isinstance(setup, dict) else None,
        "analysis_transport": setup.get("analysis_transport") if isinstance(setup, dict) else None,
        "analysis_candidate": None,
    }
    for c in candidates:
        if (c.get("provider") == current["analysis_provider"]
                and c.get("transport") == current["analysis_transport"]):
            current["analysis_candidate"] = _candidate_id(c)
            break

    gp = quota.get("gproxy") if isinstance(quota, dict) and isinstance(quota.get("gproxy"), dict) else {}
    has_password = bool(gp.get("password_secret") or gp.get("password") or os.environ.get("GPROXY_ADMIN_PASSWORD"))
    return {
        "initialized": status()["initialized"],
        "agents": [{
            "id": a.get("id"), "name": a.get("name"), "version": a.get("version"),
        } for a in data.get("agents", [])],
        "plans": plans,
        "providers": [_public_candidate(c) for c in candidates],
        "current": current,
        "gproxy": {
            "base_url": _normalize_url(gp.get("base_url") or _detect_gproxy(candidates)),
            "username": str(gp.get("username") or "admin"),
            "has_password": has_password,
        },
    }


def _safe_auth(candidate: dict[str, Any], secrets: dict[str, Any]) -> dict[str, Any] | None:
    auth = candidate.get("_auth")
    if not isinstance(auth, dict):
        return None
    if auth.get("kind") == "env" and auth.get("name"):
        return {"kind": "env", "name": str(auth["name"])}
    if auth.get("kind") == "literal" and auth.get("value"):
        secrets["analysis_api_key"] = str(auth["value"])
        return {"kind": "file", "path": str(SECRETS_FILE), "json_path": "analysis_api_key"}
    return None


def _analysis_config(candidate: dict[str, Any], model: str, secrets: dict[str, Any]) -> dict[str, Any]:
    cfg = {
        "provider": candidate.get("provider"),
        "plan": candidate.get("plan"),
        "source_agent": candidate.get("source_agent"),
        "transport": candidate.get("transport"),
        "model": model,
    }
    for key in ("base_url", "executable", "provider_override"):
        if candidate.get(key):
            cfg[key] = candidate[key]
    auth = _safe_auth(candidate, secrets)
    if auth:
        cfg["auth"] = auth
    return cfg


def _quota_config(selected: list[str], payload: dict[str, Any], secrets: dict[str, Any]) -> dict[str, Any]:
    old = _read_json(QUOTA_FILE)
    cfg = old if isinstance(old, dict) else {}
    allowed = set(agent_discovery.PLAN_NAMES)
    cfg["enabled_plans"] = [p for p in selected if p in allowed]

    gp_input = payload.get("gproxy") if isinstance(payload.get("gproxy"), dict) else {}
    gp = cfg.get("gproxy") if isinstance(cfg.get("gproxy"), dict) else {}
    cfg["gproxy"] = gp
    base = _normalize_url(gp_input.get("base_url") or gp.get("base_url"))
    if base:
        gp["base_url"] = base
        gp["username"] = str(gp_input.get("username") or gp.get("username") or "admin").strip() or "admin"
    password = str(gp_input.get("password") or "")
    if password:
        secrets["gproxy_admin_password"] = password
        gp["password_secret"] = "gproxy_admin_password"
        gp.pop("password", None)
    return cfg


def complete(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("invalid onboarding payload")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    discovery = agent_discovery.discover_internal(probe=False)
    candidates = [c for c in discovery.get("candidates", [])
                  if c.get("availability") in ("verified", "configured")
                  and c.get("transport") in SUPPORTED_TRANSPORTS]
    by_id = {_candidate_id(c): c for c in candidates}
    candidate_id = str(payload.get("analysis_candidate") or "")
    candidate = by_id.get(candidate_id)
    if not candidate:
        raise ValueError("Selected analysis provider is no longer available. Rescan and choose again.")

    model = str(payload.get("analysis_model") or "").strip()
    if not model:
        raise ValueError("Choose an Apocalypse analysis model.")
    known_models = [str(m) for m in candidate.get("models") or [] if m]
    if known_models and model not in known_models:
        raise ValueError("Selected model is not available from this provider path.")

    selected_plans = payload.get("quota_plans") or []
    if not isinstance(selected_plans, list):
        raise ValueError("quota_plans must be a list")
    available_plans = {
        name for name, p in (discovery.get("plans") or {}).items()
        if p.get("availability") in ("verified", "configured")
    }
    selected_plans = [str(p) for p in selected_plans if str(p) in available_plans]

    old_harness = HARNESS_FILE.read_bytes() if HARNESS_FILE.exists() else None
    old_secrets = SECRETS_FILE.read_bytes() if SECRETS_FILE.exists() else None
    old_quota = QUOTA_FILE.read_bytes() if QUOTA_FILE.exists() else None
    old_setup = SETUP_FILE.read_bytes() if SETUP_FILE.exists() else None

    secrets = _read_json(SECRETS_FILE)
    secrets = secrets if isinstance(secrets, dict) else {}
    harness = {
        "version": 2,
        "analysis_model": _analysis_config(candidate, model, secrets),
        "jobs": {
            "workspace_session_analysis": True,
            "discussion_decision_analysis": True,
            "compact_conversation_analysis": True,
            "schedule_analysis": True,
            "agent_worklog_analysis": True,
        },
    }
    quota = _quota_config(selected_plans, payload, secrets)

    try:
        _write_json(SECRETS_FILE, secrets, private=True)
        _write_json(HARNESS_FILE, harness, private=True)
        verification = analysis_harness.test()
        if not verification.get("ok"):
            raise RuntimeError("Analysis model verification returned an unexpected response.")
        _write_json(QUOTA_FILE, quota, private=True)
        setup = {
            "version": 2,
            "initialized": True,
            "agents": [{"id": a.get("id"), "name": a.get("name"), "version": a.get("version")}
                       for a in discovery.get("agents", [])],
            "quota_plans": selected_plans,
            "analysis_provider": candidate.get("provider"),
            "analysis_model": model,
            "analysis_transport": candidate.get("transport"),
            "analysis_verified": True,
        }
        _write_json(SETUP_FILE, setup, private=True)
        try:
            import quota_adapters
            quota_adapters.get_quotas(force=True)
        except Exception:
            pass
        return {"ok": True, **setup, "verification": verification}
    except Exception:
        _restore(HARNESS_FILE, old_harness)
        _restore(SECRETS_FILE, old_secrets)
        _restore(QUOTA_FILE, old_quota)
        _restore(SETUP_FILE, old_setup)
        raise
