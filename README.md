# MCP DevBridge

> 让 ChatGPT、Gemini 等支持 MCP 的客户端安全连接本机开发目录，并在多个运行根之间按路径自动路由。

MCP DevBridge 是一款 PySide6 桌面桥接工具。Windows 10/11 是主要发行平台；v0.8.4 同时提供 Linux / SteamOS Desktop Mode 的构建、用户目录安装和升级链。

它**不提供模型推理，也不调用 OpenAI / Gemini 模型 API**。客户端是否允许写入、是否需要额外确认，以及相关额度/套餐限制，仍由对应平台决定。

## v0.8.4 的核心变化

### 数小时长任务：持久化 plan → 执行 → 审查 → 返工 → PASS → return

v0.8.4 为 ChatGPT/Codex/Gemini 等网页端增加了**耐久长任务编排**。对于多阶段或预计超过约 2 分钟的任务，不再依赖某一次 MCP `tools/call` 长时间保持连接，也不再只依赖模型当前聊天上下文记住“做到哪一步”。

CodexPro 新增：

- `long_run_start`：先把目标、步骤和每步验收标准持久化到 `.ai-bridge/long-runs/<run_id>.json`。
- `long_run_update`：记录进度、证据、返工和后台 `task_id`；步骤首次标记 `done` 必须有证据。
- `long_run_review`：按当前 work revision 做 PASS/FAIL 审查；FAIL 必须给出可执行返工项并重新打开失败步骤。
- `long_run_complete`：只有所有步骤完成、最新 PASS 审查覆盖当前 revision、且没有仍在运行/未知未解释的后台任务时才允许完成。
- `long_run_status` / `long_run_list`：网页刷新、Connector 重连、上下文压缩或 CodexPro 进程重启后，可以依靠 durable `run_id` 恢复，不需要猜测之前聊天里做到哪里。
- `bash` 新增 `long_run_id` / `long_run_step_id` 绑定；后台任务本身仍**没有固定执行时长上限**，而 `wait_task` 只做最多 30 秒的短轮询，不会因为轮询返回而终止任务。

模型侧指令也同步收紧：长任务应先建 durable plan，分阶段 checkpoint，审查失败则返工；**在 `long_run_complete` 成功前不得向用户返回“已全部完成”**。完整设计、故障恢复和与 MCP Tasks / mcp-agent / LangGraph 的取舍见 `docs/en/LONG_RUNNING_TASKS.md`。

### 所有运行根同时 active

项目列表不再有“入口项目”或“当前工作区决定权限”的概念。只要一个项目根处于运行中（READY），它和其它运行根就同时参与 Hub 路由。

例如同时启动：

```text
C:\
D:\
```

那么：

```text
C:\Program Files (x86)\...
C:\Users\...\project
D:\Environment\mcp
D:\meme
```

都可以直接作为目标路径，无论它们是不是 Git 仓库，也不需要把每个子目录单独加入项目列表。

路由规则：

1. 绝对路径优先；若多个运行根有包含关系，选择最具体的根。
2. 相对路径只有在能唯一定位到一个运行根时才自动选择。
3. 多个根存在同名相对路径时返回歧义错误，要求改用绝对路径，不猜测。
4. `bash` 任务产生的 `task_id` 会绑定原运行根，后续 `wait_task/get_task/cancel_task` 自动回到同一根。
5. CodexPro `workspace_id=ws_...` 仅作为没有路径/任务证据时的兼容 affinity。
6. `devbridge_switch_workspace` / `devbridge_workspace_id` 仍保留兼容和显式覆盖能力，但不是正常开发工作流的前置步骤。

路径安全没有因为自动路由而放松：`..`、symlink/junction 逃逸和本地工具越界 `cwd` 都会被拒绝；根盘扫描遇到无权限目录会跳过并返回 warning，而不是让整次扫描失败。

Hub 本身只有一个全局 Gateway 端口和一个客户端访问 Bearer；每个项目只保留自己的内部 CodexPro / Windows-MCP 端口与上游凭据。旧配置中的 per-project gateway_port 会被兼容读取后忽略，不再形成入口端口。

### 多根 Hub 生命周期

Gateway/Tunnel 是独立于项目的共享 Hub 连接。Hub 启停不再绑定任何项目根、项目端口或项目 Bearer；项目只负责自己的 CodexPro/Windows 内部引擎。

- 停止一个运行根：只停止该根的 ProjectUnit，其它运行根和 Hub 保持可用。
- 停止最后一个运行根：再关闭共享 Gateway/Tunnel。
- “启动所有项目 / 停止所有项目”用于批量生命周期控制。
- 项目表只保留：名称 / 路径 / 状态 / 端口 / 操作。

> `Local` 模式同样经过共享 loopback Gateway，只是不建立公网 Tunnel。无论 Local 还是公网模式，一个客户端地址都可以在所有 READY 根之间按 path/cwd/task 自动路由，不需要先选入口项目。

## 架构概览

```text
ChatGPT / Gemini
        │ MCP over HTTPS
        ▼
Cloudflare / ngrok / Quick Tunnel
        ▼
OAuth/Bearer Gateway (loopback)
        │
        ├── active root C:\  → CodexPro
        ├── active root D:\  → CodexPro
        ├── active nested root → CodexPro
        └── optional remote device → remote DevBridge Hub
                                      │
                                      └── its own active roots
```

CodexPro 提供文件、Git、Shell、异步任务、代码分析等开发工具。Windows-MCP 是可选系统控制桥；注册表、环境配置等系统类命令在允许的权限模式下不需要先“切换项目”。

## 安装

### Windows

从 GitHub Release 下载：

```text
MCPDevBridge-Setup-0.8.4.exe
```

安装器为 per-user 安装，不要求管理员权限。v0.8.4 显式保留安装目录选择页，用户可以安装到默认目录，也可以选择其它目录。

正式包自带固定版本的 Node.js、uv/uvx 和 cloudflared 私有运行时，不要求把这些工具安装到系统 PATH。可选 Windows-MCP 仍由内置 uvx 按锁定版本首次获取。

首次使用：

1. 打开 MCP DevBridge。
2. 添加希望授权的根目录；如果希望整盘下所有子路径都可访问，可以直接添加 `C:\`、`D:\` 等盘根。
3. 按需要选择权限模式和连接方式。
4. 启动一个或多个项目，或点击“启动所有项目”。
5. 将桌面显示的 MCP URL / 授权信息配置到 ChatGPT、Gemini 等客户端。
6. 后续请求直接在提示词或工具参数中使用目标绝对路径，无需先切换 workspace。

### Linux / SteamOS Desktop Mode

GitHub Release 提供：

```text
MCPDevBridge-Linux-x86_64-0.8.4.tar.gz
```

解压后执行：

```bash
tar -xzf MCPDevBridge-Linux-x86_64-0.8.4.tar.gz
cd MCPDevBridge
./install.sh
```

默认安装到：

```text
~/.local/opt/MCPDevBridge
```

也可以显式选择用户可写目录：

```bash
./install.sh --target-dir "$HOME/Applications/MCPDevBridge"
```

安装器会拒绝 `/`、`$HOME`、`$HOME/.local` 等危险目标，也不会覆盖一个看起来不是 MCP DevBridge 的非空目录。Desktop Entry / autostart 遵循有效的绝对 `XDG_DATA_HOME` / `XDG_CONFIG_HOME`；相对 XDG 值按规范视为无效并回退到用户默认目录。

SteamOS 建议在 Desktop Mode 使用用户目录安装，不改写只读系统基座。

## 连接方式

| 方式 | 说明 | 适合 |
|---|---|---|
| Cloudflare Named Tunnel | 固定域名，公网长期地址 | 主 Hub / 长期使用 |
| ngrok 固定域名 | 固定 ngrok hostname | 已有 ngrok reserved domain |
| Quick Tunnel | 随机 `trycloudflare.com`，重建会变化 | 临时测试 / 远端设备回传 |
| Local | 仅 loopback，共享 Gateway，多根自动路由 | 本机开发 / 无公网暴露 |

所有公网模式都终止在 Gateway；CodexPro、Gateway、Windows 桥本身只监听 loopback。

## 多设备

一个主 Hub 可以登记其它 MCP DevBridge 设备。设备切换和本机工作区路由是两层独立逻辑：

```text
客户端
  → 选择/自动确定设备
  → 目标设备内部按 path/cwd/task 自动确定 active root
  → 执行工具
```

远端 Quick Tunnel 地址变化可通过心跳更新到主 Hub，而 ChatGPT 仍只需要配置主 Hub 的固定地址。

## 权限与安全

桌面新项目默认仍是“完全访问（危险）”语义，对应 `system + full_system`，首次实际启用时必须完成一次风险确认。用户可主动降为项目工作区或只读模式。

主要安全边界：

- 路径 containment 使用 canonical/real path，防止 `..` 和 symlink/junction 越界。
- 本地 `run_command/run_program` 的 `cwd` 必须位于目标运行根内。
- 公网请求必须通过 Gateway 的 OAuth/Bearer 认证；loopback 匿名仅限本机兼容路径。
- command/content/patch 与 secret-like 字段写审计日志前会脱敏。
- 已知格式化磁盘、破坏引导、递归删除系统盘等高风险命令仍受硬拦截。
- Windows 密钥优先使用 Credential Manager，兼容 DPAPI fallback。
- Linux/SteamOS 优先桌面 secret service；无服务时使用用户配置目录中的 AES-GCM 加密 fallback，并限制文件权限。

详细模型见 `docs/en/SECURITY.md` 与 `项目架构.md`。

## 开发

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

完整 Windows 发布构建：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1 -Version 0.8.4
```

产物：

```text
release/MCPDevBridge-Setup-0.8.4.exe
```

### Linux build host

```bash
uv venv --python 3.12
uv pip install -p .venv -e '.[dev,package]'
cd third_party/codexpro && npm ci && npm run build && cd ../..
bash scripts/build_linux.sh 0.8.4
```

产物：

```text
release/MCPDevBridge-Linux-x86_64-0.8.4.tar.gz
```

CI 的 Linux 构建基线为 Ubuntu 22.04，以避免在过新的 glibc 环境构建后失去对较旧发行环境的兼容性。

## 发布与更新

桌面启动后检查 GitHub Release，之后按固定周期检查更新。Windows 使用内置 `live_upgrade.ps1` 做 detached 升级接力；Linux 使用随包提供的 `live_upgrade.sh`。升级接力文件只保存非敏感恢复元数据，真正凭据在新进程启动后从 SecretsStore 重新读取。

当前维护版本：**0.8.4**。`v0.9.x` 远端 tag/历史仍保留，但不属于本维护线，也不会被 v0.8.4 发布覆盖或 force-push。

## 文档

- `AGENTS.md`：开发/Agent 快速约束。
- `项目架构.md`：当前架构与数据流。
- `开发计划.md`：v0.8.4 维护目标和发布门。
- `进度验收.md`：当前发布的真实测试/构建/Release 证据。
- `docs/en/ARCHITECTURE.md`：English architecture.
- `docs/en/COMPATIBILITY.md`：Windows / Linux / SteamOS compatibility.
- `docs/en/SECURITY.md`：security model.
- `docs/en/DEVELOPMENT.md`：build and verification commands.
- `docs/en/CHANGELOG.md`：release history.

## License

项目许可证见 `LICENSE`；第三方组件及其许可证见 `THIRD_PARTY_LICENSES.md`。
