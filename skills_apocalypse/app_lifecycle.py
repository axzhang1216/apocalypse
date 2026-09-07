#!/usr/bin/env python3
"""Desktop lifecycle helpers for Apocalypse settings.

Keeps first-run reconfiguration and Windows installer updates out of the UI
server implementation. No LLM call is made by this module.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

APP_VERSION = "0.2.0"
REPO = "axzhang1216/apocalypse"
RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _win_creationflags(new_console: bool = False) -> int:
    if os.name != "nt":
        return 0
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if new_console:
        flags |= getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    return flags


def _version_tuple(value: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", str(value).split("-", 1)[0])
    return tuple(int(x) for x in nums[:4]) or (0,)


def _arch() -> str:
    return "x64" if sys.maxsize > 2**32 else "x86"


def _github_json(url: str, timeout: float = 8.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "accept": "application/vnd.github+json",
            "user-agent": f"Apocalypse/{APP_VERSION}",
            "x-github-api-version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def app_status() -> dict[str, Any]:
    return {
        "version": APP_VERSION,
        "arch": _arch(),
        "platform": sys.platform,
        "packaged": _is_frozen(),
        "reinitialize_supported": os.name == "nt" or not _is_frozen(),
        "self_update_supported": os.name == "nt",
    }


def _setup_command() -> list[str]:
    if _is_frozen():
        companion = Path(sys.executable).with_name("ApocalypseSetup.exe")
        if not companion.exists():
            raise RuntimeError(f"Configure Apocalypse companion not found: {companion}")
        return [str(companion), "--force"]
    script = Path(__file__).resolve().with_name("setup_wizard.py")
    if not script.exists():
        raise RuntimeError(f"setup_wizard.py not found: {script}")
    return [sys.executable, str(script), "--force"]


def launch_reinitialize() -> dict[str, Any]:
    cmd = _setup_command()
    kwargs: dict[str, Any] = {"cwd": str(Path(cmd[0]).resolve().parent)}
    if os.name == "nt":
        kwargs["creationflags"] = _win_creationflags(new_console=True)
    else:
        # Source installs on Unix inherit the launching terminal. The native
        # desktop installer path is Windows-focused today.
        kwargs["start_new_session"] = True
        kwargs["stdin"] = subprocess.DEVNULL
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    proc = subprocess.Popen(cmd, **kwargs)
    return {"ok": True, "pid": proc.pid, "message": "Apocalypse setup launched."}


def check_update() -> dict[str, Any]:
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
        "release_url": release.get("html_url"),
        "published_at": release.get("published_at"),
        "can_install": bool(os.name == "nt" and asset and asset.get("browser_download_url")),
    }


def _download(url: str, destination: Path) -> None:
    req = urllib.request.Request(url, headers={"user-agent": f"Apocalypse/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=30) as r, open(destination, "wb") as f:
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
        f.flush()
        os.fsync(f.fileno())


def apply_update() -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("Automatic installer updates are currently supported on Windows only.")
    info = check_update()
    if not info.get("available"):
        return {"ok": True, "launched": False, **info, "message": "Apocalypse is already up to date."}
    if not info.get("asset_url"):
        raise RuntimeError(f"Release {info.get('tag') or ''} has no {info['arch']} installer asset.")

    update_dir = Path(tempfile.gettempdir()) / "Apocalypse" / "updates" / str(info.get("tag") or "latest")
    update_dir.mkdir(parents=True, exist_ok=True)
    installer = update_dir / str(info.get("asset_name") or f"Apocalypse-Setup-{info['arch']}.exe")
    tmp = installer.with_suffix(installer.suffix + ".download")
    try:
        _download(str(info["asset_url"]), tmp)
        if tmp.stat().st_size < 1_000_000:
            raise RuntimeError("Downloaded installer is unexpectedly small.")
        os.replace(tmp, installer)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

    proc = subprocess.Popen(
        [str(installer)],
        cwd=str(installer.parent),
        creationflags=_win_creationflags(new_console=False),
    )
    return {
        "ok": True,
        "launched": True,
        **info,
        "installer": str(installer),
        "pid": proc.pid,
        "message": "Update installer launched. Complete the installer to apply the update.",
    }
