#!/usr/bin/env bash
# Apocalypse install script
# Usage: bash install.sh
# Installs the skill and the standalone Apocalypse UI launcher.
#
# Cross-platform: detects Windows (Git Bash / MSYS / Cygwin), macOS, Linux.
# The web UI can be started independently of Claude Code / any LLM with:
#   apocalypse-ui
# Workspace analysis remains an explicit, separate operation.

set -e

DEST="$HOME/.claude/skills/apocalypse"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

case "$OSTYPE" in
    msys*|cygwin*|win32) PLATFORM="windows" ;;
    darwin*)             PLATFORM="macos"   ;;
    linux*)              PLATFORM="linux"   ;;
    *)                   PLATFORM="other"   ;;
esac

if command -v python3 >/dev/null 2>&1 && python3 -c "" >/dev/null 2>&1; then
    PY=python3
else
    PY=python
fi

echo "Installing Apocalypse to $DEST (platform: $PLATFORM, python: $PY) ..."
mkdir -p "$DEST/hooks"

# Core web UI + compatibility backend
cp "$REPO_DIR/SKILL.md"               "$DEST/SKILL.md"
cp "$REPO_DIR/server.py"              "$DEST/server.py"
cp "$REPO_DIR/spatial_server.py"      "$DEST/spatial_server.py"
cp "$REPO_DIR/spatial_server_plus.py" "$DEST/spatial_server_plus.py"
cp "$REPO_DIR/spatial_os.html"        "$DEST/spatial_os.html"
cp "$REPO_DIR/spatial_os.css"         "$DEST/spatial_os.css"
cp "$REPO_DIR/spatial_os.js"          "$DEST/spatial_os.js"
cp "$REPO_DIR/apocalypse_ui.py"       "$DEST/apocalypse_ui.py"
cp "$REPO_DIR/apocalypse-ui"          "$DEST/apocalypse-ui"
cp "$REPO_DIR/apocalypse-ui.cmd"      "$DEST/apocalypse-ui.cmd"

# Existing dashboard / workspace / agent integration
cp "$REPO_DIR/dashboard.html"         "$DEST/dashboard.html"
cp "$REPO_DIR/start.sh"               "$DEST/start.sh"
cp "$REPO_DIR/hooks/on-tool.sh"       "$DEST/hooks/on-tool.sh"
cp "$REPO_DIR/hooks/on-stop.sh"       "$DEST/hooks/on-stop.sh"
cp "$REPO_DIR/workspace.html"         "$DEST/workspace.html"
cp "$REPO_DIR/workspace_init.py"      "$DEST/workspace_init.py"
cp "$REPO_DIR/apocalypse.py"          "$DEST/apocalypse.py"
cp "$REPO_DIR/codex_workspace.py"     "$DEST/codex_workspace.py"
cp "$REPO_DIR/platform_utils.py"      "$DEST/platform_utils.py"

if [ -d "$REPO_DIR/apocalypse" ]; then
    rm -rf "$DEST/apocalypse/__pycache__"
    mkdir -p "$DEST/apocalypse"
    cp "$REPO_DIR"/apocalypse/*.py "$DEST/apocalypse/"
fi
cp "$REPO_DIR/apocalypse.sh" "$DEST/apocalypse.sh"

if [ "$PLATFORM" = "windows" ]; then
    cp "$REPO_DIR/apocalypse.cmd" "$DEST/apocalypse.cmd"
fi

for vendor in three.module.min.js OrbitControls.js; do
  if [ -f "$REPO_DIR/$vendor" ]; then
    cp "$REPO_DIR/$vendor" "$DEST/$vendor"
  elif [ ! -f "$DEST/$vendor" ]; then
    echo "WARNING: $vendor not found in $REPO_DIR — legacy workspace.html may not load" >&2
  fi
done
for tex in "$REPO_DIR"/*.png "$REPO_DIR"/*.jpg "$REPO_DIR"/*.jpeg; do
  [ -f "$tex" ] && cp "$tex" "$DEST/"
done

chmod +x "$DEST/start.sh" "$DEST/apocalypse.sh" "$DEST/apocalypse-ui" \
         "$DEST/hooks/on-tool.sh" "$DEST/hooks/on-stop.sh"

# ── Standalone UI launcher ──────────────────────────────────────────────────
# This is deliberately independent of Claude Code and does not run workspace
# analysis or any LLM call.
mkdir -p "$HOME/bin"
if [ "$PLATFORM" = "windows" ]; then
    cp "$DEST/apocalypse-ui.cmd" "$HOME/bin/apocalypse-ui.cmd"
    [ -f "$DEST/apocalypse.cmd" ] && cp "$DEST/apocalypse.cmd" "$HOME/bin/apocalypse.cmd"
    echo "Installed standalone launcher: ~/bin/apocalypse-ui.cmd"
else
    cp "$DEST/apocalypse-ui" "$HOME/bin/apocalypse-ui"
    chmod +x "$HOME/bin/apocalypse-ui"
    echo "Installed standalone launcher: ~/bin/apocalypse-ui"
fi

# Existing apocalypse CLI alias (session browser / update commands)
SHELL_NAME=""
case "$SHELL" in
    */zsh)  SHELL_NAME="zsh"  ;;
    */bash) SHELL_NAME="bash" ;;
    */fish) SHELL_NAME="fish" ;;
    *)
        if [ "$PLATFORM" = "macos" ]; then SHELL_NAME="zsh";
        elif [ "$PLATFORM" = "linux" ]; then SHELL_NAME="bash"; fi
        ;;
esac

write_alias() {
    local rc="$1" line="$2" marker="$3"
    [ -z "$rc" ] || [ -z "$line" ] && return 0
    if [ -f "$rc" ] && grep -qF "$marker" "$rc" 2>/dev/null; then
        return 0
    fi
    mkdir -p "$(dirname "$rc")"; touch "$rc"
    printf '\n# Apocalypse launcher\n%s\n' "$line" >> "$rc"
}

if [ "$PLATFORM" = "windows" ]; then
    write_alias "$HOME/.bashrc" "alias apocalypse='PYTHONUTF8=1 $PY \"\$HOME/.claude/skills/apocalypse/apocalypse.py\"'" '.claude/skills/apocalypse/apocalypse.py'
else
    case "$SHELL_NAME" in
        zsh)  write_alias "$HOME/.zshrc"  "alias apocalypse='PYTHONUTF8=1 $PY \"\$HOME/.claude/skills/apocalypse/apocalypse.py\"'" '.claude/skills/apocalypse/apocalypse.py' ;;
        bash) write_alias "$HOME/.bashrc" "alias apocalypse='PYTHONUTF8=1 $PY \"\$HOME/.claude/skills/apocalypse/apocalypse.py\"'" '.claude/skills/apocalypse/apocalypse.py' ;;
        fish) write_alias "$HOME/.config/fish/config.fish" "alias apocalypse 'PYTHONUTF8=1 $PY $HOME/.claude/skills/apocalypse/apocalypse.py'" '.claude/skills/apocalypse/apocalypse.py' ;;
    esac
fi

# ── Claude hooks: installation-time integration, not UI startup ─────────────
APOCALYPSE_SKILL_DIR="$DEST" "$PY" <<'PYEOF'
import json, os
from pathlib import Path
p = Path.home()/'.claude'/'settings.local.json'
skill = os.environ['APOCALYPSE_SKILL_DIR'].replace('\\','/')
try:
    cfg = json.loads(p.read_text('utf-8')) if p.exists() else {}
except Exception:
    cfg = {}
hooks = cfg.setdefault('hooks', {})

def has(items, marker):
    for e in items:
        if marker in str(e): return True
    return False

changed=False
on_tool=f'bash "{skill}/hooks/on-tool.sh"'
for event, cmd in [('PreToolUse', on_tool+' pre'), ('PostToolUse', on_tool+' post')]:
    rows=hooks.setdefault(event,[])
    if not has(rows,'on-tool.sh'):
        rows.append({'matcher':'.*','hooks':[{'type':'command','command':cmd}]}); changed=True
rows=hooks.setdefault('Stop',[])
if not has(rows,'on-stop.sh'):
    rows.append({'matcher':'','hooks':[{'type':'command','command':f'bash "{skill}/hooks/on-stop.sh"'}]}); changed=True
if changed:
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(cfg,indent=2),encoding='utf-8')
    print('Claude hooks registered (optional realtime enrichment).')
PYEOF

echo ""
echo "Files installed."
echo ""
echo "Standalone UI (NO LLM):"
echo "  apocalypse-ui             # start server + open browser"
echo "  apocalypse-ui status      # inspect local server"
echo "  apocalypse-ui stop        # stop local server"
echo "  apocalypse-ui restart     # restart local server"
echo ""
echo "Direct fallback:"
echo "  $PY $DEST/apocalypse_ui.py start"
echo ""
echo "Workspace analysis remains separate:"
echo "  apocalypse --update"
echo ""
echo "Apocalypse → http://localhost:7749"
echo "Legacy dashboard → http://localhost:7749/legacy/dashboard"
echo ""
echo "NOTE: install no longer starts the web server automatically."
echo "Run 'apocalypse-ui' when you want the UI."
