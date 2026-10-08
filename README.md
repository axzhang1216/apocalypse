# Apocalypse Spatial OS

**Your agents. One memory.**

Agents working in this repo should read [AGENTS.md](AGENTS.md) before changing layout, data, or local configuration.

Apocalypse is a local workspace for understanding and operating AI-agent work across projects. It combines a spatial project memory, live/recent sessions, activity history, Plan usage, agenda context, and an Apocalypse-owned analysis harness in one interface.

## Current product

Apocalypse now has one UI: **Spatial OS**.

- **SPACE** — projects, sessions, conversations and discussion/decision memory in one semantic universe.
- **OPS** — activity, LLM Plan usage, agenda, live conversations and active agents.
- **Session tools** — open/export/resume Claude sessions, plus safe `CLEAN NON-TEXT` and `REPAIR JSONL` maintenance.
- **Analysis harness** — Apocalypse chooses and calls its own analysis model rather than depending on one specific agent.
- **Settings** — UI scale, in-app reinitialize, and Windows self-update.

The old dashboard, Three.js nebula workspace and `apocalypse.py` TUI are no longer part of the product.

## Feishu agenda sync

Feishu sync is **optional and manual** — Apocalypse never contacts the
Feishu API at startup. AGENDA renders the last synced snapshot (or a local
placeholder) and the ↻ SYNC button runs the network sync on demand.

If `~/.claude/apocalypse/secrets.json` contains `feishu_app_id` and
`feishu_app_secret` (or `FEISHU_APP_ID` / `FEISHU_APP_SECRET` are set), OPS
can sync Feishu calendars and tasks into AGENDA.

Personal calendar/task data requires **user OAuth**. Click `FEISHU LOGIN`
(or `SYNC` when unauthorized) and complete the Feishu consent page. Register
the redirect URI `http://localhost:7749/api/feishu/oauth/callback` in the
Feishu app security settings, and authorize user scopes such as
`calendar:calendar:read` / `calendar:calendar` and `task:task:read`.

After authorization, click ↻ SYNC to fetch events from all calendars
available to the user (not only the primary calendar) and open Task v2
tasks. The result is cached locally (`~/.claude/apocalypse/feishu_sync_state.json`)
so AGENDA keeps showing the last-known events. Opening a day in ACTIVITY
also merges that day's Feishu events into the day log. Tokens are stored
locally in `secrets.json` and refresh automatically.

A failed sync never blocks the app: it only marks the AGENDA zone with a
`FEISHU NOT SYNCED` / `FEISHU SYNC FAILED` banner (and the badge shows the
reason on hover), so you can retry with ↻ SYNC. Server-side write endpoints
remain available at `POST /api/feishu/event` and `POST /api/feishu/task`.

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
├── stream_state.json
├── conversations/
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

## Live conversation watcher

`backend/stream_watcher.py` tails every agent's chat logs (Claude, Codex, pi, OpenClaw, Grok JSONL plus the Hermes SQLite store) about every 5 seconds, normalizes new lines and attaches them to per-session conversation records in real time:

- Every message is judged by Jev (TypeSafe) — meaningfulness for all roles, new-topic detection for user messages — with the analysis model as fallback; noise (tool output, pings, IDE context) is dropped before judging.
- A conversation closes when a user message starts a new topic; the last assistant reply of an episode is tagged as the conclusion.
- Conversations are stored under `~/.claude/apocalypse/conversations/` as one JSONL per session, in the same record format the batch pipeline produces.
- OPS lists conversations — click one to see the user questions and the assistant conclusion replies; the star map shows the most recent conversations under each project.

History on first start: sessions already covered by the batch pipeline are adopted from its output (`skills_apocalypse/data/conversations_output` when running from the repo, or the directory in `APOCALYPSE_BATCH_CONVERSATIONS_DIR`). Adopted files are copied into the live directory, which is then authoritative; delete a live file to re-adopt from the batch output.

Environment variables (all optional): `APOCALYPSE_STREAM_POLL_SECONDS` (default 5), `APOCALYPSE_JEV_TIMEOUT` (default 20), `APOCALYPSE_BATCH_CONVERSATIONS_DIR`, `APOCALYPSE_OUTPUT_LANGUAGE` (conversation titles default to 简体中文).

One watcher per machine: both the desktop app and the dev server start the watcher, but a lock file (`stream_watcher.lock`) guarantees only the first one actually watches — two watchers would fight over the same live files.

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
├── frontend/          # Spatial OS html / css / js
├── backend/           # server, Spatial OS runtime, quotas, harness
├── pipeline/          # clean → segment → knowledge IR (latest scripts only)
├── data/              # cleaned sessions, conversations, knowledge IR, chat logs
├── hooks/
├── tests/
├── apocalypse-ui      # launcher
└── install.sh
```

Claude hooks are optional realtime enrichment. Apocalypse itself is designed to remain agent-independent.
