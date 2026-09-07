#!/usr/bin/env python3
"""Standalone launcher and first-run entrypoint for Apocalypse Spatial OS.

UI startup itself never needs an LLM. `init` discovers existing agents/providers
and configures Apocalypse's own analysis harness; `analyze` explicitly refreshes
cached agenda/worklog analysis.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

PORT = 7749
HOST = "127.0.0.1"
URL = f"http://localhost:{PORT}"
DATA_DIR = Path.home() / ".claude" / "apocalypse"
PID_FILE = DATA_DIR / "server.pid"
LOG_FILE = DATA_DIR / "server.log"
SETUP_FILE = DATA_DIR / "setup.json"
SKILL_DIR = Path(__file__).resolve().parent
SPATIAL_SERVER = SKILL_DIR / "spatial_server_plus.py"
LEGACY_SERVER = SKILL_DIR / "server.py"
SETUP_SCRIPT = SKILL_DIR / "setup_wizard.py"
OPS_ANALYSIS = SKILL_DIR / "ops_analysis.py"


def _http_ok(path: str, timeout: float = 0.8) -> bool:
    try:
        with urllib.request.urlopen(URL + path, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def _port_open() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0); return True
    except OSError:
        return False
    except Exception:
        return _port_open()


def _owned_process_alive() -> tuple[bool, int | None]:
    pid = _read_pid()
    return _pid_alive(pid), pid


def _server_kind() -> str | None:
    if _http_ok("/api/world"):
        return "spatial"
    if _http_ok("/api/events"):
        return "legacy"
    return None


def status_payload() -> dict:
    owned, pid = _owned_process_alive(); kind = _server_kind()
    return {"running": bool(kind), "kind": kind, "pid": pid if owned else None,
            "port": PORT, "url": URL, "owned_process": owned, "port_open": _port_open(),
            "initialized": SETUP_FILE.exists(), "log": str(LOG_FILE)}


def print_status() -> int:
    info = status_payload(); print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0 if info["running"] else 1


def _terminate_pid(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def stop_server(quiet: bool = False) -> bool:
    owned, pid = _owned_process_alive()
    if not owned or not pid:
        if PID_FILE.exists() and not _port_open():
            try: PID_FILE.unlink()
            except OSError: pass
        if not quiet: print("Apocalypse is not running as an owned process.")
        return not _port_open()
    _terminate_pid(pid)
    deadline = time.time() + 4.0
    while time.time() < deadline:
        if not _pid_alive(pid) and not _port_open(): break
        time.sleep(0.1)
    if _pid_alive(pid) and os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    try:
        if PID_FILE.exists() and _read_pid() == pid: PID_FILE.unlink()
    except OSError: pass
    stopped = not _port_open()
    if not quiet: print("Apocalypse stopped." if stopped else f"Port {PORT} is still in use.")
    return stopped


def _entrypoint() -> Path:
    if SPATIAL_SERVER.exists(): return SPATIAL_SERVER
    if LEGACY_SERVER.exists(): return LEGACY_SERVER
    raise FileNotFoundError("Neither spatial_server_plus.py nor server.py exists beside apocalypse_ui.py")


def _spawn_server(entry: Path) -> subprocess.Popen:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "ab", buffering=0)
    kwargs = {"cwd": str(SKILL_DIR), "stdin": subprocess.DEVNULL, "stdout": log,
              "stderr": subprocess.STDOUT, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                   | getattr(subprocess, "DETACHED_PROCESS", 0)
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen([sys.executable, str(entry)], **kwargs)
    finally:
        log.close()


def start_server(open_browser: bool = True) -> bool:
    kind = _server_kind()
    if kind == "spatial":
        print(f"Apocalypse Spatial OS already running → {URL}")
        if open_browser: webbrowser.open(URL)
        return True
    owned, pid = _owned_process_alive()
    if kind == "legacy" and owned and pid:
        print(f"Upgrading Apocalypse-owned legacy server (pid {pid}) to Spatial OS…")
        if not stop_server(quiet=True):
            print(f"Could not stop existing Apocalypse process. Check {LOG_FILE}", file=sys.stderr); return False
    elif _port_open():
        print(f"Port {PORT} is already occupied by a process that does not expose Apocalypse Spatial OS.\nRefusing to kill an unrelated process.", file=sys.stderr); return False
    entry = _entrypoint(); proc = _spawn_server(entry); deadline = time.time() + 6.0
    while time.time() < deadline:
        if _server_kind() == ("spatial" if entry == SPATIAL_SERVER else "legacy"):
            print(f"Apocalypse started (pid {proc.pid}) → {URL}")
            if open_browser: webbrowser.open(URL)
            return True
        if proc.poll() is not None: break
        time.sleep(0.12)
    print(f"Apocalypse failed to start. Check {LOG_FILE}", file=sys.stderr); return False


def open_ui() -> bool:
    if not _server_kind():
        print("Apocalypse is not running. Starting it first…"); return start_server(open_browser=True)
    webbrowser.open(URL); print(f"Opened {URL}"); return True


def run_init(force: bool = True) -> int:
    if not SETUP_SCRIPT.exists():
        print("setup_wizard.py is missing from this installation.", file=sys.stderr); return 1
    cmd = [sys.executable, str(SETUP_SCRIPT)] + (["--force"] if force else [])
    return subprocess.run(cmd, cwd=str(SKILL_DIR)).returncode


def run_analysis(hours: int = 24) -> int:
    if not OPS_ANALYSIS.exists():
        print("ops_analysis.py is missing from this installation.", file=sys.stderr); return 1
    return subprocess.run([sys.executable, str(OPS_ANALYSIS), "--hours", str(hours)], cwd=str(SKILL_DIR)).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apocalypse Spatial OS launcher and local analysis harness.")
    parser.add_argument("action", nargs="?", default="start",
                        choices=("start", "stop", "restart", "status", "open", "init", "analyze"))
    parser.add_argument("--no-open", action="store_true", help="Do not open a browser after start/restart")
    parser.add_argument("--hours", type=int, default=24, help="Worklog window for analyze")
    args = parser.parse_args(argv)
    if args.action == "status": return print_status()
    if args.action == "stop": return 0 if stop_server() else 1
    if args.action == "open": return 0 if open_ui() else 1
    if args.action == "init": return run_init(force=True)
    if args.action == "analyze": return run_analysis(max(1, args.hours))
    if args.action == "restart":
        if _owned_process_alive()[0]: stop_server(quiet=True)
        elif _port_open() and not _server_kind():
            print(f"Port {PORT} is occupied by an unrelated process; restart aborted.", file=sys.stderr); return 1
        return 0 if start_server(open_browser=not args.no_open) else 1
    if not SETUP_FILE.exists() and sys.stdin.isatty():
        print("Apocalypse has not been initialized yet. Starting first-run setup…")
        if run_init(force=False) != 0: return 1
    elif not SETUP_FILE.exists():
        print("Apocalypse is not initialized. Run `apocalypse-ui init` to choose Plan usage and the analysis model.", file=sys.stderr)
    return 0 if start_server(open_browser=not args.no_open) else 1


if __name__ == "__main__":
    raise SystemExit(main())
