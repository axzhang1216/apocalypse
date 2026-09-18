"""Small, dependency-free Feishu calendar and task adapter.

Credentials are read from Apocalypse's private secrets.json (or environment
variables).  This module deliberately returns a safe status object: tokens
and secrets never leave the server or appear in API responses.
"""
from __future__ import annotations

import json
import os
import secrets as _pysecrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://open.feishu.cn/open-apis"
TZ = ZoneInfo(os.getenv("APOCALYPSE_TIMEZONE", "Asia/Shanghai"))
SECRETS = Path.home() / ".claude" / "apocalypse" / "secrets.json"
REDIRECT_URI = os.getenv("FEISHU_REDIRECT_URI") or "http://localhost:7749/api/feishu/oauth/callback"
AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
OAUTH_SCOPES = os.getenv("FEISHU_OAUTH_SCOPES") or "offline_access calendar:calendar:read calendar:calendar task:task:read"
_token = {"value": None, "expires": 0.0}
_user_tok_cache = {"value": None, "expires": 0.0}
_pending_states = {}
_lock = threading.Lock()


def _credentials():
    data = {}
    try:
        data = json.loads(SECRETS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    app_id = os.getenv("FEISHU_APP_ID") or data.get("feishu_app_id") or data.get("app_id")
    app_secret = os.getenv("FEISHU_APP_SECRET") or data.get("feishu_app_secret") or data.get("app_secret")
    return app_id, app_secret


def configured():
    return all(_credentials())


def _request(method, path, *, params=None, body=None, token=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": "Bearer " + (token or _access_token()), "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read().decode("utf-8", errors="replace"))
        except ValueError:
            result = {"code": exc.code, "msg": str(exc)}
        raise FeishuError(result.get("msg") or f"HTTP {exc.code}", result.get("code"), result) from exc
    if result.get("code", 0) != 0:
        raise FeishuError(result.get("msg") or "Feishu API error", result.get("code"), result)
    return result.get("data") or {}


class FeishuError(RuntimeError):
    def __init__(self, message, code=None, payload=None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


def _access_token():
    app_id, app_secret = _credentials()
    if not app_id or not app_secret:
        raise FeishuError("Feishu app credentials are not configured")
    with _lock:
        if _token["value"] and _token["expires"] > time.time() + 60:
            return _token["value"]
        payload = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
        req = urllib.request.Request(BASE + "/auth/v3/tenant_access_token/internal", data=payload,
                                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise FeishuError("could not obtain Feishu tenant token") from exc
        if result.get("code", 0) != 0 or not result.get("tenant_access_token"):
            raise FeishuError(result.get("msg") or "could not obtain Feishu tenant token", result.get("code"), result)
        _token.update(value=result["tenant_access_token"], expires=time.time() + int(result.get("expire", 7200)))
        return _token["value"]


def _load_secrets():
    try:
        return json.loads(SECRETS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_secrets(update):
    data = _load_secrets()
    data.update(update)
    SECRETS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def authorize_url(state):
    query = urllib.parse.urlencode({"app_id": _credentials()[0], "redirect_uri": REDIRECT_URI,
                                    "response_type": "code", "state": state, "scope": OAUTH_SCOPES},
                                   quote_via=urllib.parse.quote)
    return AUTHORIZE_URL + "?" + query


def oauth_state():
    state = _pysecrets.token_urlsafe(16)
    now = time.time()
    for key in [k for k, v in _pending_states.items() if now - v > 600]:
        _pending_states.pop(key, None)
    _pending_states[state] = now
    return state


def _token_request(payload):
    app_id, app_secret = _credentials()
    if not app_id or not app_secret:
        raise FeishuError("Feishu app credentials are not configured")
    v2_error = None
    body = json.dumps({**payload, "client_id": app_id, "client_secret": app_secret}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + "/authen/v2/oauth/token", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("code", 0) == 0 and result.get("access_token"):
            return result
        v2_error = result.get("msg") or "token exchange failed"
    except urllib.error.HTTPError as exc:
        try:
            v2_error = json.loads(exc.read().decode("utf-8", errors="replace")).get("msg") or f"HTTP {exc.code}"
        except ValueError:
            v2_error = f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        v2_error = str(exc)
    legacy = {"grant_type": payload.get("grant_type"), "client_id": app_id, "client_secret": app_secret}
    for key in ("code", "refresh_token", "redirect_uri"):
        if payload.get(key):
            legacy[key] = payload[key]
    req = urllib.request.Request(BASE + "/authen/v1/oauth/token", data=urllib.parse.urlencode(legacy).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        data = result.get("data") or {}
        if result.get("code", 0) == 0 and data.get("access_token"):
            return {**data, "code": 0}
        raise FeishuError(result.get("msg") or v2_error or "token exchange failed")
    except FeishuError:
        raise
    except Exception as exc:
        raise FeishuError(v2_error or str(exc)) from exc


def complete_oauth(code, state):
    issued = _pending_states.pop(state, None)
    if issued is None or time.time() - issued > 600:
        return False, "授权状态已过期，请重新发起授权"
    if not code:
        return False, "缺少授权码 code"
    data = _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI})
    access = data.get("access_token")
    if not access:
        return False, data.get("msg") or "token exchange failed"
    update = {
        "feishu_user_access_token": access,
        "feishu_user_refresh_token": data.get("refresh_token") or "",
        "feishu_user_expires_at": time.time() + int(data.get("expires_in") or 7200),
        "feishu_user_refresh_expires_at": time.time() + int(data.get("refresh_expires_in") or 2592000),
        "feishu_user_scope": data.get("scope") or "",
    }
    _save_secrets(update)
    _user_tok_cache.update(value=access, expires=update["feishu_user_expires_at"])
    name = ""
    try:
        info = _request("GET", "/authen/v1/user_info", token=access)
        name = info.get("name") or ""
        if name:
            _save_secrets({"feishu_user_name": name})
    except Exception:
        pass
    return True, name or "授权完成"


def _user_token():
    if _user_tok_cache["value"] and time.time() < _user_tok_cache["expires"] - 60:
        return _user_tok_cache["value"]
    sec = _load_secrets()
    tok = sec.get("feishu_user_access_token")
    exp = float(sec.get("feishu_user_expires_at") or 0)
    if tok and time.time() < exp - 60:
        _user_tok_cache.update(value=tok, expires=exp)
        return tok
    refresh = sec.get("feishu_user_refresh_token")
    rexp = float(sec.get("feishu_user_refresh_expires_at") or 0)
    if not refresh or (rexp and time.time() > rexp):
        return None
    data = _token_request({"grant_type": "refresh_token", "refresh_token": refresh})
    access = data.get("access_token")
    if not access:
        return None
    _save_secrets({
        "feishu_user_access_token": access,
        "feishu_user_refresh_token": data.get("refresh_token") or refresh,
        "feishu_user_expires_at": time.time() + int(data.get("expires_in") or 7200),
        "feishu_user_refresh_expires_at": time.time() + int(data.get("refresh_expires_in") or 2592000),
    })
    return access


def _identity_token():
    try:
        return _user_token()
    except FeishuError:
        return None


def clear_oauth():
    _save_secrets({"feishu_user_access_token": "", "feishu_user_refresh_token": "",
                   "feishu_user_expires_at": 0, "feishu_user_refresh_expires_at": 0})
    _user_tok_cache.update(value=None, expires=0.0)


def oauth_status():
    if not configured():
        return {"configured": False, "authorized": False, "message": "FEISHU APP NOT CONFIGURED"}
    try:
        authorized = _user_token() is not None
    except FeishuError:
        authorized = False
    sec = _load_secrets()
    return {"configured": True, "authorized": authorized, "redirect_uri": REDIRECT_URI,
            "user_name": sec.get("feishu_user_name") or "",
            "message": "FEISHU USER AUTHORIZED" if authorized else "FEISHU USER AUTH REQUIRED"}


def _day_window(day=None):
    if day is None:
        day = datetime.now(TZ).date()
    elif isinstance(day, str):
        day = datetime.strptime(day, "%Y-%m-%d").date()
    elif isinstance(day, datetime):
        day = day.astimezone(TZ).date()
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    return start, start + timedelta(days=1)


def _timestamp(value):
    try:
        return int(str(value))
    except (ValueError, TypeError):
        return None


def _ms_to_s(value):
    """Normalize a timestamp to seconds; task v2 uses milliseconds, calendar uses seconds."""
    ts = _timestamp(value)
    if ts and ts > 10_000_000_000:  # milliseconds
        ts //= 1000
    return ts


def _is_done(item):
    return str(item.get("status") or "") == "done" or bool(_ms_to_s(item.get("completed_at")))


def _event(item):
    start = item.get("start_time") or {}
    end = item.get("end_time") or {}
    st = _timestamp(start.get("timestamp"))
    et = _timestamp(end.get("timestamp"))
    def clock(ts):
        return datetime.fromtimestamp(ts, TZ).strftime("%H:%M") if ts else "—"
    return {"id": item.get("event_id"), "source": "feishu", "start": clock(st), "end": clock(et),
            "title": item.get("summary") or "Untitled event", "project": "FEISHU CALENDAR",
            "description": item.get("description") or "", "start_ts": st, "end_ts": et,
            "url": item.get("url") or ""}


def _task(item):
    due = item.get("due") or {}
    ts = _ms_to_s(due.get("timestamp"))
    return {"id": item.get("guid") or item.get("task_id"), "source": "feishu", "title": item.get("summary") or "Untitled task",
            "description": item.get("description") or "", "priority": str(item.get("priority") or "medium").lower(),
            "due": datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M") if ts else "", "due_ts": ts,
            "completed": _is_done(item)}


def _calendar_ids(token):
    primary = _request("GET", "/calendar/v4/calendars/primary", token=token)
    calendar_id = primary.get("calendar_id")
    cal_list = []
    try:
        cals = _request("GET", "/calendar/v4/calendars", params={"page_size": 50}, token=token)
        cal_list = [c.get("calendar_id") for c in (cals.get("calendar_list") or []) if c.get("calendar_id")]
    except FeishuError:
        pass
    if calendar_id and calendar_id not in cal_list:
        cal_list.insert(0, calendar_id)
    return calendar_id, cal_list


def _events_for_window(token, start, end, cal_list):
    events_raw = []
    for cid in cal_list:
        try:
            ev = _request("GET", "/calendar/v4/calendars/" + urllib.parse.quote(cid, safe="") + "/events/instance_view",
                          params={"start_time": int(start.timestamp()), "end_time": int(end.timestamp()), "page_size": 100}, token=token)
            events_raw.extend(ev.get("items") or [])
        except FeishuError:
            continue
    return sorted([_event(x) for x in events_raw], key=lambda e: e.get("start_ts") or 0)


def fetch_day_events(day):
    """Return Feishu calendar events for a YYYY-MM-DD day (or empty list)."""
    if not configured():
        return []
    try:
        user_tok = _user_token()
    except FeishuError:
        user_tok = None
    if not user_tok:
        return []
    try:
        _, cal_list = _calendar_ids(user_tok)
        start, end = _day_window(day)
        return _events_for_window(user_tok, start, end, cal_list)
    except FeishuError:
        return []


def fetch_schedule():
    if not configured():
        return None, {"configured": False, "connected": False, "authorized": False, "message": "FEISHU CREDENTIALS NOT CONFIGURED"}
    try:
        user_tok = _user_token()
    except FeishuError:
        user_tok = None
    authorized = bool(user_tok)
    ident = "USER" if authorized else "APP"
    try:
        calendar_id, cal_list = _calendar_ids(user_tok)
        start, end = _day_window()
        events = _events_for_window(user_tok, start, end, cal_list)
        task_error = None
        try:
            tasks = _request("GET", "/task/v2/tasks", params={"page_size": 100}, token=user_tok)
        except FeishuError as exc:
            tasks = {"items": []}
            task_error = exc
        message = f"FEISHU LIVE · {ident} · CALENDAR + TASKS"
        if task_error:
            message = f"FEISHU LIVE · {ident} · TASKS UNAVAILABLE · {str(task_error)[:130]}"
        return (
            {"events": events,
             "tasks": [_task(x) for x in tasks.get("items", []) if not _is_done(x)],
             "suggested": [], "source": "feishu", "calendar_id": calendar_id},
            {"configured": True, "authorized": authorized, "connected": True, "tasks_connected": task_error is None,
             "message": message, "task_error_code": task_error.code if task_error else None},
        )
    except FeishuError as exc:
        permission = "permission" if exc.code in (99991672, 99991671) or "scope" in str(exc).lower() else "api"
        return None, {"configured": True, "authorized": authorized, "connected": False,
                      "message": f"FEISHU {ident} {permission.upper()} ERROR · {str(exc)[:180]}", "code": exc.code}


def create_event(summary, start_ts, end_ts, description=""):
    primary = _request("GET", "/calendar/v4/calendars/primary", token=_identity_token())
    cid = urllib.parse.quote(primary["calendar_id"], safe="")
    return _request("POST", f"/calendar/v4/calendars/{cid}/events", body={"summary": summary, "description": description,
        "start_time": {"timestamp": str(int(start_ts)), "timezone": str(TZ)}, "end_time": {"timestamp": str(int(end_ts)), "timezone": str(TZ)},
        "need_notification": True})


def create_task(summary, due_ts=None, description=""):
    body = {"summary": summary, "description": description}
    if due_ts:
        body["due"] = {"timestamp": str(int(due_ts)), "timezone": str(TZ)}
    return _request("POST", "/task/v2/tasks", body=body, token=_identity_token())


def status():
    if not configured():
        return {"configured": False, "connected": False, "authorized": False, "message": "FEISHU CREDENTIALS NOT CONFIGURED"}
    _, info = fetch_schedule()
    info["redirect_uri"] = REDIRECT_URI
    info["scope"] = _load_secrets().get("feishu_user_scope") or ""
    return info
