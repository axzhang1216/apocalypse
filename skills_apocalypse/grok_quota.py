#!/usr/bin/env python3
"""Grok Build/SuperGrok quota reader.

Reads the same Grok CLI OAuth session as Orca, keeps all secrets local, and
queries Grok's billing endpoint for the weekly plan window. Grok currently does
not expose a 5-hour window in this payload, so Apocalypse intentionally renders
that UI-only window as 100% remaining when authentication is valid.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_PROXY_BASE = "https://cli-chat-proxy.grok.com/v1"
PREFERRED_ISSUER = "https://auth.x.ai"
TOKEN_SKEW_SECONDS = 5 * 60
WEEKLY_MINUTES = 7 * 24 * 60


def _empty_window() -> dict[str, Any]:
    return {"remaining": 0.0, "reset_in_min": 0, "available": False}


def _default_five_hour() -> dict[str, Any]:
    # Grok/SuperGrok's current billing payload exposes a weekly usage period but
    # no separate 5-hour quota. The OPS UI always has a 5H row, so for a valid
    # Grok session we deliberately show that unsupported window as fully
    # available instead of displaying NO DATA.
    return {
        "remaining": 1.0,
        "reset_in_min": 0,
        "available": True,
        "used_percent": "0",
        "synthetic": True,
        "reason": "provider_does_not_report_5h",
    }


def _empty_row(status: str, *, auth: str = "ok", error: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "provider": "Grok",
        "status": status,
        "auth": auth,
        "five_hour": _default_five_hour() if auth == "ok" else _empty_window(),
        "weekly": _empty_window(),
        "source": "official",
    }
    if error:
        row["error"] = error
    return row


def _grok_home() -> Path:
    raw = os.environ.get("GROK_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".grok"


def _auth_path() -> Path:
    return _grok_home() / "auth.json"


def _proxy_base() -> str:
    return os.environ.get("GROK_CLI_CHAT_PROXY_BASE_URL", "").strip().rstrip("/") or DEFAULT_PROXY_BASE


def _billing_url() -> str:
    return _proxy_base() + "/billing?format=credits"


def _billing_host() -> str:
    try:
        return urllib.parse.urlsplit(_billing_url()).netloc or "unknown"
    except Exception:
        return "unknown"


def _load_auth() -> dict[str, Any] | None:
    try:
        data = json.loads(_auth_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _expiry(entry: dict[str, Any]) -> float | None:
    raw = entry.get("expires_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _is_fresh(entry: dict[str, Any]) -> bool:
    exp = _expiry(entry)
    return exp is None or exp - time.time() > TOKEN_SKEW_SECONDS


def _preferred_key(key: str) -> bool:
    return key == PREFERRED_ISSUER or key.startswith(PREFERRED_ISSUER + "::")


def _session(status: str, issuer: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": status,
        "token": str(entry.get("key") or ""),
        "issuer": issuer,
        "user_id": entry.get("user_id"),
        "team_id": entry.get("team_id"),
        "email": entry.get("email"),
        "expires_at": entry.get("expires_at"),
    }


def read_auth_session() -> dict[str, Any]:
    data = _load_auth()
    if not data:
        return {"status": "missing", "path": str(_auth_path())}

    preferred_seen = False
    expired_preferred: tuple[str, dict[str, Any]] | None = None
    fallback: tuple[str, dict[str, Any]] | None = None
    for issuer, raw in data.items():
        preferred = _preferred_key(str(issuer))
        preferred_seen = preferred_seen or preferred
        if not isinstance(raw, dict) or not isinstance(raw.get("key"), str) or not raw.get("key"):
            continue
        if preferred:
            if _is_fresh(raw):
                return _session("ok", str(issuer), raw)
            if expired_preferred is None:
                expired_preferred = (str(issuer), raw)
        elif fallback is None:
            fallback = (str(issuer), raw)

    if preferred_seen:
        if expired_preferred:
            return _session("expired", *expired_preferred)
        return {"status": "missing", "path": str(_auth_path())}
    if fallback:
        issuer, entry = fallback
        return _session("ok" if _is_fresh(entry) else "expired", issuer, entry)
    return {"status": "missing", "path": str(_auth_path())}


def _money(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    try:
        num = float(value.get("val"))
        return num if num == num else None
    except (TypeError, ValueError):
        return None


def _timestamps_match(a: Any, b: Any) -> bool:
    if not a or not b:
        return False
    try:
        pa = datetime.fromisoformat(str(a).replace("Z", "+00:00")).timestamp()
        pb = datetime.fromisoformat(str(b).replace("Z", "+00:00")).timestamp()
        return pa == pb
    except Exception:
        return False


def _confirmed_weekly_period(cfg: dict[str, Any]) -> bool:
    period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
    return (
        period.get("type") == "USAGE_PERIOD_TYPE_WEEKLY"
        and _timestamps_match(period.get("start"), cfg.get("billingPeriodStart"))
        and _timestamps_match(period.get("end"), cfg.get("billingPeriodEnd"))
    )


def _has_monthly_budget(cfg: dict[str, Any]) -> bool:
    limit = _money(cfg.get("monthlyLimit"))
    used = _money(cfg.get("used"))
    return limit is not None and used is not None and limit > 0


def _weekly_percent(cfg: dict[str, Any]) -> float | None:
    if "creditUsagePercent" in cfg:
        try:
            value = float(cfg.get("creditUsagePercent"))
            return value if value == value else None
        except (TypeError, ValueError):
            return None
    values = []
    for key in ("onDemandCap", "onDemandUsed", "prepaidBalance", "monthlyLimit", "used"):
        val = _money(cfg.get(key))
        if val is not None:
            values.append(val)
    if any(v == 0 for v in values) or _has_monthly_budget(cfg):
        return None
    return 0.0 if _confirmed_weekly_period(cfg) else None


def _reset_minutes(cfg: dict[str, Any]) -> int:
    period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
    end = period.get("end") or cfg.get("billingPeriodEnd")
    if not end:
        return 0
    try:
        ts = datetime.fromisoformat(str(end).replace("Z", "+00:00")).timestamp()
        return max(0, int((ts - time.time()) / 60))
    except Exception:
        return 0


def _headers(session: dict[str, Any]) -> dict[str, str]:
    headers = {
        "Authorization": "Bearer " + str(session["token"]),
        "X-XAI-Token-Auth": "xai-grok-cli",
        "Accept": "application/json",
    }
    if session.get("user_id"):
        headers["x-userid"] = str(session["user_id"])
    return headers


def _billing_request(session: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    req = urllib.request.Request(_billing_url(), headers=_headers(session))
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read()
            data = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
            return int(response.status), data if isinstance(data, dict) else None
    except urllib.error.HTTPError as exc:
        return int(exc.code), None


def diagnose_grok() -> dict[str, Any]:
    path = _auth_path()
    data = _load_auth()
    preferred = [str(k) for k in data if _preferred_key(str(k))] if isinstance(data, dict) else []
    session = read_auth_session()
    result: dict[str, Any] = {
        "provider": "Grok",
        "grok_home": str(_grok_home()),
        "grok_home_from_env": bool(os.environ.get("GROK_HOME", "").strip()),
        "auth_file_exists": path.exists(),
        "auth_entry_count": len(data) if isinstance(data, dict) else 0,
        "preferred_issuer_present": bool(preferred),
        "auth_status": session.get("status") or "missing",
        "selected_issuer": session.get("issuer"),
        "user_id_present": bool(session.get("user_id")),
        "team_id_present": bool(session.get("team_id")),
        "expires_at": session.get("expires_at"),
        "token_fresh": session.get("status") == "ok",
        "billing_host": _billing_host(),
        "billing_http": None,
        "subscription_tier": None,
        "period_type": None,
        "weekly_field_present": False,
        "weekly_percent_available": False,
        "monthly_budget_present": False,
        "five_hour_source": "default_100_percent",
    }
    if session.get("status") != "ok":
        result["conclusion"] = "LOGIN REQUIRED" if session.get("status") == "missing" else "TOKEN STALE"
        return result
    try:
        status, payload = _billing_request(session)
    except Exception as exc:
        result["billing_error"] = type(exc).__name__
        result["conclusion"] = "BILLING REQUEST FAILED"
        return result
    result["billing_http"] = status
    if status != 200 or not isinstance(payload, dict):
        result["conclusion"] = {401: "TOKEN REJECTED", 403: "BILLING FORBIDDEN", 412: "NO PERSONAL BILLING CONTEXT"}.get(status, "BILLING HTTP ERROR")
        return result
    cfg = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
    pct = _weekly_percent(cfg)
    result.update({
        "subscription_tier": str(cfg.get("subscriptionTier") or "") or None,
        "period_type": str(period.get("type") or "") or None,
        "weekly_field_present": "creditUsagePercent" in cfg,
        "weekly_percent_available": pct is not None,
        "monthly_budget_present": _has_monthly_budget(cfg),
        "reset_in_min": _reset_minutes(cfg) if pct is not None else None,
    })
    if pct is not None:
        result["weekly_used_percent"] = round(max(0.0, min(100.0, pct)), 1)
        result["conclusion"] = "WEEKLY QUOTA AVAILABLE"
    elif _has_monthly_budget(cfg):
        result["conclusion"] = "MONTHLY ONLY"
    else:
        result["conclusion"] = "SIGNED IN · WEEKLY NOT REPORTED"
    return result


def fetch_grok_quota() -> dict[str, Any]:
    session = read_auth_session()
    if session["status"] == "missing":
        return {"auth": "missing"}
    if session["status"] == "expired":
        return {"auth": "expired"}
    try:
        status, payload = _billing_request(session)
    except Exception as exc:
        return _empty_row("BILLING ERROR", error=f"Grok billing request failed: {type(exc).__name__}")
    if status == 401:
        return {"auth": "expired"}
    if status == 403:
        return _empty_row("BILLING UNAVAILABLE", error="Grok billing endpoint returned HTTP 403")
    if status == 412:
        return _empty_row("NO PERSONAL BILLING", error="Grok billing endpoint returned HTTP 412 for this account/team")
    if status != 200:
        return _empty_row("BILLING ERROR", error=f"Grok billing endpoint returned HTTP {status}")
    if not isinstance(payload, dict):
        return _empty_row("NO WEEKLY DATA", error="Grok billing response was not an object")

    cfg = payload.get("config") if isinstance(payload.get("config"), dict) else payload
    pct = _weekly_percent(cfg)
    if pct is None:
        return _empty_row("NO WEEKLY DATA", error="Grok did not report a weekly usage percentage")
    pct = max(0.0, min(100.0, pct))
    weekly = {
        "remaining": max(0.0, 1.0 - pct / 100.0),
        "reset_in_min": _reset_minutes(cfg),
        "available": True,
        "used_percent": str(int(round(pct))),
        "window_minutes": WEEKLY_MINUTES,
    }
    return {
        "provider": "Grok",
        "status": "OFFICIAL",
        "auth": "ok",
        "five_hour": _default_five_hour(),
        "weekly": weekly,
        "source": "official",
    }


if __name__ == "__main__":
    print(json.dumps(diagnose_grok(), ensure_ascii=False, indent=2))
