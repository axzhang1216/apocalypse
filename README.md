# Apocalypse Spatial OS

**Your agents. One memory.**

Apocalypse is a local workspace for understanding and operating AI-agent work across projects. It combines a spatial project memory, live/recent sessions, activity history, Plan usage, agenda context, and an Apocalypse-owned analysis harness in one interface.

## Current product

Apocalypse now has one UI: **Spatial OS**.

- **SPACE** — projects, sessions and discussion/decision memory in one semantic universe.
- **OPS** — activity, LLM Plan usage, agenda, current sessions and active agents.
- **Session tools** — open/export/resume Claude sessions, plus safe `CLEAN NON-TEXT` and `REPAIR JSONL` maintenance.
- **Analysis harness** — Apocalypse chooses and calls its own analysis model rather than depending on one specific agent.
- **Settings** — UI scale, in-app reinitialize, and Windows self-update.

The old dashboard, Three.js nebula workspace and `apocalypse.py` TUI are no longer part of the product.

## First launch

The first run stays inside the main window.

1. `APOCALYPSE · YOUR AGENTS. ONE MEMORY.` appears over the Spatial OS background.
2. Apocalypse scans supported local agents and their configured provider/model paths.
3. Choose which detected Plans should appear in **LLM QUOTA**.
4. Choose the provider + model Apocalypse should use for analysis.
5. Apocalypse verifies the selected model with a minimal request and saves local configuration.

Supported discovery currently includes Claude Code, Codex, Hermes, Pi and OpenClaw configuration paths. Browser-facing discovery never returns API keys or tokens.

Re-run the same flow later from **Settings → Reinitialize**.

## Local configuration

Apocalypse keeps its own state under:

```text
~/.claude/apocalypse/
├── setup.json
├── harness.json
├── quota_sources.json
├── secrets.json
├── workspace.json
├── events.jsonl
└── repair_backups/
```

Source-agent configuration is read for discovery but is not rewritten by onboarding.

## Analysis transports

The harness currently supports:

- Anthropic Messages-compatible HTTP
- OpenAI Responses-compatible HTTP
- OpenAI Chat Completions-compatible HTTP
- authenticated Claude Code CLI
- authenticated Codex CLI
- authenticated Hermes one-shot CLI

Workspace/session analysis, discussion-decision extraction, compact conversation analysis, schedule analysis and agent worklog analysis all use the selected Apocalypse analysis model.

## Plan usage

The current personal Plan set is:

- Claude
- OpenAI / Codex
- Grok
- Volc Agent
- MiniMax International

The OPS UI intentionally keeps a stable two-window view: **5-HOUR** and **WEEKLY**. GProxy quota-cycle data can be configured during onboarding and is used when available; missing data is shown as unavailable rather than replaced with fake percentages.

## Windows

Use the latest release installer from the GitHub Releases page. The Windows app is a WebView2 desktop shell around the same local Spatial OS served at:

```text
http://localhost:7749
```

The installer contains a single `Apocalypse.exe`. First-run configuration happens in that window; there is no separate console setup executable.

## Source install

From `skills_apocalypse/`:

```bash
bash install.sh
apocalypse-ui
```

Useful launcher commands:

```bash
apocalypse-ui status
apocalypse-ui stop
apocalypse-ui restart
apocalypse-ui init      # opens the web reinitialize flow
apocalypse-ui analyze   # refresh cached agenda/worklog analysis
```

## Runtime layout

```text
skills_apocalypse/
├── spatial_os.html / .css / .js
├── onboarding_ui.js
├── spatial_server.py
├── spatial_server_plus.py
├── server.py                  # transcript/session API core
├── agent_discovery.py
├── onboarding.py
├── analysis_harness.py
├── workspace_init.py
├── quota_adapters.py
├── ops_analysis.py
├── app_lifecycle.py
├── desktop_app.py
├── apocalypse_ui.py
├── hooks/
└── tests/
```

Claude hooks are optional realtime enrichment. Apocalypse itself is designed to remain agent-independent.
