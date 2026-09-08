#!/usr/bin/env python3
"""Desktop lifecycle and staged self-update helpers for Apocalypse Spatial OS."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

APP_VERSION = "0.3.2"
REPO = "axzhang1216/apocalypse"
RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
DATA_DIR = Path.home() / ".claude" / "apocalypse"
UPDATE_DIR = DATA_DIR / "updates"
UPDATE_STATE_FILE = DATA_DIR / "update_state.json"

_state_lock = threading.RLock()
_download_thread: threading.Thread | None = None
_restart_event = threading.Event()
_restart_requested = False


def _version_tuple(value: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", str(value).split("-", 1)[0])
    return tuple(int(x) for x in nums[:4]) or (0,)


def _arch() -> str:
    return "x64" if sys.maxsize > 2**32 else "x86"


def _is_packaged() -> bool:
    return bool(getattr(sys, "frozen", False))


def _github_json(url: str, timeout: float = 8.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "accept": "application/vnd.github+json",
            "user-agent": f"Apocalypse/{APP_VERSION}",
            "x-github-api-version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _base_state(phase: str = "idle") -> dict[str, Any]:
    return {
        "phase": phase,
        "current_version": APP_VERSION,
        "target_version": None,
        "progress": 0.0,
        "downloaded": 0,
        "total": 0,
        "asset_name": None,
        "installer": None,
        "error": None,
        "restart_available": False,
    }


def _read_state() -> dict[str, Any]:
    try:
        raw = json.loads(UPDATE_STATE_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else _base_state()
    except Exception:
        return _base_state()


def _write_state(state: dict[str, Any]) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state = {**_base_state(), **state, "current_version": APP_VERSION}
    tmp = UPDATE_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, UPDATE_STATE_FILE)
    return state


def _public_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        k: state.get(k)
        for k in (
            "phase", "current_version", "target_version", "progress", "downloaded",
            "total", "asset_name", "error", "restart_available", "latest_version",
            "available", "arch", "published_at", "can_install",
        )
    }


def _normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    target = str(state.get("target_version") or "")
    if target and _version_tuple(target) <= _version_tuple(APP_VERSION):
        installer_text = str(state.get("installer") or "")
        if installer_text:
            try:
                Path(installer_text).unlink(missing_ok=True)
            except Exception:
                pass
        state = _base_state()
        try:
            UPDATE_STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    elif state.get("phase") == "install_on_exit":
        # If this executable is still the old version, the previous helper did
        # not complete (for example UAC was cancelled). Keep the staged update.
        installer_text = str(state.get("installer") or "")
        installer = Path(installer_text) if installer_text else None
        exists = bool(installer and installer.exists())
        state["phase"] = "ready" if exists else "error"
        state["restart_available"] = exists
        if not exists:
            state["error"] = "The staged installer is no longer available. Download the update again."
    state["current_version"] = APP_VERSION
    return state


def app_status() -> dict[str, Any]:
    return {
        "version": APP_VERSION,
        "arch": _arch(),
        "platform": sys.platform,
        "packaged": _is_packaged(),
        "reinitialize_supported": True,
        "self_update_supported": os.name == "nt" and _is_packaged(),
    }


def _fetch_release_info() -> dict[str, Any]:
    try:
        release = _github_json(RELEASE_API)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub update check failed: HTTP {exc.code}") from exc
    except Exception as exc:
        raise RuntimeError(f"GitHub update check failed: {exc}") from exc

    tag = str(release.get("tag_name") or "").strip()
    latest = tag[1:] if tag.lower().startswith("v") else tag
    arch = _arch()
    expected = f"Apocalypse-Setup-{arch}.exe".lower()
    asset = next(
        (a for a in (release.get("assets") or []) if str(a.get("name") or "").lower() == expected),
        None,
    )
    available = bool(latest and _version_tuple(latest) > _version_tuple(APP_VERSION))
    return {
        "current_version": APP_VERSION,
        "latest_version": latest or None,
        "tag": tag or None,
        "available": available,
        "arch": arch,
        "asset_name": asset.get("name") if isinstance(asset, dict) else None,
        "asset_url": asset.get("browser_download_url") if isinstance(asset, dict) else None,
        "asset_size": asset.get("size") if isinstance(asset, dict) else None,
        "asset_digest": asset.get("digest") if isinstance(asset, dict) else None,
        "release_url": release.get("html_url"),
        "published_at": release.get("published_at"),
        "can_install": bool(os.name == "nt" and _is_packaged() and asset and asset.get("browser_download_url")),
    }


def check_update() -> dict[str, Any]:
    """Compatibility GET used by the Settings UI.

    Active downloads are served from local state, so polling never hammers the
    GitHub API. Idle checks still query Latest Release once.
    """
    with _state_lock:
        state = _normalize_state(_read_state())
        phase = state.get("phase") or "idle"
        if phase in ("checking", "downloading", "ready", "install_on_exit", "error"):
            return _public_state(state)
    info = _fetch_release_info()
    return {**info, "phase": "available" if info.get("available") else "up_to_date", "progress": 0.0,
            "target_version": info.get("latest_version") if info.get("available") else None,
            "restart_available": False, "error": None}


def update_status(refresh_release: bool = False) -> dict[str, Any]:
    with _state_lock:
        state = _normalize_state(_read_state())
        phase = state.get("phase") or "idle"
    if refresh_release and phase not in ("checking", "downloading", "ready", "install_on_exit"):
        try:
            info = _fetch_release_info()
            with _state_lock:
                state.update({
                    "latest_version": info.get("latest_version"),
                    "available": info.get("available"),
                    "arch": info.get("arch"),
                    "published_at": info.get("published_at"),
                    "can_install": info.get("can_install"),
                })
                if not info.get("available") and phase == "idle":
                    state["phase"] = "up_to_date"
        except Exception as exc:
            state["error"] = str(exc)
    return _public_state(state)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_worker() -> None:
    global _download_thread
    try:
        info = _fetch_release_info()
        if not info.get("available"):
            with _state_lock:
                _write_state({
                    **_base_state("up_to_date"),
                    "latest_version": info.get("latest_version"),
                    "available": False,
                    "arch": info.get("arch"),
                    "can_install": info.get("can_install"),
                })
            return
        if not info.get("can_install"):
            raise RuntimeError(
                f"v{info.get('latest_version')} is available, but no {info.get('arch')} installer can be staged."
            )
        digest = str(info.get("asset_digest") or "")
        if not digest.lower().startswith("sha256:"):
            raise RuntimeError("Release asset has no SHA256 digest; refusing an unverified update.")
        expected_sha = digest.split(":", 1)[1].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise RuntimeError("Release asset SHA256 digest is invalid.")

        target = str(info["latest_version"])
        UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        version_dir = UPDATE_DIR / f"v{target}"
        version_dir.mkdir(parents=True, exist_ok=True)
        installer = version_dir / str(info["asset_name"])
        temp = installer.with_suffix(installer.suffix + ".download")
        total = int(info.get("asset_size") or 0)
        state = {
            **_base_state("downloading"),
            "target_version": target,
            "latest_version": target,
            "available": True,
            "arch": info.get("arch"),
            "asset_name": info.get("asset_name"),
            "installer": str(installer),
            "total": total,
            "expected_sha256": expected_sha,
            "published_at": info.get("published_at"),
            "can_install": True,
        }
        with _state_lock:
            _write_state(state)

        req = urllib.request.Request(
            str(info["asset_url"]),
            headers={"user-agent": f"Apocalypse/{APP_VERSION}"},
        )
        downloaded = 0
        hasher = hashlib.sha256()
        last_state_write = 0.0
        with urllib.request.urlopen(req, timeout=30) as response, open(temp, "wb") as handle:
            header_total = int(response.headers.get("Content-Length") or 0)
            if header_total > 0:
                total = header_total
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                hasher.update(chunk)
                downloaded += len(chunk)
                now = time.monotonic()
                if now - last_state_write >= 0.18:
                    progress = min(1.0, downloaded / total) if total else 0.0
                    with _state_lock:
                        state.update({"downloaded": downloaded, "total": total, "progress": progress})
                        _write_state(state)
                    last_state_write = now
            handle.flush()
            os.fsync(handle.fileno())

        actual_sha = hasher.hexdigest().lower()
        if actual_sha != expected_sha:
            temp.unlink(missing_ok=True)
            raise RuntimeError("Downloaded installer failed SHA256 verification.")
        if downloaded < 1_000_000:
            temp.unlink(missing_ok=True)
            raise RuntimeError("Downloaded installer is unexpectedly small.")
        os.replace(temp, installer)
        with _state_lock:
            state.update({
                "phase": "ready",
                "downloaded": downloaded,
                "total": total or downloaded,
                "progress": 1.0,
                "actual_sha256": actual_sha,
                "restart_available": True,
                "error": None,
            })
            _write_state(state)
    except Exception as exc:
        with _state_lock:
            state = _read_state()
            state.update({"phase": "error", "error": str(exc), "restart_available": False})
            _write_state(state)
    finally:
        with _state_lock:
            _download_thread = None


def start_update_download() -> dict[str, Any]:
    """Start release check + verified installer download and return immediately."""
    global _download_thread
    if os.name != "nt" or not _is_packaged():
        raise RuntimeError("Background installer updates are available in the packaged Windows app only.")
    with _state_lock:
        state = _normalize_state(_read_state())
        if state.get("phase") in ("checking", "downloading", "ready", "install_on_exit"):
            return _public_state(state)
        if _download_thread is not None and _download_thread.is_alive():
            return _public_state(state)
        state = _write_state({**_base_state("checking"), "error": None})
        _download_thread = threading.Thread(target=_download_worker, name="apocalypse-update-download", daemon=True)
        _download_thread.start()
        return _public_state(state)


def apply_update() -> dict[str, Any]:
    """Compatibility POST used by the Settings UI.

    First click stages the update. Once ready, the next click is RESTART NOW.
    """
    with _state_lock:
        state = _normalize_state(_read_state())
        if state.get("phase") == "ready":
            return request_restart_now()
    return start_update_download()


def _ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def arm_update_on_exit(parent_pid: int, restart: bool = False, app_executable: str | None = None) -> dict[str, Any]:
    """Launch a hidden helper that waits for this process, then silently installs."""
    if os.name != "nt" or not _is_packaged():
        return {"ok": False, "armed": False, "reason": "not packaged Windows"}
    with _state_lock:
        state = _normalize_state(_read_state())
        if state.get("phase") != "ready":
            return {"ok": False, "armed": False, "reason": "no staged update"}
        installer_text = str(state.get("installer") or "")
        installer = Path(installer_text) if installer_text else None
        if not installer or not installer.exists():
            state.update({"phase": "error", "error": "Staged installer is missing.", "restart_available": False})
            _write_state(state)
            return {"ok": False, "armed": False, "reason": "installer missing"}
        expected = str(state.get("expected_sha256") or "").lower()
        if not expected or _sha256_file(installer) != expected:
            state.update({"phase": "error", "error": "Staged installer failed final SHA256 verification.", "restart_available": False})
            _write_state(state)
            return {"ok": False, "armed": False, "reason": "digest mismatch"}

        exe = Path(app_executable or sys.executable).resolve()
        helper = installer.parent / "apply-update.ps1"
        restart_ps = "$true" if restart else "$false"
        helper.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$parentPid = {int(parent_pid)}\n"
            "while (Get-Process -Id $parentPid -ErrorAction SilentlyContinue) { Start-Sleep -Milliseconds 250 }\n"
            f"$installer = {_ps_quote(installer)}\n"
            "$args = @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/CLOSEAPPLICATIONS')\n"
            "$p = Start-Process -FilePath $installer -ArgumentList $args -Wait -PassThru\n"
            "if ($p.ExitCode -eq 0) {\n"
            f"  Remove-Item {_ps_quote(UPDATE_STATE_FILE)} -Force -ErrorAction SilentlyContinue\n"
            f"  if ({restart_ps}) {{ Start-Process -FilePath {_ps_quote(exe)} }}\n"
            "}\n",
            encoding="utf-8",
        )
        flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        proc = subprocess.Popen(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-WindowStyle", "Hidden", "-File", str(helper),
            ],
            cwd=str(installer.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
        state.update({"phase": "install_on_exit", "helper_pid": proc.pid, "restart_after_install": restart})
        _write_state(state)
        return {"ok": True, "armed": True, "pid": proc.pid, "restart": restart}


def request_restart_now() -> dict[str, Any]:
    """Ask the desktop shell to close; its finally block arms the staged updater."""
    global _restart_requested
    with _state_lock:
        state = _normalize_state(_read_state())
        if state.get("phase") != "ready":
            raise RuntimeError("No staged update is ready to install.")
        _restart_requested = True
    # Let the HTTP response reach the UI before the window is destroyed.
    threading.Timer(0.55, _restart_event.set).start()
    return {"ok": True, "restarting": True, "target_version": state.get("target_version"), "phase": "restarting"}


def wait_for_restart_request() -> None:
    _restart_event.wait()


def restart_requested() -> bool:
    with _state_lock:
        return bool(_restart_requested)
