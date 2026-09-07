#!/usr/bin/env python3
"""Plan usage adapters for Apocalypse's existing LLM QUOTA panel.

Only providers selected by the first-run setup wizard are returned. GProxy is
currently the preferred source because it already normalizes provider quota
cycles. The UI contract stays {five_hour, weekly}; no fake percentages are
created when a source is unavailable.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DATA_DIR = Path.home() / ".claude" / "apocalypse"
CONFIG_FILE = DATA_DIR / "quota_sources.json"
SECRETS_FILE = DATA_DIR / "secrets.json"
LEGACY_FILE = DATA_DIR / "quotas.json"
CACHE_TTL = 30.0

TARGETS = [
    ("Claude", ("claude", "anthropic")),
    ("OpenAI", ("codex", "openai", "open ai", "chatgpt")),
    ("Grok", ("grok", "xai", "x.ai")),
    ("Volc Agent", ("volc agent", "volcengine", "volc", "ark", "doubao", "火山", "豆包")),
    ("MiniMax Intl", ("minimax international", "minimax intl", "minimax")),
]

_cache_lock = threading.Lock()
_cache_at = 0.0
_cache_value: list[dict[str, Any]] | None = None


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _config() -> dict[str, Any]:
    raw = _load_json(CONFIG_FILE)
    cfg = raw if isinstance(raw, dict) else {}
    gp = cfg.setdefault("gproxy", {})
    if not isinstance(gp, dict):
        gp = {}
        cfg["gproxy"] = gp
    if os.environ.get("GPROXY_BASE_URL"):
        gp["base_url"] = os.environ["GPROXY_BASE_URL"]
    if os.environ.get("GPROXY_ADMIN_USER"):
        gp["username"] = os.environ["GPROXY_ADMIN_USER"]
    if os.environ.get("GPROXY_ADMIN_PASSWORD"):
        gp["password"] = os.environ["GPROXY_ADMIN_PASSWORD"]
    return cfg


def _enabled(cfg: dict[str, Any]) -> list[str]:
    value = cfg.get("enabled_plans")
    if value is None:
        return [name for name, _ in TARGETS]  # backward compatibility
    if not isinstance(value, list):
        return []
    allowed = {name for name, _ in TARGETS}
    return [str(x) for x in value if str(x) in allowed]


def _password(gp: dict[str, Any]) -> str:
    if os.environ.get("GPROXY_ADMIN_PASSWORD"):
        return os.environ["GPROXY_ADMIN_PASSWORD"]
    if gp.get("password"):
        return str(gp["password"])
    key = str(gp.get("password_secret") or "")
    secrets = _load_json(SECRETS_FILE)
    if key and isinstance(secrets, dict) and secrets.get(key):
        return str(secrets[key])
    return ""


def _http_json(opener, url: str, *, method="GET", body=None, timeout=8.0):
    data = None
    headers = {"accept": "application/json", "user-agent": "Apocalypse-Quota/1"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with opener.open(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8", errors="replace")) if raw else None


def _gproxy_client(cfg: dict[str, Any]):
    gp = cfg.get("gproxy") or {}
    base = str(gp.get("base_url") or "").strip().rstrip("/")
    user = str(gp.get("username") or "admin").strip()
    password = _password(gp)
    if not base:
        raise RuntimeError("GProxy base_url is not configured")
    if not password:
        raise RuntimeError("GProxy admin password is not configured")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    _http_json(opener, base + "/admin/login", method="POST", body={"username": user, "password": password})
    return opener, base


def _empty_window():
    return {"remaining": 0.0, "reset_in_min": 0, "available": False}


def _empty_provider(name: str, status="NO DATA"):
    return {"provider": name, "status": status, "five_hour": _empty_window(), "weekly": _empty_window()}


def _provider_text(p: Any) -> str:
    try:
        return json.dumps(p, ensure_ascii=False, sort_keys=True).lower()
    except Exception:
        return str(p).lower()


def _provider_id(p: dict[str, Any]):
    for k in ("id", "provider_id", "providerId"):
        if p.get(k) is not None:
            return p[k]
    return None


def _aliases(cfg: dict[str, Any], name: str, defaults: tuple[str, ...]):
    custom = (cfg.get("gproxy") or {}).get("provider_aliases")
    vals = custom.get(name) if isinstance(custom, dict) else None
    if isinstance(vals, str):
        vals = [vals]
    if isinstance(vals, list):
        cleaned = tuple(str(v).strip().lower() for v in vals if str(v).strip())
        if cleaned:
            return cleaned
    return defaults


def _match_provider(providers: list[Any], aliases: tuple[str, ...]):
    scored = []
    for idx, p in enumerate(providers):
        if not isinstance(p, dict):
            continue
        text = _provider_text(p)
        score = 0
        for a in aliases:
            a = a.lower()
            if a and a in text:
                score += 20 + len(a)
                if re.search(r"(^|[^a-z0-9])" + re.escape(a) + r"([^a-z0-9]|$)", text):
                    score += 20
        if score:
            scored.append((score, -idx, p))
    return max(scored, default=(0, 0, None), key=lambda x: (x[0], x[1]))[2]


def _cycles(opener, base: str, provider_id: Any):
    qs = urllib.parse.urlencode({"provider_id": provider_id, "limit": 200})
    data = _http_json(opener, base + "/admin/credential-quota-cycles?" + qs)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("items", "cycles", "data"):
            if isinstance(data.get(k), list):
                return [x for x in data[k] if isinstance(x, dict)]
    return []


def _epoch(v):
    try:
        n = float(v)
        return n / 1000.0 if n > 10_000_000_000 else n
    except Exception:
        return None


def _duration_hours(c):
    a, b = _epoch(c.get("period_start")), _epoch(c.get("period_end"))
    return (b - a) / 3600.0 if a is not None and b is not None and b > a else None


def _kind(c):
    text = " ".join(str(c.get(k) or "") for k in ("window_key", "label", "name")).lower()
    compact = re.sub(r"[^a-z0-9]+", "", text)
    if any(x in text for x in ("weekly", "week", "7 day", "7-day", "7d")) or "weekly" in compact:
        return "weekly"
    if any(x in text for x in ("5-hour", "5 hour", "5h", "five hour")) or "5hour" in compact:
        return "five_hour"
    h = _duration_hours(c)
    if h is not None and 3.5 <= h <= 7.0:
        return "five_hour"
    if h is not None and 120 <= h <= 200:
        return "weekly"
    return None


def _current(c, now):
    if str(c.get("status") or "").lower() == "open":
        return True
    a, b = _epoch(c.get("period_start")), _epoch(c.get("period_end"))
    return a is not None and b is not None and a <= now <= b


def _pick(cycles):
    now = time.time()
    grouped = {"five_hour": [], "weekly": []}
    for c in cycles:
        k = _kind(c)
        if k:
            grouped[k].append(c)
    for rows in grouped.values():
        rows.sort(key=lambda c: (_current(c, now), _epoch(c.get("last_observed_at")) or _epoch(c.get("period_end")) or 0), reverse=True)
    return (grouped["five_hour"][0] if grouped["five_hour"] else None,
            grouped["weekly"][0] if grouped["weekly"] else None)


def _window(c):
    if not c:
        return _empty_window()
    try:
        used = float(c.get("used_percent"))
        remaining = min(1.0, max(0.0, 1.0 - used / 100.0))
        available = True
    except Exception:
        remaining, available = 0.0, False
    end = _epoch(c.get("period_end"))
    reset = max(0, int(round((end - time.time()) / 60))) if end else 0
    return {"remaining": remaining, "reset_in_min": reset, "available": available,
            "window_key": c.get("window_key") or "", "used_percent": c.get("used_percent"), "period_end": end}


def _from_gproxy(cfg):
    enabled = set(_enabled(cfg))
    if not enabled:
        return []
    opener, base = _gproxy_client(cfg)
    raw = _http_json(opener, base + "/admin/providers")
    if isinstance(raw, list):
        providers = raw
    elif isinstance(raw, dict):
        providers = next((raw[k] for k in ("items", "providers", "data") if isinstance(raw.get(k), list)), [])
    else:
        providers = []
    out = []
    for name, defaults in TARGETS:
        if name not in enabled:
            continue
        p = _match_provider(providers, _aliases(cfg, name, defaults))
        if not p:
            out.append(_empty_provider(name, "NO PROVIDER")); continue
        pid = _provider_id(p)
        if pid is None:
            out.append(_empty_provider(name, "NO PROVIDER ID")); continue
        try:
            five, week = _pick(_cycles(opener, base, pid))
            out.append({"provider": name, "status": "GPROXY" if (five or week) else "NO DATA",
                        "five_hour": _window(five), "weekly": _window(week), "source": "gproxy", "provider_id": pid})
        except Exception:
            out.append(_empty_provider(name, "NO DATA"))
    return out


def _manual_rows():
    raw = _load_json(LEGACY_FILE)
    if isinstance(raw, dict):
        raw = raw.get("providers")
    return {str(r.get("provider") or "").lower(): r for r in (raw or []) if isinstance(r, dict) and r.get("provider")} if isinstance(raw, list) else {}


def _merge_manual(rows):
    manual = _manual_rows()
    for row in rows:
        m = manual.get(str(row.get("provider") or "").lower())
        if not m:
            continue
        filled = False
        for key in ("five_hour", "weekly"):
            if row.get(key, {}).get("available"):
                continue
            src = m.get(key)
            if isinstance(src, dict) and src.get("remaining") is not None:
                row[key] = {"remaining": float(src.get("remaining") or 0), "reset_in_min": int(src.get("reset_in_min") or 0), "available": True}
                filled = True
        if filled:
            row["status"] = "LOCAL" if all(row.get(k, {}).get("available") for k in ("five_hour", "weekly")) else "PARTIAL"
    return rows


def _fetch_uncached():
    cfg = _config()
    enabled = _enabled(cfg)
    if not enabled:
        return []
    try:
        rows = _from_gproxy(cfg)
    except Exception:
        rows = [_empty_provider(name, "GPROXY OFFLINE") for name, _ in TARGETS if name in enabled]
    return _merge_manual(rows)


def get_quotas(force: bool = False):
    global _cache_at, _cache_value
    now = time.monotonic()
    with _cache_lock:
        if not force and _cache_value is not None and now - _cache_at < CACHE_TTL:
            return json.loads(json.dumps(_cache_value))
        _cache_value = _fetch_uncached()
        _cache_at = now
        return json.loads(json.dumps(_cache_value))
