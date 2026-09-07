#!/usr/bin/env bash
# Apocalypse install script
# Usage: bash install.sh
# Installs the standalone UI, provider discovery/setup, and analysis harness.
set -e

DEST="$HOME/.claude/skills/apocalypse"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
case "$OSTYPE" in
    msys*|cygwin*|win32) PLATFORM="windows" ;;
    darwin*) PLATFORM="macos" ;;
    linux*) PLATFORM="linux" ;;
    *) PLATFORM="other" ;;
esac
if command -v python3 >/dev/null 2>&1 && python3 -c "" >/dev/null 2>&1; then PY=python3; else PY=python; fi

echo "Installing Apocalypse to $DEST (platform: $PLATFORM, python: $PY) ..."
mkdir -p "$DEST/hooks"

# Core web UI + standalone runtime
for f in SKILL.md server.py spatial_server.py spatial_server_plus.py spatial_os.html spatial_os.css spatial_os.js \
         apocalypse_ui.py apocalypse-ui apocalypse-ui.cmd dashboard.html start.sh workspace.html workspace_init.py \
         apocalypse.py codex_workspace.py platform_utils.py quota_adapters.py agent_discovery.py analysis_harness.py \
         setup_wizard.py ops_analysis.py anthropic.py; do
    cp "$REPO_DIR/$f" "$DEST/$f"
done
cp "$REPO_DIR/hooks/on-tool.sh" "$DEST/hooks/on-tool.sh"
cp "$REPO_DIR/hooks/on-stop.sh" "$DEST/hooks/on-stop.sh"

if [ -d "$REPO_DIR/apocalypse" ]; then
    rm -rf "$DEST/apocalypse/__pycache__"
    mkdir -p "$DEST/apocalypse"
    cp "$REPO_DIR"/apocalypse/*.py "$DEST/apocalypse/"
fi
cp "$REPO_DIR/apocalypse.sh" "$DEST/apocalypse.sh"
[ -f "$REPO_DIR/quota_sources.example.json" ] && cp "$REPO_DIR/quota_sources.example.json" "$DEST/quota_sources.example.json"
if [ "$PLATFORM" = "windows" ]; then cp "$REPO_DIR/apocalypse.cmd" "$DEST/apocalypse.cmd"; fi
for vendor in three.module.min.js OrbitControls.js; do
  if [ -f "$REPO_DIR/$vendor" ]; then cp "$REPO_DIR/$vendor" "$DEST/$vendor";
  elif [ ! -f "$DEST/$vendor" ]; then echo "WARNING: $vendor missing — legacy workspace may not load" >&2; fi
done
for tex in "$REPO_DIR"/*.png "$REPO_DIR"/*.jpg "$REPO_DIR"/*.jpeg; do [ -f "$tex" ] && cp "$tex" "$DEST/"; done
chmod +x "$DEST/start.sh" "$DEST/apocalypse.sh" "$DEST/apocalypse-ui" "$DEST/hooks/on-tool.sh" "$DEST/hooks/on-stop.sh"

# Standalone launcher
mkdir -p "$HOME/bin"
if [ "$PLATFORM" = "windows" ]; then
    cp "$DEST/apocalypse-ui.cmd" "$HOME/bin/apocalypse-ui.cmd"
    [ -f "$DEST/apocalypse.cmd" ] && cp "$DEST/apocalypse.cmd" "$HOME/bin/apocalypse.cmd"
else
    cp "$DEST/apocalypse-ui" "$HOME/bin/apocalypse-ui"; chmod +x "$HOME/bin/apocalypse-ui"
fi

# Existing apocalypse CLI alias
SHELL_NAME=""
case "$SHELL" in */zsh) SHELL_NAME="zsh";; */bash) SHELL_NAME="bash";; */fish) SHELL_NAME="fish";;
*) [ "$PLATFORM" = "macos" ] && SHELL_NAME="zsh"; [ "$PLATFORM" = "linux" ] && SHELL_NAME="bash";; esac
write_alias(){ local rc="$1" line="$2" marker="$3"; [ -z "$rc" ] || [ -z "$line" ] && return 0; if [ -f "$rc" ] && grep -qF "$marker" "$rc" 2>/dev/null; then return 0; fi; mkdir -p "$(dirname "$rc")"; touch "$rc"; printf '\n# Apocalypse launcher\n%s\n' "$line" >> "$rc"; }
if [ "$PLATFORM" = "windows" ]; then
    write_alias "$HOME/.bashrc" "alias apocalypse='PYTHONUTF8=1 $PY \"\$HOME/.claude/skills/apocalypse/apocalypse.py\"'" '.claude/skills/apocalypse/apocalypse.py'
else
    case "$SHELL_NAME" in
      zsh) write_alias "$HOME/.zshrc" "alias apocalypse='PYTHONUTF8=1 $PY \"\$HOME/.claude/skills/apocalypse/apocalypse.py\"'" '.claude/skills/apocalypse/apocalypse.py';;
      bash) write_alias "$HOME/.bashrc" "alias apocalypse='PYTHONUTF8=1 $PY \"\$HOME/.claude/skills/apocalypse/apocalypse.py\"'" '.claude/skills/apocalypse/apocalypse.py';;
      fish) write_alias "$HOME/.config/fish/config.fish" "alias apocalypse 'PYTHONUTF8=1 $PY $HOME/.claude/skills/apocalypse/apocalypse.py'" '.claude/skills/apocalypse/apocalypse.py';;
    esac
fi

# Claude hooks are optional enrichment; Apocalypse itself remains agent-independent.
APOCALYPSE_SKILL_DIR="$DEST" "$PY" <<'PYEOF'
import json, os
from pathlib import Path
p=Path.home()/'.claude'/'settings.local.json'; skill=os.environ['APOCALYPSE_SKILL_DIR'].replace('\\','/')
try: cfg=json.loads(p.read_text('utf-8')) if p.exists() else {}
except Exception: cfg={}
hooks=cfg.setdefault('hooks',{})
def has(items,marker): return any(marker in str(e) for e in items)
changed=False; on_tool=f'bash "{skill}/hooks/on-tool.sh"'
for event,cmd in [('PreToolUse',on_tool+' pre'),('PostToolUse',on_tool+' post')]:
    rows=hooks.setdefault(event,[])
    if not has(rows,'on-tool.sh'): rows.append({'matcher':'.*','hooks':[{'type':'command','command':cmd}]}); changed=True
rows=hooks.setdefault('Stop',[])
if not has(rows,'on-stop.sh'): rows.append({'matcher':'','hooks':[{'type':'command','command':f'bash "{skill}/hooks/on-stop.sh"'}]}); changed=True
if changed:
    p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(cfg,indent=2),encoding='utf-8')
    print('Claude hooks registered (optional realtime enrichment).')
PYEOF

echo ""
echo "Files installed."

# First-run discovery is interactive only; CI/package builds must never hang.
SETUP_STATE="$HOME/.claude/apocalypse/setup.json"
if [ ! -f "$SETUP_STATE" ] && [ -t 0 ] && [ -t 1 ]; then
    echo ""
    echo "Starting Apocalypse first-run provider discovery..."
    "$PY" "$DEST/setup_wizard.py"
else
    echo ""
    if [ -f "$SETUP_STATE" ]; then
        echo "Apocalypse is already initialized. Reconfigure with: apocalypse-ui init"
    else
        echo "Non-interactive install: run 'apocalypse-ui init' once to choose Plan usage + analysis model."
    fi
fi

echo ""
echo "Commands:"
echo "  apocalypse-ui             # start server + open UI"
echo "  apocalypse-ui init        # rescan agents/providers and reconfigure"
echo "  apocalypse-ui analyze     # refresh cached agenda + agent worklog analysis"
echo "  apocalypse-ui status      # inspect local server"
echo "  apocalypse-ui stop        # stop local server"
echo "  apocalypse-ui restart     # restart local server"
echo ""
echo "Workspace update uses the selected Apocalypse analysis model through its own harness."
echo "Apocalypse → http://localhost:7749"
