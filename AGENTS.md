# 给在这个仓库里工作的 Agent

先读本文件，再读 [README.md](README.md)。README 是产品设计理念和架构的最新说明。本文件只规定怎么干活、什么进 GitHub、什么留在本机。

如果改了目录结构、入口、产品模块或用户可见的行为，同一轮改动里更新 README 的对应章节。不要只改代码。

## 在哪工作

标准仓库是 `E:\BaiduSyncdisk\ClaudeCode_Workspace\apocalypse`，分支 `main`。

Orca 可能会另外开一个 git worktree。那是同一仓库的另一个工作目录，不是另一份项目。未提交的文件不会因为 merge 自动回到主目录。日常开发和提交以主目录为准。

## 代码怎么放

```text
skills_apocalypse/
├── frontend/    Spatial OS 的 html / css / js
├── backend/     本地服务、配额、分析 harness、启动逻辑
├── pipeline/    聊天记录清洗 → 分段 → Knowledge IR
├── data/        跑出来的数据，不进 Git
├── hooks/
├── tests/
├── apocalypse-ui
└── install.sh
```

- 产品界面和产品服务分开放。不要把新的前端文件再丢回 `backend/`。
- 知识流水线只改 `pipeline/` 里现用的脚本。`archive/superseded/` 里是旧版本，不要接回主流程。
- 流水线调用分析模型时，从 `backend/analysis_harness.py` 走。不要在流水线里另写一套模型客户端。
- 清洗阶段判断「这句话有没有意义」用 Jev（TypeSafe）。分段和 Knowledge IR 用 harness 里配置的分析模型。

## 什么进 GitHub

进仓库的是产品本身，换一台机器克隆下来仍能安装、阅读和继续开发的那些文件：

- `frontend/`、`backend/`、`pipeline/` 的脚本和提示词
- `tests/`、`hooks/`、`install.sh`、`apocalypse-ui`
- `README.md`、`AGENTS.md`、`.github/workflows/`
- `.gitignore`

不进 GitHub 的是某一台电脑的状态、密钥和跑批结果：

- `skills_apocalypse/data/`：清洗结果、分段对话、Knowledge IR、聊天记录导出、旧跑批
- `archive/`：本机归档和探测脚本
- `vendor/`：本机放下的第三方说明
- `~/.claude/apocalypse/` 整棵目录
- 任何 API key、token、OAuth refresh token、密码、cookie

`.gitignore` 已经挡住 `data/`、`archive/`、`vendor/`。不要为了「方便别人复现」把这些目录加回提交。别人要复现，应在自己机器上重新配置和重新跑，而不是拿走这台机器的数据。

## 什么是本机特有的

下面这些能力的**代码**在仓库里，**这台机器是否启用、用哪家、密钥是什么**只存在本地。换一台电脑不会自动拥有同样的接线。

本地配置目录：

```text
~/.claude/apocalypse/
├── harness.json          分析模型和 Jev 的入口，不含密钥本身
├── secrets.json          密钥和 OAuth token
├── quota_sources.json    用量源是否启用，以及用量源自己的凭据引用
├── setup.json            这次初始化选了哪些 agent 和哪个分析模型
├── workspace.json        空间记忆数据
└── events.jsonl          本机活动日志
```

这台机器当前的接线：

- 分析模型是 MiniMax（`MiniMax-M3`，OpenAI Chat 兼容接口）。密钥字段是 `secrets.json` 的 `analysis_api_key`。`harness.json` 只记录 provider、模型和密钥放在哪个字段。
- 流水线默认用简体中文写结果。要用别的语言，设环境变量 `APOCALYPSE_OUTPUT_LANGUAGE`，不要改代码里的默认值来迁就一次任务。
- 对话清洗的意义判断用 Jev，`harness.json` 的 `typesafe` 段，密钥字段是 `typesafe_api_key`。也可以用环境变量 `TYPESAFE_API_KEY`。
- 飞书日历和任务走用户 OAuth。应用凭据和用户 token 都在 `secrets.json`（`feishu_app_id`、`feishu_app_secret`、`feishu_user_*`）。没有这些字段时，议程同步应显示未授权，不要伪造日历数据。
- 用量面板这台机器启用了 OpenAI、Grok、Volc Agent、MiniMax Intl。Claude Code 被发现了，但没有放进用量源。GProxy 一类用量源的账号密码只放在 `quota_sources.json` / `secrets.json`。

写代码时不要把上述选择写成全仓库的默认值。README 里的「支持哪些厂商」是产品能力；`harness.json` 和 `setup.json` 里的选择是这台机器的选择。

## 改完要检查什么

- 改了目录、启动方式、产品模块：更新 README 的 Runtime layout 或对应章节。
- 改了安装进 `~/.claude/skills/apocalypse` 的文件清单：更新 `install.sh`。
- 改了前端路径或桌面打包入口：同时看 `.github/workflows/windows-desktop-build.yml` 和 `spatial-os-check.yml`。
- 不要把 `secrets.json`、`harness.json`、`workspace.json` 或 `data/` 里的文件贴进提交说明或代码注释。
