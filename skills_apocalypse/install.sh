#!/usr/bin/env bash
set -e

DEST="$HOME/.claude/skills/apocalypse"
SRC="$(cd "$(dirname "$0")" && pwd)"
case "$OSTYPE" in
  msys*|cygwin*|win32) PLATFORM="windows" ;;
  *) PLATFORM="unix" ;;
esac
if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi

echo "Installing Apocalypse Spatial OS → $DEST"
mkdir -p "$DEST/hooks" "$HOME/bin"

FILES=(
  server.py spatial_server.py spatial_server_plus.py
  spatial_os.html spatial_os.css spatial_os.js onboarding_ui.js
  apocalypse_ui.py apocalypse-ui apocalypse-ui.cmd
  workspace_init.py platform_utils.py
  agent_discovery.py onboarding.py analysis_harness.py anthropic.py
  quota_adapters.py ops_analysis.py app_lifecycle.py
)
for f in "${FILES[@]}"; do cp "$SRC/$f" "$DEST/$f"; done
cp "$SRC/hooks/on-tool.sh" "$DEST/hooks/on-tool.sh"
cp "$SRC/hooks/on-stop.sh" "$DEST/hooks/on-stop.sh"
chmod +x "$DEST/apocalypse-ui" "$DEST/hooks/on-tool.sh" "$DEST/hooks/on-stop.sh"

if [ "$PLATFORM" = "windows" ]; then
  cp "$DEST/apocalypse-ui.cmd" "$HOME/bin/apocalypse-ui.cmd"
else
  cp "$DEST/apocalypse-ui" "$HOME/bin/apocalypse-ui"
  chmod +x "$HOME/bin/apocalypse-ui"
fi

# Optional Claude realtime enrichment. Apocalypse itself remains agent-independent.
APOCALYPSE_DIR="$DEST" "$PY" <<'PYEOF'
import json, os, shutil
from pathlib import Path
if not shutil.which('claude'):
    raise SystemExit(0)
p=Path.home()/'.claude'/'settings.local.json'
skill=os.environ['APOCALYPSE_DIR'].replace('\\','/')
try: cfg=json.loads(p.read_text('utf-8')) if p.exists() else {}
except Exception: cfg={}
hooks=cfg.setdefault('hooks',{})
def has(rows, marker): return any(marker in str(x) for x in rows)
changed=False
for event,cmd in [('PreToolUse',f'bash "{skill}/hooks/on-tool.sh" pre'),('PostToolUse',f'bash "{skill}/hooks/on-tool.sh" post')]:
    rows=hooks.setdefault(event,[])
    if not has(rows,'on-tool.sh'):
        rows.append({'matcher':'.*','hooks':[{'type':'command','command':cmd}]});changed=True
rows=hooks.setdefault('Stop',[])
if not has(rows,'on-stop.sh'):
    rows.append({'matcher':'','hooks':[{'type':'command','command':f'bash "{skill}/hooks/on-stop.sh"'}]});changed=True
if changed:
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(cfg,indent=2),encoding='utf-8')
    print('Claude hooks registered for optional realtime enrichment.')
PYEOF

echo ""
echo "Installed. Run: apocalypse-ui"
echo "First launch opens the in-app Apocalypse onboarding flow."
echo "Reconfigure later from Settings → Reinitialize."
echo "UI → http://localhost:7749"
