# AGENTS.md — MCP DevBridge v0.8.6 维护指南

本文件是仓库内 AI/Agent 与工程师的快速入口。当前维护线是 `release/v0.8.6`；v0.9+ 历史保留在远端，不得为了“补功能”把多 Agent runtime 重新混回本维护线。

## 1. 开工阅读顺序

1. `AGENTS.md`：当前硬约束与开发入口。
2. `项目架构.md`：v0.8.6 真实运行架构、路由、安全和平台边界。
3. `开发计划.md`：当前维护目标与发布门。
4. `进度验收.md`：本轮实际验证结果与发布状态。
5. `docs/en/LONG_RUNNING_TASKS.md`：数小时任务的 durable plan/checkpoint/review/rework 契约。
6. `docs/en/` 其它文件：公开英文架构、兼容、安全、开发与变更记录。

若文档与代码冲突，以**当前代码 + 可复现测试**为准，并在同一变更中修正文档。

## 2. 当前产品语义

MCP DevBridge 是 PySide6 桌面应用，通过 CodexPro、可选 Windows-MCP 与 Hub Gateway，把本地开发目录提供给 ChatGPT / Gemini 等 MCP 客户端。Windows 是主要桌面平台；v0.8.6 同时恢复 Linux / SteamOS Desktop Mode 的用户目录安装、运行、升级与构建链。

### 多根路由是硬约束

- 项目列表中所有**运行中（READY）**的根目录同时处于 active 状态，地位平等。
- 启动 `C:\` 后，`C:\Program Files (x86)\...`、任意 Git/非 Git 子目录均继承该根的可访问边界；启动 `D:\` 同理。无需为每个子目录单独注册项目。
- 不存在“入口项目 / 当前项目决定权限 / 某项目负责 Hub bootstrap”语义。共享 Hub 独立于所有项目根；所有 READY 项目通过 ProjectManager 并列注册到 Gateway。
- 绝对路径按**最具体的运行根**匹配；例如同时运行 `D:\` 与 `D:\Environment\mcp` 时，后者负责其后代路径。
- 相对路径只有在能唯一定位到一个运行根时才自动路由；多个根存在同名路径时必须报歧义，要求绝对路径，禁止猜测。
- `task_id` 绑定产生任务的运行根；CodexPro `workspace_id=ws_...` 仅作为没有更强证据时的 follow-up affinity。
- 路由优先级：显式 DevBridge override（兼容） > task affinity > 本次 path/cwd/patch/command 证据 > opaque workspace handle > 无状态稳定 fallback。自动 fallback 不写入 session current-workspace。
- `devbridge_switch_workspace` / `devbridge_workspace_id` 只保留兼容、诊断与显式覆盖，不是正常文件/Git/Shell 工作流的前置步骤。
- 一次工具调用若明确跨越两个 active root，应拆分调用或显式指定目标；不得静默选择其中一个。

### 路径安全也是硬约束

- containment 使用 canonical/real path 语义，不能只比较字符串前缀。
- `..`、junction、symlink 不能把文件操作或本地工具 `cwd` 带出目标运行根。
- 根盘扫描遇到 `EACCES/EPERM` 等不可读目录应记录 warning 并继续，而不是让整次 tree/inventory/inspect 失败。
- Git 工具在给定路径向上发现最近 Git 仓库，因此磁盘根项目下的嵌套仓库可直接使用。

### 长任务执行纪律也是硬约束

- 多阶段任务或预计超过约 2 分钟的工作，在有写权限时先调用 `long_run_start`，把 objective、steps 与 acceptance criteria 持久化。
- 长命令只通过后台 `bash` 执行，并用 `long_run_id` / `long_run_step_id` 绑定；禁止把 build/test/install/upload 放进同步 `run_command` / `run_program` 等待数分钟。
- 每个阶段完成后用 `long_run_update` 写 checkpoint/证据；步骤不能在无证据时标记 `done`。
- 实现完成后必须 `long_run_review`。FAIL 必须形成可执行返工并重新审查；任何 review 后的工作变化都会让旧 PASS 失效。
- `long_run_complete` 是最终 return 门：步骤、证据、最新 PASS revision 与后台任务状态全部通过前，不得向用户声称“一条龙已完成”。
- 浏览器刷新、Connector 重连、上下文压缩后，以 `.ai-bridge/long-runs/<run_id>.json` 为事实源，通过 `long_run_list/status` 恢复；不得靠聊天记忆猜进度。
- MCP/CodexPro 重启后若旧 `task_id` 变成 unknown，必须提供明确终态证据后再 resolve；不得把“找不到任务”当成成功。
- 原生 MCP `io.modelcontextprotocol/tasks` 未来按 capability negotiation 接入；当前普通 tool-level durable fallback 不能因为宿主暂不支持 extension 而失效。
- 目标仍可执行且无需真实用户输入/授权时，`wait_task` 返回 running 不能成为结束 assistant turn 的理由；应继续自动轮询/推进，并在工具密集阶段约每 45–60 秒给用户一条简短进展说明。
- v0.8.6 的 `wait_task` 无 progress token 时单次最多 30 秒；有标准 MCP progress token 时可到 120 秒并约每 8 秒发 progress notification。SSE keepalive、EventStore replay 与 durable long-run 只能降低/恢复链路中断，不能声称能绕过 ChatGPT 宿主自身的 hard turn/message-delivery timeout。

## 3. Hub、设备与连接方式

公网模式链路：

```text
ChatGPT / Gemini
        │ HTTPS /mcp
        ▼
Cloudflare / ngrok / Quick Tunnel
        ▼
OAuth/Bearer Gateway (loopback)
        ├── active root A → CodexPro A
        ├── active root B → CodexPro B
        └── optional remote device → remote DevBridge Hub
```

- Gateway/Tunnel 是共享 Hub 连接，不属于某个“入口项目”。
- 单独停止一个根不得影响其它 READY 根；最后一个运行根停止后才关闭共享 Hub/Tunnel。
- 多设备选择与本机多根选择是两层概念。多台设备在线时可显式选择设备；目标设备内部仍按自己的 active roots 自动路由。
- `Local` 模式同样经过共享 loopback Gateway，只是不建立公网 Tunnel；一个 Local MCP 地址也能在所有 READY 根之间自动路由。

## 4. 安全与密钥

- 引擎、Gateway、legacy backend 默认只绑定 loopback；公网只能经明确配置的 tunnel。
- 公开 URL 与凭据分离。禁止把 Bearer、OAuth secret、Cloudflare token 拼进 URL、日志或仓库。
- Windows：优先 Windows Credential Manager，兼容 DPAPI fallback。
- Linux/SteamOS：优先桌面 secret service；fallback 为用户配置目录内 AES-GCM 加密存储，密钥/密文文件限制为用户可读写。
- 新项目桌面默认仍为 `system + full_system`（完全访问，危险），首次实际启用需一次风险确认；用户可降为 workspace/developer 或 read-only。
- `system/full_system` 允许系统级工作，但已知删盘/格式化/引导修改等危险命令拦截继续生效。
- 审计日志中的 command/content/patch 与 secret-like 字段必须脱敏。
- 任何新的路径路由不得削弱 PathGuard/permission checks。

## 5. 关键源码

| 路径 | 责任 |
|---|---|
| `src/local_dev_mcp_bridge/desktop_main.py` | 桌面 UI、项目列表、全部项目启停、配置与升级接力 |
| `project_manager.py` | 项目 catalog、每根独立 `ProjectUnit`、端口和生命周期 |
| `app_state.py` | 共享 Hub 的 Tunnel/Gateway 编排；不得持有项目 Codex/Windows 引擎 |
| `gateway.py` | OAuth/Bearer、Hub MCP 代理、多根/任务/设备自动路由 |
| `engines.py` | CodexPro / Windows-MCP 进程管理与私有运行时解析 |
| `platform_support.py` | Windows/Linux 平台差异、XDG/桌面路径、进程参数 |
| `secrets.py` | Windows Credential/DPAPI 与 Linux secret-service/AES-GCM |
| `update_manager.py` | GitHub Release 检查、资产选择、更新接力 |
| `third_party/codexpro/src/longRunOps.ts` | durable 长任务 plan/checkpoint/evidence/review/rework/completion 状态机 |
| `third_party/codexpro/` | 项目文件/Git/Shell/任务/分析主引擎 fork |
| `scripts/build.ps1` | Windows 测试、构建、PyInstaller、Inno Setup |
| `scripts/build_linux.sh` | Linux 测试、CodexPro smoke、PyInstaller、tar.gz |
| `scripts/install_linux.sh` | Linux/SteamOS 用户目录安装与 desktop/autostart |
| `.github/workflows/` | Windows + Ubuntu 22.04 CI / release 构建 |

## 6. 开发与验证

### Windows

```powershell
uv venv --python 3.12
uv pip install -p .venv -e ".[dev,package]"
cd third_party/codexpro
npm ci
npm run build
cd ../..
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m pyright --pythonpath .venv\Scripts\python.exe src tests
.venv\Scripts\python.exe -m pytest tests -q --disable-warnings
cd third_party/codexpro
npm run smoke
```

发布构建：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1 -Version 0.8.6
```

### Linux / SteamOS build host

```bash
uv venv --python 3.12
uv pip install -p .venv -e '.[dev,package]'
cd third_party/codexpro && npm ci && npm run build && cd ../..
bash scripts/build_linux.sh 0.8.6
```

Linux release 以 Ubuntu 22.04 构建以保持较旧 glibc 基线；SteamOS Desktop Mode 真机验证是额外兼容证据，不得用“未真机”掩盖 CI/构建失败。

## 7. 发布门

发布 v0.8.6 前至少必须满足：

- `git diff --check` 通过，工作树只包含有意变更；生成物、缓存、安装运行文件不得 track。
- Python Ruff、Pyright、全量 pytest 通过。
- CodexPro TypeScript build、完整 smoke 通过；必须包含 durable long-run persistence/restart/evidence/stale-review/rework/running-task gates，以及根盘不可读目录与 nested Git 回归。
- `uv lock --check` 通过；PowerShell 与 Bash 发布脚本语法检查通过。
- Windows `build.ps1 -Version 0.8.6` 成功，安装器可选安装目录且 frozen payload 完整。
- GitHub Release workflow 同时产出 Windows installer 与 `MCPDevBridge-Linux-x86_64-0.8.6.tar.gz`。
- commit、branch push、`v0.8.6` tag、GitHub Release 资产必须指向同一源提交。
- v0.9.x tags/branches 是历史，不删除、不改写、不 force-push。

最终实测结果只写入 `进度验收.md`；禁止复制旧版本的测试数量、SHA-256 或“已发布”状态冒充本轮证据。
