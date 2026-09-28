"""Cross-platform helpers for Apocalypse.

Centralises every OS-specific behaviour (terminal launching, Python
resolution, shell rc detection) so callers (server.py, apocalypse.py,
install.sh) don't have to know which platform they're on.

This file is a stdlib-only module. Imported by both server.py and
apocalypse.py; called indirectly from install.sh / start.sh via
embedded Python heredocs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal, Optional

Platform = Literal["windows", "macos", "linux", "other"]


# ────────────────────────── Platform detection ──────────────────────────

def get_platform() -> Platform:
    """Return one of 'windows', 'macos', 'linux', 'other'."""
    p = sys.platform
    if p == "win32" or p.startswith("cygwin") or p.startswith("msys"):
        return "windows"
    if p == "darwin":
        return "macos"
    if p.startswith("linux"):
        return "linux"
    return "other"


def is_windows() -> bool:
    return get_platform() == "windows"


def is_macos() -> bool:
    return get_platform() == "macos"


def is_linux() -> bool:
    return get_platform() == "linux"


# ────────────────────────── Python resolution ──────────────────────────

def get_python() -> str:
    """Return absolute path to a working Python 3 interpreter.

    Windows has a broken 'python3' stub in WindowsApps that opens the
    Microsoft Store instead of running python — never use it. macOS and
    Linux ship 'python3' (and may not have 'python' at all).
    """
    if is_windows():
        py = shutil.which("python")
        if py:
            return py
        return "python"
    # macOS / Linux: prefer python3
    py = shutil.which("python3")
    if py:
        return py
    py = shutil.which("python")
    if py:
        return py
    return "python3"


# ────────────────────────── Terminal launching ──────────────────────────

def launch_in_terminal(command: str, cwd: Optional[str] = None) -> bool:
    """Launch a command in a new visible terminal window.

    Returns True on success, False on failure (e.g. headless server with
    no GUI). Never raises — callers are happy with a no-op fallback.
    """
    plat = get_platform()
    if plat == "windows":
        return _launch_windows(command, cwd)
    if plat == "macos":
        return _launch_macos(command, cwd)
    if plat == "linux":
        return _launch_linux(command, cwd)
    return _launch_unix_shell(command, cwd)  # fallback for "other"


def _launch_windows(command: str, cwd: Optional[str]) -> bool:
    """Windows: write a temp .bat, open in wt (Windows Terminal) or cmd.

    Behaviour is intentionally identical to the pre-refactor code in
    server.py:653-664 and apocalypse.py:491-509.
    """
    try:
        bat = tempfile.NamedTemporaryFile(
            mode="w", suffix=".bat", delete=False,
            encoding="utf-8", dir=str(Path.home()),
        )
        bat.write("@echo off\n")
        if cwd:
            bat.write(f'cd /d "{cwd}"\n')
        bat.write(f"{command}\n")
        bat.close()

        wt = shutil.which("wt")
        if wt:
            args = [wt, "-w", "0", "nt", "--title", "Claude Code"]
            if cwd:
                args += ["-d", cwd]
            args += ["cmd", "/k", bat.name]
            subprocess.Popen(args)
        else:
            os.system(f'start "Claude Code" cmd /k "{bat.name}"')
        return True
    except Exception as e:
        print(f"[platform_utils] Windows launch failed: {e}", file=sys.stderr)
        return False


def _launch_macos(command: str, cwd: Optional[str]) -> bool:
    """macOS: write a .command script and `open` it (Terminal.app)."""
    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".command", delete=False,
            encoding="utf-8", dir=str(Path.home()),
        )
        tmp.write("#!/bin/bash\n")
        if cwd:
            tmp.write(f'cd "{cwd}"\n')
        tmp.write(f"{command}\n")
        tmp.close()
        os.chmod(tmp.name, 0o755)
        subprocess.Popen(["open", tmp.name])
        return True
    except Exception as e:
        print(f"[platform_utils] macOS launch failed: {e}", file=sys.stderr)
        return False


def _launch_linux(command: str, cwd: Optional[str]) -> bool:
    """Linux: probe for a terminal emulator and exec into it.

    Probes the XDG-default `x-terminal-emulator` first (works on most
    distros regardless of DE), then falls back to a few well-known names.
    The trailing `; bash` keeps the window open after the command exits
    (matches Windows `cmd /k` behaviour).
    """
    cd_prefix = f'cd "{cwd}" && ' if cwd else ""
    inner = f'{cd_prefix}{command}; bash'

    candidates = [
        ("x-terminal-emulator", _args_xdg),
        ("gnome-terminal",      _args_gnome),
        ("konsole",             _args_konsole),
        ("xfce4-terminal",      _args_xdg),
        ("mate-terminal",       _args_xdg),
        ("lxterminal",          _args_xdg),
        ("alacritty",           _args_alacritty),
        ("kitty",               _args_kitty),
        ("urxvt",               _args_xterm_like),
        ("xterm",               _args_xterm_like),
    ]

    for binary, arg_fn in candidates:
        path = shutil.which(binary)
        if not path:
            continue
        try:
            args = arg_fn(path, inner)
            subprocess.Popen(args, start_new_session=True)
            return True
        except Exception as e:
            print(f"[platform_utils] {binary} launch failed: {e}", file=sys.stderr)
            continue

    print(
        "[platform_utils] No terminal emulator found. Install one of:\n"
        "  x-terminal-emulator (XDG), gnome-terminal, konsole,\n"
        "  xfce4-terminal, alacritty, kitty, xterm",
        file=sys.stderr,
    )
    return False


def _args_xdg(terminal: str, inner: str) -> list:
    """Default `-e bash -c '...'` form for most XDG terminals."""
    return [terminal, "-e", "bash", "-c", inner]


def _args_gnome(terminal: str, inner: str) -> list:
    """gnome-terminal needs `--` separator before the command."""
    return [terminal, "--", "bash", "-c", inner]


def _args_konsole(terminal: str, inner: str) -> list:
    return [terminal, "-e", "bash", "-c", inner]


def _args_alacritty(terminal: str, inner: str) -> list:
    return [terminal, "-e", "bash", "-c", inner]


def _args_kitty(terminal: str, inner: str) -> list:
    return [terminal, "bash", "-c", inner]


def _args_xterm_like(terminal: str, inner: str) -> list:
    """xterm/urxvt take a single command string after -e."""
    return [terminal, "-e", f'bash -c "{inner}"']


def _launch_unix_shell(command: str, cwd: Optional[str]) -> bool:
    """Fallback for unknown Unix-ish platforms: background bash subshell.

    Not a real terminal window, but at least runs the command.
    """
    try:
        cd = f'cd "{cwd}" && ' if cwd else ""
        subprocess.Popen(["bash", "-c", f"{cd}{command} &"], start_new_session=True)
        return True
    except Exception as e:
        print(f"[platform_utils] Unix fallback launch failed: {e}", file=sys.stderr)
        return False


# ────────────────────────── Shell rc detection ──────────────────────────

def get_shell_rc_path() -> Optional[Path]:
    """Return the user's shell rc file path, or None on unsupported shells.

    Detection priority:
      1. $SHELL env var basename (zsh / bash / fish)
      2. Platform default (macOS → zsh, Linux → bash, Windows → None)
    """
    shell = os.environ.get("SHELL", "")
    home = Path.home()

    if shell.endswith("zsh"):
        return home / ".zshrc"
    if shell.endswith("bash"):
        return home / ".bashrc"
    if shell.endswith("fish"):
        return home / ".config" / "fish" / "config.fish"

    # No $SHELL or unknown — fall back to platform default
    plat = get_platform()
    if plat == "macos":
        return home / ".zshrc"
    if plat == "linux":
        return home / ".bashrc"
    return None  # Windows: no shell rc


def get_shell_name() -> str:
    """Return 'zsh', 'bash', 'fish', or '' for unknown."""
    shell = os.environ.get("SHELL", "")
    for name in ("zsh", "bash", "fish"):
        if shell.endswith(name):
            return name
    return ""


# ────────────────────────── Self-test (CLI) ──────────────────────────

if __name__ == "__main__":  # pragma: no cover
    print(f"platform:       {get_platform()}")
    print(f"python:         {get_python()}")
    print(f"shell rc:       {get_shell_rc_path()}")
    print(f"shell name:     {get_shell_name() or '(unknown)'}")
