#!/usr/bin/env python3
"""Discover locally installed agents and the providers/models they already use.

Discovery is intentionally read-only. It never asks an LLM a question. Native
agent availability is established from executable + local auth/config state;
HTTP providers are probed with a cheap models request when possible.

Returned discovery data never contains raw secrets. `discover_internal()` is
used only by the local setup wizard and may carry private `_auth` metadata so it
can generate Apocalypse's own harness configuration.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

HOME = Path.home()

AGENTS = {
    "claude": {"commands": ("claude",), "label": "Claude Code"},
    "codex": {"commands": ("codex",), "label": "Codex"},
    "pi": {"commands": ("pi",), "label": "Pi"},
    "hermes": {"commands": ("hermes", "hermes-cli"), "label": "Hermes"},
    "openclaw": {"commands": ("openclaw",), "label": "OpenClaw"},
}

PLAN_NAMES = ("Claude", "OpenAI", "Grok", "Volc Agent", "MiniMax Intl")


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _toml(path: Path) -> Any:
    try:
        import tomllib
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _version(exe: str) -> str:
    for args in (("--version",), ("version",)):
        try:
            p = subprocess.run(
                [exe, *args], capture_output=True, text=True, timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            text = (p.stdout or p.stderr or "").strip().splitlines()
            if text:
                return text[0][:160]
        except Exception:
            pass
    return "installed"


def installed_agents() -> list[dict[str, Any]]:
    rows = []
    for aid, meta in AGENTS.items():
        exe = next((shutil.which(c) for c in meta["commands"] if shutil.which(c)), None)
        if exe:
            rows.append({"id": aid, "name": meta["label"], "executable": exe, "version": _version(exe)})
    return rows


def infer_plan(*parts: Any) -> str | None:
    text = " ".join(str(x or "") for x in parts).lower()
    if any(x in text for x in ("anthropic", "claude")):
        return "Claude"
    if any(x in text for x in ("grok", "x.ai", " xai", "xai/")):
        return "Grok"
    if "minimax" in text:
        return "MiniMax Intl"
    if any(x in text for x in ("volc", "volcengine", "doubao", "ark.cn", "seed-", "火山", "豆包")):
        return "Volc Agent"
    if any(x in text for x in ("openai", "chatgpt", "codex", "gpt-", "o1", "o3", "o4")):
        return "OpenAI"
    return None


def _api_transport(api: str | None, plan: str | None = None) -> str:
    s = (api or "").lower().replace("-", "_")
    if "anthropic" in s or "claude_messages" in s or s == "messages":
        return "anthropic_messages"
    if "response" in s:
        return "openai_responses"
    if "chat" in s or "openai" in s:
        return "openai_chat"
    return "anthropic_messages" if plan == "Claude" else "openai_responses"


def _clean_base(url: str | None) -> str:
    return str(url or "").strip().rstrip("/")


def _models_url(base: str) -> str:
    base = _clean_base(base)
    if not base:
        return ""
    parsed = urllib.parse.urlsplit(base)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1") or path.endswith("/v1beta"):
        return base + "/models"
    if not path:
        return base + "/v1/models"
    # Custom gateway roots commonly expose /models beside their API prefix.
    return base + "/models"


def _auth_value(auth: dict[str, Any] | None) -> str | None:
    if not auth:
        return None
    if auth.get("kind") == "env":
        return os.environ.get(str(auth.get("name") or ""))
    if auth.get("kind") == "literal":
        return str(auth.get("value") or "") or None
    return None


def _probe_http(candidate: dict[str, Any]) -> tuple[str, list[str], str]:
    """Return (availability, discovered_models, detail)."""
    base = candidate.get("base_url") or ""
    url = _models_url(base)
    if not url:
        return "configured", [], "no base URL to probe"
    token = _auth_value(candidate.get("_auth"))
    headers = {"accept": "application/json", "user-agent": "Apocalypse-Setup/1"}
    if token:
        headers["authorization"] = "Bearer " + token
        headers["x-api-key"] = token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            raw = r.read(1_500_000)
            status = r.status
        data = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
        models = []
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) and isinstance(data, dict):
            items = data.get("models")
        if isinstance(items, list):
            for x in items:
                mid = x.get("id") or x.get("name") if isinstance(x, dict) else x
                if mid and str(mid) not in models:
                    models.append(str(mid))
        return "verified", models[:100], f"HTTP {status}"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "unavailable", [], f"HTTP {e.code} auth rejected"
        if e.code in (404, 405):
            return "configured", [], f"models probe unsupported (HTTP {e.code})"
        return "configured", [], f"models probe HTTP {e.code}"
    except Exception as e:
        return "configured", [], f"models probe unavailable: {type(e).__name__}"


def _candidate(*, source_agent: str, provider: str, plan: str | None, transport: str,
               models: list[str] | None = None, base_url: str = "", auth: dict[str, Any] | None = None,
               executable: str = "", configured: bool = True, detail: str = "") -> dict[str, Any]:
    return {
        "source_agent": source_agent,
        "provider": provider,
        "plan": plan,
        "transport": transport,
        "models": list(dict.fromkeys(str(m) for m in (models or []) if m)),
        "base_url": _clean_base(base_url),
        "executable": executable,
        "availability": "configured" if configured else "unavailable",
        "detail": detail,
        "_auth": auth,
    }


def _claude_candidates(agent: dict[str, Any] | None) -> list[dict[str, Any]]:
    settings = _json(HOME / ".claude" / "settings.json") or {}
    local = _json(HOME / ".claude" / "settings.local.json") or {}
    env = {}
    for cfg in (settings, local):
        if isinstance(cfg, dict) and isinstance(cfg.get("env"), dict):
            env.update(cfg["env"])
    models = []
    for key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        if env.get(key) and env[key] not in models:
            models.append(str(env[key]))
    # Stable Claude Code aliases let the user's installed client resolve the exact model.
    for alias in ("sonnet", "opus", "haiku"):
        if alias not in models:
            models.append(alias)
    auth = None
    if env.get("ANTHROPIC_AUTH_TOKEN"):
        auth = {"kind": "literal", "value": env["ANTHROPIC_AUTH_TOKEN"], "source": "~/.claude/settings*.json"}
    elif env.get("ANTHROPIC_API_KEY"):
        auth = {"kind": "literal", "value": env["ANTHROPIC_API_KEY"], "source": "~/.claude/settings*.json"}
    elif os.environ.get("ANTHROPIC_API_KEY"):
        auth = {"kind": "env", "name": "ANTHROPIC_API_KEY"}
    rows = []
    if agent:
        auth_present = (HOME / ".claude" / ".credentials.json").exists() or bool(auth) or bool(settings)
        rows.append(_candidate(
            source_agent="claude", provider="Anthropic / Claude Code", plan="Claude",
            transport="claude_cli", models=models, executable=agent["executable"],
            configured=auth_present, detail="native Claude Code login/config" if auth_present else "CLI found; login not detected",
        ))
    if env.get("ANTHROPIC_BASE_URL"):
        rows.append(_candidate(
            source_agent="claude", provider="Claude configured API", plan=infer_plan(env.get("ANTHROPIC_BASE_URL"), *models),
            transport="anthropic_messages", models=models, base_url=env.get("ANTHROPIC_BASE_URL", ""), auth=auth,
            configured=bool(auth), detail="from Claude Code environment",
        ))
    return rows


def _codex_candidates(agent: dict[str, Any] | None) -> list[dict[str, Any]]:
    cfg = _toml(HOME / ".codex" / "config.toml") or {}
    auth_file = HOME / ".codex" / "auth.json"
    model = str(cfg.get("model") or "") if isinstance(cfg, dict) else ""
    rows = []
    if agent:
        configured = auth_file.exists() or bool(cfg) or bool(os.environ.get("OPENAI_API_KEY"))
        rows.append(_candidate(
            source_agent="codex", provider="OpenAI / Codex", plan="OpenAI", transport="codex_cli",
            models=[model] if model else ["(Codex default)"], executable=agent["executable"], configured=configured,
            detail="native Codex login/config" if configured else "CLI found; login not detected",
        ))
    providers = cfg.get("model_providers") if isinstance(cfg, dict) else None
    if isinstance(providers, dict):
        for name, p in providers.items():
            if not isinstance(p, dict):
                continue
            base = p.get("base_url") or p.get("baseUrl") or ""
            env_key = p.get("env_key") or p.get("envKey")
            auth = {"kind": "env", "name": str(env_key)} if env_key else None
            plan = infer_plan(name, model, base)
            rows.append(_candidate(
                source_agent="codex", provider=str(name), plan=plan,
                transport=_api_transport(str(p.get("wire_api") or p.get("api") or ""), plan),
                models=[model] if model else [], base_url=str(base), auth=auth,
                configured=not env_key or bool(os.environ.get(str(env_key))), detail="from ~/.codex/config.toml",
            ))
    return rows


_GENERIC_PATHS = {
    "openclaw": (
        HOME / ".openclaw" / "openclaw.json", HOME / ".openclaw" / "config.json", HOME / ".openclaw" / "settings.json",
    ),
    "hermes": (
        HOME / ".hermes" / "config.json", HOME / ".hermes" / "settings.json", HOME / ".config" / "hermes" / "config.json",
    ),
    "pi": (
        HOME / ".pi" / "agent" / "settings.json", HOME / ".pi" / "settings.json", HOME / ".config" / "pi" / "settings.json",
    ),
}


def _secret_from_block(block: dict[str, Any]) -> dict[str, Any] | None:
    for k in ("apiKey", "api_key", "token", "authToken", "auth_token"):
        v = block.get(k)
        if isinstance(v, str) and v.strip():
            # ${ENV} and $ENV remain references rather than copied secrets.
            m = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", v.strip())
            if m:
                return {"kind": "env", "name": m.group(1)}
            return {"kind": "literal", "value": v.strip(), "source": "agent config"}
    for k in ("env", "envKey", "env_key", "apiKeyEnv", "api_key_env"):
        v = block.get(k)
        if isinstance(v, str) and v.strip():
            return {"kind": "env", "name": v.strip()}
    return None


def _model_ids(value: Any) -> list[str]:
    out = []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return out
    for x in value:
        if isinstance(x, str):
            mid = x
        elif isinstance(x, dict):
            mid = x.get("id") or x.get("model") or x.get("name")
        else:
            mid = None
        if mid and str(mid) not in out:
            out.append(str(mid))
    return out


def _walk_provider_blocks(node: Any, path: str = "root"):
    if isinstance(node, dict):
        base = node.get("baseUrl") or node.get("base_url") or node.get("apiBase") or node.get("api_base")
        models = _model_ids(node.get("models") or node.get("model_ids") or [])
        model_single = node.get("model")
        if model_single and not isinstance(model_single, (dict, list)) and str(model_single) not in models:
            models.append(str(model_single))
        api = node.get("api") or node.get("protocol") or node.get("wire_api") or node.get("type")
        if base or (models and any(k in node for k in ("apiKey", "api_key", "token", "env_key", "envKey"))):
            yield path, node, str(base or ""), models, str(api or "")
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                yield from _walk_provider_blocks(v, path + "." + str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, (dict, list)):
                yield from _walk_provider_blocks(v, path + f"[{i}]")


def _generic_candidates(agent_id: str, agent: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = []
    seen = set()
    for path in _GENERIC_PATHS.get(agent_id, ()):
        data = _json(path)
        if data is None:
            continue
        for jpath, block, base, models, api in _walk_provider_blocks(data):
            provider_name = jpath.split(".")[-1]
            plan = infer_plan(provider_name, base, api, *models)
            auth = _secret_from_block(block)
            key = (base, tuple(models), api, plan)
            if key in seen:
                continue
            seen.add(key)
            rows.append(_candidate(
                source_agent=agent_id, provider=provider_name, plan=plan,
                transport=_api_transport(api, plan), models=models, base_url=base, auth=auth,
                configured=bool(base and (auth or not any(k in block for k in ("apiKey", "api_key", "token")))),
                detail=f"from {path.name}:{jpath}",
            ))
    return rows


def _dedupe(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    seen = set()
    for c in candidates:
        key = (c.get("source_agent"), c.get("transport"), c.get("base_url"), tuple(c.get("models") or []), c.get("provider"))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def discover_internal(probe: bool = True) -> dict[str, Any]:
    agents = installed_agents()
    amap = {a["id"]: a for a in agents}
    candidates = []
    candidates.extend(_claude_candidates(amap.get("claude")))
    candidates.extend(_codex_candidates(amap.get("codex")))
    for aid in ("pi", "hermes", "openclaw"):
        if amap.get(aid) or any(p.exists() for p in _GENERIC_PATHS.get(aid, ())):
            candidates.extend(_generic_candidates(aid, amap.get(aid)))
    candidates = _dedupe(candidates)

    if probe:
        for c in candidates:
            if c["transport"] in ("claude_cli", "codex_cli"):
                # `--version` already succeeded. Avoid consuming plan usage just to probe.
                if c["availability"] != "unavailable":
                    c["availability"] = "verified"
                continue
            if c.get("base_url") and c["availability"] != "unavailable":
                status, models, detail = _probe_http(c)
                c["availability"] = status
                c["detail"] = (c.get("detail") + "; " + detail).strip("; ")
                if models:
                    configured = c.get("models") or []
                    c["models"] = list(dict.fromkeys(configured + models))

    plans = {}
    for name in PLAN_NAMES:
        rows = [c for c in candidates if c.get("plan") == name]
        if not rows:
            continue
        rank = {"verified": 3, "configured": 2, "unavailable": 0}
        best = max(rows, key=lambda x: rank.get(x.get("availability"), 1))
        plans[name] = {
            "availability": best.get("availability"),
            "sources": sorted(set(c.get("source_agent") or "" for c in rows)),
            "models": list(dict.fromkeys(m for c in rows for m in (c.get("models") or [])))[:100],
        }
    return {"agents": agents, "candidates": candidates, "plans": plans}


def _public_candidate(c: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in c.items() if not k.startswith("_")}


def discover(probe: bool = True) -> dict[str, Any]:
    d = discover_internal(probe=probe)
    return {"agents": d["agents"], "providers": [_public_candidate(c) for c in d["candidates"]], "plans": d["plans"]}


if __name__ == "__main__":
    print(json.dumps(discover(probe=True), ensure_ascii=False, indent=2))
