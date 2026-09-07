#!/usr/bin/env python3
"""Personal plan quota adapters for Apocalypse.

The OPS UI expects exactly two windows per provider: 5-HOUR and WEEKLY.
This module keeps that UI contract stable while sourcing real upstream quota
cycles from the user's own GProxy admin API.

Configuration (not committed):
  ~/.claude/apocalypse/quota_sources.json

Environment variables override the file:
  GPROXY_BASE_URL
  GPROXY_ADMIN_USER
  GPROXY_ADMIN_PASSWORD

No credential is ever returned by /api/quotas. A small in-memory cache avoids
logging into GProxy on every Spatial OS refresh.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DATA_DIR = Path.home() / ".claude" / "apocalypse"
CONFIG_FILE = DATA_DIR / "quota_sources.json"
LEGACY_FILE = DATA_DIR / "quotas.json"
CACHE_TTL = 30.0

# Labels are intentionally the exact names Apocalypse should show today.
# Aliases are matched against GProxy provider metadata, case-insensitively.
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


def _empty_window() -> dict[str, Any]:
    return {"remaining": 0.0, "reset_in_min": 0, "available": False}


def _empty_provider(name: str, status: str = "NO DATA") -> dict[str, Any]:
    return {
        "provider": name,
        "status": status,
        "five_hour": _empty_window(),
        "weekly": _empty_window(),
    }


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

    # Environment overrides are convenient for the packaged EXE and avoid
    # storing the admin password in the repo.
    if os.environ.get("GPROXY_BASE_URL"):
        gp["base_url"] = os.environ["GPROXY_BASE_URL"]
    if os.environ.get("GPROXY_ADMIN_USER"):
        gp["username"] = os.environ["GPROXY_ADMIN_USER"]
    if os.environ.get("GPROXY_ADMIN_PASSWORD"):
        gp["password"] = os.environ["GPROXY_ADMIN_PASSWORD"]
    return cfg


def _http_json(opener: urllib.request.OpenerDirector, url: str, *, method: str = "GET", body: Any = None, timeout: float = 8.0) -> Any:
    data = None
    headers = {"accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with opener.open(req, timeout=timeout) as r:
        raw = r.read()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8", errors="replace"))


def _gproxy_client(cfg: dict[str, Any]) -> tuple[urllib.request.OpenerDirector, str]:
    gp = cfg.get("gproxy") or {}
    base = str(gp.get("base_url") or "").strip().rstrip("/")
    user = str(gp.get("username") or "admin").strip()
    password = str(gp.get("password") or "")
    if not base:
        raise RuntimeError("GProxy base_url is not configured")
    if not password:
        raise RuntimeError("GProxy admin password is not configured")

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    _http_json(
        opener,
        base + "/admin/login",
        method="POST",
        body={"username": user, "password": password},
    )
    return opener, base


def _provider_text(provider: Any) -> str:
    if not isinstance(provider, dict):
        return str(provider).lower()
    # Provider payloads can evolve; matching the serialized metadata keeps this
    # personal adapter resilient to fields such as name/channel/family/label.
    try:
        return json.dumps(provider, ensure_ascii=False, sort_keys=True).lower()
    except Exception:
        return str(provider).lower()


def _provider_id(provider: dict[str, Any]) -> Any:
    for k in ("id", "provider_id", "providerId"):
        if provider.get(k) is not None:
            return provider[k]
    return None


def _custom_aliases(cfg: dict[str, Any], name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    gp = cfg.get("gproxy") or {}
    custom = gp.get("provider_aliases") if isinstance(gp, dict) else None
    if not isinstance(custom, dict):
        return defaults
    vals = custom.get(name)
    if isinstance(vals, str):
        vals = [vals]
    if not isinstance(vals, list):
        return defaults
    clean = tuple(str(v).strip().lower() for v in vals if str(v).strip())
    return clean or defaults


def _match_provider(providers: list[Any], aliases: tuple[str, ...]) -> dict[str, Any] | None:
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for idx, p in enumerate(providers):
        if not isinstance(p, dict):
            continue
        text = _provider_text(p)
        score = 0
        for alias in aliases:
            a = alias.lower()
            if a and a in text:
                # Exact-ish tokens beat incidental substring matches.
                score += 20 + len(a)
                if re.search(r"(^|[^a-z0-9])" + re.escape(a) + r"([^a-z0-9]|$)", text):
                    score += 20
        if score:
            scored.append((score, -idx, p))
    return max(scored, default=(0, 0, None), key=lambda x: (x[0], x[1]))[2]


def _cycles(opener: urllib.request.OpenerDirector, base: str, provider_id: Any) -> list[dict[str, Any]]:
    qs = urllib.parse.urlencode({"provider_id": provider_id, "limit": 200})
    data = _http_json(opener, base + "/admin/credential-quota-cycles?" + qs)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("items", "cycles", "data"):
            val = data.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    return []


def _epoch(v: Any) -> float | None:
    if v is None:
        return None
    try:
        n = float(v)
        # Tolerate millisecond timestamps, though GProxy currently uses seconds.
        if n > 10_000_000_000:
            n /= 1000.0
        return n
    except Exception:
        return None


def _duration_hours(c: dict[str, Any]) -> float | None:
    a, b = _epoch(c.get("period_start")), _epoch(c.get("period_end"))
    if a is None or b is None or b <= a:
        return None
    return (b - a) / 3600.0


def _cycle_text(c: dict[str, Any]) -> str:
    return " ".join(str(c.get(k) or "") for k in ("window_key", "label", "name")).lower()


def _window_kind(c: dict[str, Any]) -> str | None:
    text = _cycle_text(c)
    compact = re.sub(r"[^a-z0-9]+", "", text)
    if any(x in text for x in ("weekly", "week", "7 day", "7-day", "7d")) or "weekly" in compact:
        return "weekly"
    if any(x in text for x in ("5-hour", "5 hour", "5h", "five hour")) or "5hour" in compact:
        return "five_hour"
    hours = _duration_hours(c)
    if hours is not None:
        if 3.5 <= hours <= 7.0:
            return "five_hour"
        if 120 <= hours <= 200:
            return "weekly"
    return None


def _is_current(c: dict[str, Any], now: float) -> bool:
    status = str(c.get("status") or "").lower()
    if status == "open":
        return True
    start, end = _epoch(c.get("period_start")), _epoch(c.get("period_end"))
    return bool(start is not None and end is not None and start <= now <= end)


def _cycle_score(c: dict[str, Any], now: float) -> tuple[int, float]:
    current = 1 if _is_current(c, now) else 0
    observed = max(
        _epoch(c.get("last_observed_at")) or 0,
        _epoch(c.get("period_end")) or 0,
        _epoch(c.get("created_at")) or 0,
    )
    return current, observed


def _pick_cycles(cycles: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    now = time.time()
    grouped: dict[str, list[dict[str, Any]]] = {"five_hour": [], "weekly": []}
    unknown: list[dict[str, Any]] = []
    for c in cycles:
        kind = _window_kind(c)
        if kind:
            grouped[kind].append(c)
        else:
            unknown.append(c)

    for rows in grouped.values():
        rows.sort(key=lambda c: _cycle_score(c, now), reverse=True)

    # If GProxy has an unfamiliar label, infer from duration rather than losing
    # the data. The shortest current quota window maps to 5H; the longest to week.
    if unknown and (not grouped["five_hour"] or not grouped["weekly"]):
        current = [c for c in unknown if _is_current(c, now)] or unknown
        with_duration = [(c, _duration_hours(c)) for c in current]
        with_duration = [(c, h) for c, h in with_duration if h is not None]
        with_duration.sort(key=lambda x: x[1])
        if with_duration and not grouped["five_hour"]:
            grouped["five_hour"].append(with_duration[0][0])
        if len(with_duration) >= 2 and not grouped["weekly"]:
            grouped["weekly"].append(with_duration[-1][0])

    return (
        grouped["five_hour"][0] if grouped["five_hour"] else None,
        grouped["weekly"][0] if grouped["weekly"] else None,
    )


def _window(c: dict[str, Any] | None) -> dict[str, Any]:
    if not c:
        return _empty_window()
    try:
        used = float(c.get("used_percent"))
        remaining = min(1.0, max(0.0, 1.0 - used / 100.0))
    except Exception:
        remaining = 0.0
    end = _epoch(c.get("period_end"))
    reset = max(0, int(round((end - time.time()) / 60))) if end else 0
    return {
        "remaining": remaining,
        "reset_in_min": reset,
        "available": c.get("used_percent") is not None,
        "window_key": c.get("window_key") or "",
        "used_percent": c.get("used_percent"),
        "period_end": end,
    }


def _from_gproxy(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    opener, base = _gproxy_client(cfg)
    providers_raw = _http_json(opener, base + "/admin/providers")
    if isinstance(providers_raw, list):
        providers = providers_raw
    elif isinstance(providers_raw, dict):
        providers = next((providers_raw[k] for k in ("items", "providers", "data") if isinstance(providers_raw.get(k), list)), [])
    else:
        providers = []

    rows = []
    for name, defaults in TARGETS:
        aliases = _custom_aliases(cfg, name, defaults)
        p = _match_provider(providers, aliases)
        if not p:
            rows.append(_empty_provider(name, "NO PROVIDER"))
            continue
        pid = _provider_id(p)
        if pid is None:
            rows.append(_empty_provider(name, "NO PROVIDER ID"))
            continue
        try:
            cycles = _cycles(opener, base, pid)
            five, week = _pick_cycles(cycles)
            row = {
                "provider": name,
                "status": "GPROXY" if (five or week) else "NO DATA",
                "five_hour": _window(five),
                "weekly": _window(week),
                "source": "gproxy",
                "provider_id": pid,
            }
            rows.append(row)
        except Exception:
            rows.append(_empty_provider(name, "NO DATA"))
    return rows


def _manual_rows() -> dict[str, dict[str, Any]]:
    raw = _load_json(LEGACY_FILE)
    if isinstance(raw, dict):
        raw = raw.get("providers")
    if not isinstance(raw, list):
        return {}
    out = {}
    for r in raw:
        if not isinstance(r, dict):
            continue
        name = str(r.get("provider") or "").strip().lower()
        if name:
            out[name] = r
    return out


def _merge_manual(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use local quotas.json only to fill windows GProxy could not provide."""
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
                row[key] = {
                    "remaining": float(src.get("remaining") or 0),
                    "reset_in_min": int(src.get("reset_in_min") or 0),
                    "available": True,
                }
                filled = True
        if filled and not any(row.get(k, {}).get("available") is False for k in ("five_hour", "weekly")):
            row["status"] = "LOCAL"
        elif filled:
            row["status"] = "PARTIAL"
    return rows


def _fetch_uncached() -> list[dict[str, Any]]:
    cfg = _config()
    try:
        rows = _from_gproxy(cfg)
    except Exception:
        rows = [_empty_provider(name, "GPROXY OFFLINE") for name, _ in TARGETS]
    return _merge_manual(rows)


def get_quotas(force: bool = False) -> list[dict[str, Any]]:
    global _cache_at, _cache_value
    now = time.monotonic()
    with _cache_lock:
        if not force and _cache_value is not None and now - _cache_at < CACHE_TTL:
            return json.loads(json.dumps(_cache_value))
        value = _fetch_uncached()
        _cache_value = value
        _cache_at = now
        return json.loads(json.dumps(value))
