#!/usr/bin/env python3
"""Grok Build/SuperGrok quota reader.

This mirrors Orca's current Grok usage strategy closely enough for Apocalypse:
- read the same local Grok CLI auth session
- prefer the first-party https://auth.x.ai issuer over stale legacy issuers
- respect GROK_HOME
- reject tokens inside Grok's 5-minute early-expiry window
- query the CLI billing endpoint with Grok CLI auth headers
- publish weekly usage only when the response actually supports a weekly value

The Grok billing endpoint is an undocumented product endpoint and may change.
No access token is returned to the Apocalypse frontend.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
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


def _empty_row(status: str, *, auth: str = "ok", error: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "provider": "Grok",
        "status": status,
        "auth": auth,
        "five_hour": _empty_window(),
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


def _billing_url() -> str:
    base = os.environ.get("GROK_CLI_CHAT_PROXY_BASE_URL", "").strip().rstrip("/")
    return (base or DEFAULT_PROXY_BASE) + "/billing?format=credits"


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


def read_auth_session() -> dict[str, Any]:
    """Read Grok CLI OAuth session without exposing it outside this module.

    A subtle but important detail copied from Orca: auth.json can contain an old
    issuer before the current xAI OAuth entry. Choosing the first object with a
    `key` can therefore keep reporting an expired login after re-authentication.
    """
    data = _load_auth()
    if not data:
        return {"status": "missing", "path": str(_auth_path())}

    preferred_seen = False
    expired_preferred: tuple[str, dict[str, Any]] | None = None
    fallback: tuple[str, dict[str, Any]] | None = None

    for issuer, raw in data.items():
        is_preferred = _preferred_key(str(issuer))
        preferred_seen = preferred_seen or is_preferred
        if not isinstance(raw, dict) or not isinstance(raw.get("key"), str) or not raw.get("key"):
            continue
        if is_preferred:
            if _is_fresh(raw):
                return _session("ok", str(issuer), raw)
            if expired_preferred is None:
                expired_preferred = (str(issuer), raw)
            continue
        if fallback is None:
            fallback = (str(issuer), raw)

    # If a first-party issuer exists, alternate issuers must not shadow it.
    if preferred_seen:
        if expired_preferred:
            return _session("expired", *expired_preferred)
        return {"status": "missing", "path": str(_auth_path())}

    if fallback:
        issuer, entry = fallback
        return _session("ok" if _is_fresh(entry) else "expired", issuer, entry)
    return {"status": "missing", "path": str(_auth_path())}


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


def _money(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("val")
    try:
        num = float(raw)
        return num if num == num else None
    except (TypeError, ValueError):
        return None


def _timestamps_match(a: Any, b: Any) -> bool:
    if not a or not b:
        return False
    try:
        return datetime.fromisoformat(str(a).replace("Z", "+00:00")).timestamp() == datetime.fromisoformat(str(b).replace("Z", "+00:00")).timestamp()
    except Exception:
        return False


def _confirmed_weekly_period(cfg: dict[str, Any]) -> bool:
    period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
    return (
        period.get("type") == "USAGE_PERIOD_TYPE_WEEKLY"
        and _timestamps_match(period.get("start"), cfg.get("billingPeriodStart"))
        and _timestamps_match(period.get("end"), cfg.get("billingPeriodEnd"))
    )


def _usage_scalars(cfg: dict[str, Any]) -> list[float]:
    vals = []
    for key in ("onDemandCap", "onDemandUsed", "prepaidBalance", "monthlyLimit", "used"):
        val = _money(cfg.get(key))
        if val is not None:
            vals.append(val)
    return vals


def _has_monthly_budget(cfg: dict[str, Any]) -> bool:
    limit = _money(cfg.get("monthlyLimit"))
    used = _money(cfg.get("used"))
    return limit is not None and used is not None and limit > 0


def _weekly_percent(cfg: dict[str, Any]) -> float | None:
    # The explicit field is the authoritative weekly credit value.
    if "creditUsagePercent" in cfg:
        raw = cfg.get("creditUsagePercent")
        try:
            value = float(raw)
            return value if value == value else None
        except (TypeError, ValueError):
            return None

    scalars = _usage_scalars(cfg)
    # Match Orca's current rule: if the encoder explicitly emits zero money
    # fields, an omitted weekly percentage means "not reported", not 0%.
    if any(v == 0 for v in scalars) or _has_monthly_budget(cfg):
        return None
    # Proto3 JSON can omit a default zero. Infer 0 only when the payload proves
    # the current period itself is weekly and there is no other usage evidence.
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


def fetch_grok_quota() -> dict[str, Any]:
    session = read_auth_session()
    if session["status"] == "missing":
        return {"auth": "missing"}
    if session["status"] == "expired":
        return {"auth": "expired"}

    req = urllib.request.Request(_billing_url(), headers=_headers(session))
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        # 401 really is an auth refresh problem. 403 is not necessarily one:
        # paid/team OAuth sessions can be valid for chat but denied by this
        # product billing endpoint, so prompting an endless re-login is wrong.
        if exc.code == 401:
            return {"auth": "expired"}
        if exc.code == 403:
            return _empty_row("BILLING UNAVAILABLE", error="Grok billing endpoint returned HTTP 403")
        if exc.code == 412:
            return _empty_row("NO PERSONAL BILLING", error="Grok billing endpoint returned HTTP 412 for this account/team")
        return _empty_row("BILLING ERROR", error=f"Grok billing endpoint returned HTTP {exc.code}")
    except Exception as exc:
        return _empty_row("BILLING ERROR", error=f"Grok billing request failed: {type(exc).__name__}")

    if not isinstance(data, dict):
        return _empty_row("NO WEEKLY DATA", error="Grok billing response was not an object")
    cfg = data.get("config") if isinstance(data.get("config"), dict) else data
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
        "five_hour": _empty_window(),
        "weekly": weekly,
        "source": "official",
    }


if __name__ == "__main__":
    # Safe diagnostic: never print the token.
    auth = read_auth_session()
    print(json.dumps({k: v for k, v in auth.items() if k != "token"}, ensure_ascii=False, indent=2))
