# AGENTS.md — 项目引导

本项目为多角色开发。请 AI（各类 agent，包括 opencode/Claude/Copilot）以及参与开发的工程师阅读本文件。

## 一、先读什么（上下文受限时的速览路径）

当上下文被压缩后，按以下顺序阅读以快速恢复项目知识：

1. **AGENTS.md（本文件）** — 快速定位与约定
2. **项目架构.md** — 代码结构、模块职责、数据流、关键实现细节
3. **开发计划.md** — 完整开发计划 + 每个 Phase 的验收标准
4. **进度验收.md** — 当前开发进度、已完成 Phase、遗留事项（写操作前必须读）

> 若本文件与上述任一文档冲突，以 项目架构.md 为准并同步修 AGENTS.md。

## 二、项目一句话

**MCP DevBridge**（原 LocalDev MCP Bridge）：一款 Windows 桌面程序（PySide6 单窗口），让用户在桌面选定一个本地开发项目，
一键启动独立的 MCP Server 子进程（仅监听 127.0.0.1），并通过 **Cloudflare Named Tunnel** 提供一个
**长期固定、永不变化的 HTTPS MCP 地址**，供 ChatGPT / Gemini 等支持自定义 MCP 的客户端接入。

## 三、开发约定（必须遵守）

- **不修改外部项目**：禁止修改 `D:\AStockMultiAgent` 及其它非本项目的目录（测试一律写在 `.test-workspace`）。
- **不删除原型与旧的 `.venv`**：`D:\Environment\mcp\backups-prototype\dev-mcp.py` 为原型备份；`D:\Environment\mcp\.venv` 保留。
- **合并/文档优先**：任何“并发/压缩上下文”场景，先写 进度验收.md 再继续开发。
- **认证与 URL 分离**：公开 MCP URL 必须固定（Named Tunnel），令牌（Bearer）由用户在客户端单独配置，绝不允许把随机密钥拼进 URL。
- **本机匿名放行**：仅允许 127.0.0.1 / ::1 / localhost 的本地连接匿名访问 `/mcp`；`/control/*` 一律要求 Bearer。
- **公网必须 Bearer**：经 Cloudflare 转发（Host 为公网域名）的请求必须携带有效 Bearer；
  默认 MCP SDK 2.0.0 的 `streamable_http_app()` 会自动加 DNS rebinding 防护，只放行 localhost，
  公网隧道接入时需显式禁用/调整 rebinding（见 项目架构.md「安全模型」）。
- **桌面默认完全访问**：桌面新项目默认 `system + full_system`（“完全访问（危险）”）；不做逐次写操作确认，但第一次实际启动完全访问模式仍需要一次性风险确认。后端/CLI 的兼容默认值不等同于桌面产品默认值。
- **不泄漏密钥**：日志必须脱敏（文件名/参数含 KEY/TOKEN/SECRET/PASSWORD/COOKIE/AUTH 时整值遮罩）。

## 四、环境与依赖（复现命令）

```bash
# 进入项目
cd D:\Environment\mcp\mcp-devBridge
# 创建 venv（Windows）并安装开发依赖
python -m venv .venv
.venv\Scripts\activate
uv pip install -e ".[dev,package]"

# 测试（必须在项目根执行，保证导入路径）
PYTHONIOENCODING=utf-8 .venv\Scripts\python.exe -m pytest tests/ -q

# 运行后端命令入口
.venv\Scripts\python.exe -m local_dev_mcp_bridge.standalone_server      # 单一入口（CLI 简化）
.venv\Scripts\python.exe -m local_dev_mcp_bridge.server_main --config ...  # 子进程后端入口（正式路径）
```

已锁定：Python 3.12.10、mcp==2.0.0、pydantic 2.13.4、starlette 1.4.1、uvicorn 0.52.1、PySide6 6.11.1、pytest 9.1.1、pywin32 312。
```
Windows-MCP 版本锁定在 `engines.py` 的 `WINDOWS_MCP_PINNED_VERSION`（当前 `0.8.2`，
`uvx --from windows-mcp==0.8.2 windows-mcp serve`）；升级必须先实测兼容再人工改常量。
（其余依赖见 `pyproject.toml`）

## 五、目录结构

```
mcp-devBridge/
├── pyproject.toml            # 包定义、脚本入口、ruff/pyright/pytest 配置
├── README.md                 # 一句话 + 文档索引
├── AGENTS.md                 # ← 本文件
├── 项目架构.md               # 架构说明
├── 开发计划.md               # 计划 + 验收
├── 进度验收.md               # 进度
├── src/
│   └── local_dev_mcp_bridge/
│       ├── __init__.py         # __version__、APP_NAME、APP_IDENT
│       ├── constants.py        # 目录/默认值/错误码常量；配置目录 default %LOCALAPPDATA%\LocalDevMCPBridge（`LOCALDEV_MCP_CONFIG_DIR` 可覆盖）
│       ├── models.py           # ProjectConfig / AppConfig / RuntimeConfig / TunnelState（pydantic）
│       ├── config_store.py     # 读/写这些 JSON；功能探测；命令建议
│       ├── secrets.py          # Bearer 令牌（Win CredManager + DPAPI 回退文件 secrets.dpapi.json）
│       ├── audit.py            # 审计日志 + 密钥脱敏
│       ├── agent_pool.py       # v0.9 本地并发 Agent 池：OpenCode/Claude Code + worktree + bounded queue
│       ├── shell.py            # PowerShell 命令执行、进程树终止、环境探测
│       ├── processes.py        # 受管进程注册（dev server 等）
│       ├── permissions.py      # 权限：read_only / workspace / system
│       ├── execution_profile.py # Shell 执行档位：safe / developer（默认）/ full_system + 危险命令拦截
│       ├── project_manager.py   # 多项目：ProjectUnit（每项目引擎对）+ ProjectManager（编目/独立端口/生命周期；enabled 仅兼容旧配置）
│       ├── tools.py            # 37 个 MCP 工具实现（含 list_projects / switch_workspace / shell_info / shell_self_test）
│       ├── server_factory.py   # MCPServer + Starlette app + 认证/审计/限速中间件
│       ├── server_main.py      # 后端 CLI（--config / --port），被桌面进程拉起
│       ├── standalone_server.py# 简化 CLI 单进程入口
│       ├── selftest.py         # 本地 MCP 客户端自测（桌面“测试连接”按钮）
│       ├── engines.py          # 引擎进程管理：CodexProManager / WindowsBridgeManager / 脱敏 / 就绪检测
│       ├── tunnel_manager.py   # 隧道进程：Cloudflare Named / Quick / ngrok / 仅本机；固定 URL 解析
│       ├── oauth_provider.py   # Phase 8 OAuth：LocalOAuthProvider（SDK Provider 实现，单 scope / code / refresh 轮换）
│       ├── gateway.py          # Phase 8 OAuth 网关：uvicorn 8786，OAuth 路由 + 同意页 + /mcp 反向代理
│       ├── app_state.py        # 服务协调状态机 ServiceCoordinator（顺序、URL 固定性、故障清理；无 Qt）
│       ├── backend_manager.py  # 后端子进程管理 /health 轮询（已归档，桌面改走 ServiceCoordinator）
│       └── desktop_main.py     # Phase 3 桌面 UI（PySide6 单窗口，已接线 ServiceCoordinator）
├── tests/                      # pytest 测试（Phase 12 当前 304 项全绿，以实际 pytest 输出为准）
│   ├── conftest.py
│   ├── test_fs.py · test_commands.py · test_git.py · test_config.py
│   ├── test_mcp_integration.py · test_selftest.py
│   ├── test_engines.py · test_tunnel_manager.py · test_app_state.py
│   ├── test_oauth.py           # OAuth 2.1 发现/注册/PKCE/刷新/撤销/网关代理（27 项）
│   ├── test_project_manager.py # 多项目：编目/端口唯一/并行启停/自动恢复/真机双引擎
│   └── test_workspace_switch.py# 会话级 switch_workspace 隔离 + shell_info
├── .test-workspace/            # 测试用临时工作区
└── .tools/                     # cloudflared.exe（2026.7.3）等二进制
```

## 六、常用命令（结果为准）

部分命令必须在 PowerShell 下运行（`$env:PYTHONIOENCODING="utf-8"`），否则 GBK 编码报错。
```
# 运行全部测试
$env:PYTHONIOENCODING="utf-8"; .venv\Scripts\python.exe -m pytest tests/ -q
# 运行单测
$env:PYTHONIOENCODING="utf-8"; .venv\Scripts\python.exe -m pytest tests/test_fs.py -q
# 静态检查
.venv\Scripts\python.exe -m ruff check src tests
```

## 七、开发路线图（详见 开发计划.md）

```text
Phase 0 环境与需求核对          (2026-08-06 完成)
Phase 1 后端核心 + 33 MCP 工具   (2026-08-06 完成)
Phase 2 HTTP + 认证 + 子进程    (2026-08-06 完成)
Phase 3 桌面控制程序（PySide6） (完成 → desktop_main.py 已接线 ServiceCoordinator)
Phase 4 隧道模块                 (代码完成：tunnel_manager.py + 状态机 + transport_security 方案 B；待真机公网验证)
Phase 5 Git 桌面参数配置          (完成：models.git_field_error 校验 + 桌面 Git 区，全量 145 全绿)
Phase 6 日志浏览页 + 自动清理      (完成：三 Tab 页 + 轮转 + 脱敏修复，全量 154 全绿)
Phase 7 打包（PyInstaller onedir + Inno Setup）+ 全文档   (完成：安装器编译+静默安装+启动冒烟通过；GUI 自测闭环待用户)
Phase 8 MCP OAuth（Gemini）      (代码完成：oauth_provider + gateway，test_oauth 27 项，全量 181 全绿；
                                  待用户切换 CF 路由 8787↓8786 + Gemini 真机接入)
Phase 9 多项目并行 + Shell 修复  (2026-08-09 完成：project_manager + 会话级 switch_workspace +
                                  shell_info + 桌面项目表格，全量 258 全绿；真机双项目待用户验收)
```

## 八、当前已知问题（重要）

| 项 | 说明 |
|---|---|
| **端口默认值（已统一）** | 端口配置集中维护在 `constants.py` 的 `DEFAULT_GATEWAY_PORT=8786 / DEFAULT_CODEXPRO_PORT=8787 / DEFAULT_WINDOWS_MCP_PORT=28731 / DEFAULT_LEGACY_BACKEND_PORT=8765`（旧 `local_port`/`2865` 已废弃，`RuntimeConfig.local_port` 为兼容属性）。桌面「访问令牌与 MCP 地址」区修改当前项目 Gateway 端口（含检测/恢复默认/复制 Gateway 地址），「高级设置…」修改当前项目 Codex/Windows/Gateway 端口；服务运行期间锁定。旧配置批量迁移补齐独立端口。 |
| **DNS rebinding 防护** | 已落地（2026-08-08）：`server_factory.build_transport_security(rc.public_hostname)` 方案 B（防护保持开启，allowed_hosts 追加公网域名）；`RuntimeConfig.public_hostname` 经 config/`standalone --public-hostname` 注入；测试 12 项。剩余：真机公网验证（需 Cloudflare 账号）。 |
| **standalone_server / desktop_main** | `standalone_server.py` 已存在但为 CLI；`desktop_main.py` 已实现并接线 `ServiceCoordinator`（引擎/隧道协调，见 app_state.py；backend_manager.py 已归档）。 |
| **MCP 2.0.0 兼容注意** | `streamable_http_app` 返回的是 Starlette app；追加路由用 `app.add_route`，**不要**用 `Mount` 包裹（lifespan 失效）；中间件必须用纯 ASGI（`BaseHTTPMiddleware` 会缓冲 SSE 导致 500）。 |

---

### 九、变更记录（AGENTS 的维护者：每次有重大架构变化更新本节）

- 2026-08-17 · **v0.9.0 双固定域名 / Multi-Device 显式路由 / Agent Pool**：
  - 工作区管理工具正式支持 `device_id`，共享 Hub 可跨 ChatGPT transport 无状态指定远端电脑；Quick heartbeat 继续更新动态回传 URL，但直接绑定 Quick URL 的 App 只作为临时连接。
  - 多人长期使用推荐每台电脑独立 Cloudflare Tunnel UUID/Token/hostname；不同电脑均可绑定各自 `localhost:8786`，严禁两台独立 OAuth Gateway 复用同一 Tunnel replica 身份。
  - 新增 `agent_pool.py` 与 `agent_pool_*` Gateway tools：OpenCode/Claude Code worker、默认 4/硬上限 16 物理并发、单批 64、写任务 Git worktree 隔离、取消/收集/清理与 interrupted 恢复。

- 2026-08-17 · **v0.8.1 多工作区/ChatGPT stateless transport 热修**：
  - 不再把工作区/设备选择仅绑定 `mcp-session-id`；工具 schema 增加可选显式路由提示，兼容 ChatGPT 在连续工具调用间重建 MCP transport 的行为。
  - 修复 Gateway `run_command/run_program` 对 `params.arguments` 的读取；修复 PySide 异步完成 signal 生命周期导致的“项目已 READY、运行记录仍停在正在启动”；盘符根目录补齐显示名。
  - 新增跨 transport session 路由回归，发布前重新跑全量 pytest / Ruff / Pyright / PyInstaller / Inno Setup / 在线升级与 C:\↔D:\ 真机切换。

- 2026-08-17 · **v0.8.0 配对持久化 / 自带运行时 / 更新可靠性**：
  - 同一 `pair_code + device_id` 在首次成功注册后的 1800 秒内可幂等重试；设备目录、Hub 地址和心跳凭据持久化，重启无需再次配对。
  - 更新检查改为启动后一次、之后每 12 小时；安装包内置固定版本 Node.js / uv / uvx / cloudflared，启动诊断检查私有运行时，不依赖用户全局 PATH。
  - `wait_task` 为降低 ChatGPT Web/MCP 长同步调用导致的 message delivery timeout，改为默认 15 秒、单次最多 30 秒；后台命令本身仍无固定执行时长上限。
  - v0.8.0 发布链要求重新跑 pytest、Ruff、Pyright、CodexPro build/smoke、PyInstaller、Inno Setup、冻结版单实例、live_upgrade payload、detached updater Dry Run、安装器 SHA-256、在线升级与固定域名接入验收。

- 2026-08-16 · **v0.7.2 默认异步命令任务 + 跨 session 热修复**：
  - `bash` 无公开 timeout、默认返回 task_id；删除 `start_task`，保留 get/wait/list/cancel。
  - `BashTaskManager` 必须是 CodexPro 进程级共享实例；任务本身仍按 workspace_id 隔离。v0.7.1 的 per-McpServer 实例会导致后续 MCP session 查不到 task_id。
  - 编排等待保护：`wait_task` 单次最多 60 秒；600 秒无任务观察只标记 `orchestrationStale` 并提供恢复提示，不改变/终止任务。
  - 最终验收：跨 session task lookup 回归、600s watchdog、完整 smoke/stress + 304 pytest + Ruff/Pyright 全绿；`v0.7.2` Release、安装器 digest、detached 升级、桌面快捷方式、D:\ 服务恢复、bash→wait_task 跨 MCP 真机调用均通过。

- 2026-08-13 · **v0.7.0 Multi-Device Hub + 新手体验**：
  - 新增 `device_hub.py`、`DeviceConfig/devices.json` 与设备级 SecretsStore 凭据；Gateway 支持配对/心跳、session 级设备选择和透明远端代理，单在线设备自动选择。
  - 新增 `devbridge_list_devices/get_current_device/switch_device`；远端设备选择后 workspace/文件/命令工具在远端执行，工具注入按名称去重。
  - Gateway 正式 `tools/call` 链路写 AuditLogger；进程日志改读选中项目真实 log_tail；日志/诊断全部提供普通用户语言。
  - 新增 `help_content.py`、设备页、可搜索使用手册、连接方式选择助手和 5 类 `?` 非模态帮助浮层；工作台移除重复自测与底层组件状态。
  - 最终验收：304 passed、Ruff/Pyright 全绿、Multi-Device 6 项集成与三档 UI 布局通过；`v0.7.0` Release、完整安装器、detached 本机升级、快捷方式替换、D:\ 服务恢复和安装版设备工具调用均真机通过。

- 2026-08-11 · **v0.6.0 桌面 UX / 托盘 / 项目级交互隔离**：
  - 全局 busy 改为 `_busy_project_ids`，一个项目运行不再锁其它项目；READY 项目可停止，IDLE/ERROR 项目可独立启动。
  - 顶层导航收敛为工作台 / 项目设置 / 诊断 / 日志 / 设置，进程/审计/Gateway 日志收进日志二级页，常驻说明文案精简。
  - `AppConfig.close_behavior` 默认 `tray`；标题栏“×”隐藏到系统托盘，托盘恢复/退出；设置可改为 `exit`。标题栏“—”保持普通任务栏最小化。
  - 新用户无项目时不展示机器相关路径或连接数据；未选项目连接信息显示“选择项目后显示”。
  - 新增 v0.6 回归测试；当前全量 294 passed，Ruff/Pyright 全绿；`v0.6.0` Release、安装版、托盘行为、detached 接力与桌面快捷方式替换均已真机验收。

- 2026-08-10 · **桌面交互与按项目独立配置（v0.5.0）**：
  - 项目表改为六列并移除 `enabled` UI/自动恢复；状态 1 秒刷新，操作列与服务控制均为动态“启动/停止”单按钮。
  - `ProjectConfig` 新增 `client_target / gemini_redirect_uri / gateway_port` 等按项目配置；桌面默认权限改为 `system + full_system`，首次风险确认保留。
  - 新增 `project_secrets.py`：Bearer 与 Cloudflare Tunnel Token 按项目加密保存；旧共享访问令牌只迁移给首个兼容项目，避免 Bearer 路由歧义。
  - Gateway 支持按项目 Bearer / OAuth workspace 路由并使用目标项目对应上游凭据；Gemini consent 未选项目返回 400、未运行返回 409，不再静默授权。
  - 四种连接方式全部恢复；所有公网 Tunnel 均终止在 Gateway，Local 不依赖 cloudflared；Quick/ngrok URL 统一带 `/mcp`。
  - 新增组件状态、一键连接诊断、项目级 self-test 缓存、无滚轮下拉框、异步窗口退出与 `upgrade-resume.json` 升级接力。
  - 构建改为 `dist/staging-<version>`，可在旧版 EXE 正占用历史 dist 时在线构建新版；0.5.0 PyInstaller + Inno Setup 已成功产出，289 项测试全绿，`v0.5.0` Release 已发布，本机 detached 接力安装/快捷方式替换/`D:\` 公网入口恢复已真机验收。

- 2026-08-09 · **多项目并行（v0.3.0）**：
  - 新模块 `project_manager.py`：每项目一个 `ProjectUnit`（自己的 CodexPro + Windows 桥管理器，
    自己的端口，`CODEXPRO_ROOT/ALLOWED_ROOTS` 指向各自目录，互不干扰、可同时运行）；
    `ProjectManager` 维护 `projects.json` 编目（项目 id、每项目端口分配、增删、自动恢复）。
  - `ProjectConfig` 新增 `id / codexpro_port / windows_bridge_port / windows_enabled / enabled`；
    `RuntimeConfig.project_catalog_enabled`（后端是否加载项目编目）；旧配置自动迁移（id 补发、
    端口补齐）。`engines.CodexProManager/WindowsBridgeManager.wait_ready` 现在把自身状态置
    READY（此前只有协调层置 READY，多项目视图卡在“启动中”）。
  - 会话级工作区绑定（不改协议层）：`tools.py` 新增 `WorkspaceCatalog`，按请求上下文
    （`ctx.session_id` / `mcp-session-id` 请求头，兼容 Starlette Headers 非 dict 结构）绑定
    项目；`switch_workspace(project_id)` 只影响调用它的 MCP session；新增工具
    `list_projects / switch_workspace / shell_info`（默认 Shell 优先 pwsh>powershell>cmd>Git
    Bash，WSL 永不默认）。
  - 桌面：项目下拉 → 项目表格（名称/路径/状态/CodexPro端口/启用勾选/入口标记）+「添加项目…
    /删除项目/启动项目（引擎）/停止项目」；启动桌面自动恢复「启用」项目引擎；「启动公网服务」
    复用选中项目的引擎实例（`_bind_coord_engines`，不重复 spawn）；启动预检对已运行引擎放行。
  - 测试：新增 tests/test_project_manager.py（19 项，含双引擎真机并行 spawn）、
    tests/test_workspace_switch.py（9 项）、test_mcp_integration 新增双会话隔离真实链路；
    全量 **258 全绿**；ruff/pyright 0 错误；offscreen UI 冒烟通过。

- 2026-08-09 · **桌面 UI 响应式重构 + 权限模式合一（v0.2.0）**：
  - 布局：`QMainWindow → QScrollArea`（垂直 AsNeeded / 水平 AlwaysOff）→ 内容 widget；
    默认 1200×850、最小 900×650；自测 1366×768 / 1920×1080 / 900×650 滚动与压缩行为正常，无横向滚动。
  - 长文本改只读 QLineEdit（Token、MCP 地址、Cloudflare Service URL、Gemini Client ID/Secret）；
    最近消息改 QPlainTextEdit（≥150px、自动滚底、上限 300 行、NoWrap 横向滚动）。
  - 服务控制按钮区：启动/停止/重启居左、高级设置居右，按钮最小策略防挤压；字体标题 14px/正文 12px；
    控件间距 8px、GroupBox 内边距 12px。
  - **权限模式与命令执行档位合一（仅 UI）**：只读=read_only+safe / 默认=workspace+developer /
    完全访问=system+full_system；移除独立档位下拉；风险确认合并为一次（`full_system_risk_accepted`
    随系统确认一并置位，兼容旧配置旧账号）。`--execution-profile` CLI 与引擎映射保持不变。
  - 只改 `desktop_main.py`（UI 层），后端/工具/配置结构未动；全量 239 全绿；ruff/pyright 0 错误。
- 2026-08-09 · **Shell 执行档位（safe / developer / full_system）+ Shell 管理**：
  - 新模块 `execution_profile.py`：`check_execution(command, profile)` 纯策略判定；
    - developer（默认）：首命令 ∈ 开发工具白名单（pytest/pyright/ruff/git/npm/uv/python/…）；
    - safe：保留原项目内命令行为；full_system：任意命令，需一次性风险确认
      （桌面「命令执行档位」下拉 + 首次确认对话框，`AppConfig.full_system_risk_accepted`）。
    - 危险命令硬拦截（所有档位）：format/diskpart/shutdown/reboot/bcdedit/reg delete/msiexec…、
      `rm -rf /`、`del /s C:`、`Remove-Item -Recurse C:\Windows` 等递归删盘/系统目录命令。
  - `shell.py` 新增 ShellInfo / detect_shells() / default_shell() / get_shell_info()：
    顺序 pwsh → Windows PowerShell → cmd → Git Bash；**WSL Bash 永不自动选择**（检测并报告）。
  - `tools.py`：`LocalDevTools(execution_profile=, full_system_confirmed=)`，
    run_command / run_program / start_process 均过档位校验；新增 MCP 工具 `shell_self_test`
    （Shell + python/git/pytest/pyright 探测，pytest/pyright 走 `python -m` 规避 uv trampoline）。
  - 配置贯通：AppConfig / RuntimeConfig / StartOptions / standalone_server `--execution-profile`
    + `--confirm-full-system`；desktop_main「命令执行档位」下拉 + 「开发环境检测」按钮。
  - CodexPro fork：`CODEXPRO_BASH_MODE` 新增 `developer`（bashOps.ts 首词白名单 + 危险拦截，
    git 子命令完整允许；safe/off/full 不变），`build_codex_env` workspace+developer → developer。
  - 测试：tests/test_execution_profile.py 26 项；全量 **239 全绿**（含 test_app_state，先停占用
    8787 端口的运行实例再跑）；ruff/pyright 0 错误；fork `npm run build` 通过；build.ps1 全链通过。

- 2026-08-09 · **端口配置统一（改动部署）**：端口默认值集中 `constants.DEFAULT_*_PORT`
  （Gateway 8786 / CodexPro 8787 / Windows-MCP 28731 / Legacy backend 8765，废弃 2865 与
  含义模糊的 local_port）；AppConfig 增加 4 个持久化端口字段（1-65535 校验、旧配置迁移）；
  RuntimeConfig.local_port 改为兼容属性（底层 legacy_backend_port）。桌面主界面新增
  「公网入口端口（Gateway）」（检测端口/恢复默认/复制 Service URL/占用提示/运行期锁定）
  与「高级设置…」内部端口对话框；启动前端口占用预检（CodexPro/Windows 桥/桌面 UI 层含
  Gateway），绝不偷偷换端口；CodexPro/Windows 桥端口贯穿 ServiceCoordinator 与
  CODEXPRO_WINDOWS_BRIDGE_URL 注入。新增 tests/test_port_config.py 16 项。
- 2026-08-09 · **Windows-MCP 锁版本 + 工具白名单**：`engines.py` 新增
  `WINDOWS_MCP_PINNED_VERSION=0.8.2`，启动命令 `uvx --from windows-mcp==0.8.2 ...`；
  `CODEXPRO_WINDOWS_PROFILE` 按权限模式注入（read_only/workspace→desktop_ui，
  system→system_full）；`windowsBridge.ts` 真实强制白名单：`windows_call`
  校验工具 ∈ 权限档白名单 **并且** ∈ 桥端实时 inventory，拒绝即报错不转发。
- 2026-08-09 · **构建链与发布基础设施**：重写 `scripts/build.ps1`（UTF-8、pytest→ruff→
  pyright→PyInstaller→ISCC，版本一致性检查，纯 ASCII 输出）；新增
  `.github/workflows/ci.yml`（test/lint/typecheck/TS build）与 `release.yml`（PyInstaller
  onedir artifact）；根 LICENSE（MIT）+ THIRD_PARTY_LICENSES.md 补充 Windows-MCP、
  cloudflared、桌面运行时清单。
- 2026-08-09 · **venv 目录迁移注意**：仓库从 `local-dev-mcp-bridge` 更名为
  `mcp-devBridge` 后，`.venv` 的 editable `.pth` 仍指向旧路径导致子进程
  `ModuleNotFoundError`；用 `uv pip install -e ".[dev,package]"` 重装即可。
- 2026-08-08 · 创建 AGENTS.md、项目架构.md、开发计划.md、进度验收.md。
- 2026-08-08 · 引擎/隧道/协调层入主目录并接入目录结构：engines.py、tunnel_manager.py、
  app_state.py（ServiceCoordinator）、desktop_main.py（Phase 3 桌面）；新增三套单元测试，
  全量 124 全绿。
- 2026-08-08 · Phase 3 收尾：desktop_main.py 接线 ServiceCoordinator（连接方式下拉、隧道令牌、
  Windows 桥自动令牌、URL 固定性标识、Quick/system 风险确认），桌面冒烟通过。
- 2026-08-08 · **Phase 4 transport_security 落地**：`server_factory.build_transport_security()`（方案 B，
  allowed_hosts 追加公网域名，rebinding 防护保持开启）；`RuntimeConfig.public_hostname` 贯通
  （`standalone_server --public-hostname`）；新增 tests/test_transport_security.py（12 项），全量 136 全绿。
- 2026-08-08 · 真机准备：用户已建 Cloudflare 路由 `mcp.shiningsugar.shop` → localhost:8787（DNS 生效）；
          修复 `TunnelManager` 令牌分支命令（`tunnel --url` → `tunnel run --token`）；全量 137 全绿。
- 2026-08-08 · **Phase 5 完成**：ProjectConfig 扩展 4 个可空 git 字段；`models.git_field_error()` 中文校验
  （空格/引号/控制符/元字符/邮箱格式）；桌面「Git 参数」区 + 保存按钮 + 启动时校验拦截；测试 8 项，全量 145 全绿。
- 2026-08-08 · 排查「隧道建立失败」：tunnel.log 打印 help → `--no-autoupdate` 在 `tunnel run` 子命令后解析失败
  （cloudflared 2026.7.3 实测），已从令牌分支移除（Quick 分支保留）；桌面重试后即可真机验收（见 进度验收.md）。
- 2026-08-08 · **真机验收完成（Phase 4 ✅）**：`https://mcp.shiningsugar.shop/mcp` 无 Bearer 返回 401，
  全链路（CF 边缘→隧道→:8787→认证）验证通过；桌面「Cloudflare 固定地址」流程定性正常。
- 2026-08-08 · **Phase 5/6 完成**：Git 桌面参数 4 字段+校验；三 Tab 日志页 + 轮转 + `key.upper()` 脱敏修复；全量 154 全绿。
- 2026-08-08 · **Phase 7 开工**：`packaging/local-dev-mcp-bridge.spec`（ROOT=`SPECPATH.parent`）onedir 构建成功、
  启动冒烟通过、cloudflared 捆绑；`scripts/build.ps1`（pytest+ruff+pyinstaller 一键）、`scripts/installer.iss`（ISCC 待编译）；
  `docs/en/` 五份英文文档创建。build 脚本用 `uv pip install pyinstaller`（venv 无 pip 模块）。
- 2026-08-08 · **Phase 7 完成**：安装 Inno Setup 6.7.3（winget，per-user 路径 `%LOCALAPPDATA%\Programs\Inno Setup 6`；
  无中文语言包 → 仅 English 语言节）；`PrivilegesRequired=lowest` 免 UAC per-user 安装；
  `release\LocalDevMCPBridge-Setup-0.1.0.exe` 编译并静默安装 EXIT=0，安装版启动冒烟通过；
  剩余验收项为 GUI 操作（选项目→启动→自测），待用户补跑。
- 2026-08-08 · **打包崩溃修复**：双击桌面快捷方式报 `ImportError: attempted relative import with no known parent package`（desktop_main.py:45）
  → 根因：spec 直接把 `desktop_main.py` 当入口冻结，顶层 `__main__` 无包名，相对导入必然失败；
  修复：新增 `packaging/entry_desktop.py` 绝对导入包装入口，spec 入口改用它；重建+重编安装器+覆盖安装+启动冒烟均通过。
  注意：今后 spec 入口必须是"无包内相对导入"的普通脚本，或用 `from local_dev_mcp_bridge.x import main` 包装。
- 2026-08-08 · **启动失败修复 + 令牌记忆**：
  - `CodexPro 构建产物缺失`：根因是打包后 `Path(__file__).parents[2]/third_party/codexpro/dist` 在 `_internal` 布局下
    解析到不存在的路径。修复：`CodexProManager._resolve_dist_dir()` 候选链（env `CODEXPRO_DIST_DIR` → 冻结 `_MEIPASS`
    捆绑副本 → 源码树 → 项目根 `third_party/codexpro/dist` → ProgramData）；spec `datas` 捆绑 codexpro dist（0.7MB，
    安装版 `_internal\third_party\codexpro\dist\http.js` 已验证存在）。
  - 隧道令牌默认记忆：输入框留空时自动使用上次令牌；新令牌输入即时加密保存（SecretsStore
    `LocalDevMCPBridge/CloudflareTunnelToken`），下次启动自动填入；不再每次复制粘贴。
- 2026-08-08 · **第二次启动失败修复（express 依赖缺失）**：codexpro.log 显示
  `ERR_MODULE_NOT_FOUND: Cannot find package 'express'` — 捆绑的 dist 没有兄弟 `node_modules`，
  ESM 依赖沿目录树向上解析必然失败。修复：spec `datas` 捆绑 `third_party/codexpro/node_modules`（51MB）；
  `_find_http_js()` 优先选择**带 node_modules 的 dist**（显式 `dist_dir` 仍权威直返，测试语义不变，全量 154 绿）；
  用安装版 `_internal` 产物实测 `node http.js` 成功输出
  `[CodexPro] HTTP MCP listening on http://127.0.0.1:<port>/mcp`。
- 2026-08-08 · **Phase 8 OAuth 实现完成**：
  - `oauth_provider.py`：`LocalOAuthProvider`（SDK `OAuthAuthorizationServerProvider` 实现）。
    单用户、无注册表：DCR 仅接受 `ACCESS_VIEW_MANAGE_MCP_CONTENT` 单 scope；同意窗口 10 分钟、
    code 单次 5 分钟、access 1 小时、refresh 60 天加密落盘 + 轮换、RFC 8707 resource 绑定；
    client 注册密文落盘（SecretsStore → DPAPI/凭据管理），token 仅内存（access 哈希索引）。
  - `gateway.py`：uvicorn 127.0.0.1:8786（`GATEWAY_HOST/GATEWAY_PORT`）。SDK `create_auth_routes`
    （`/.well-known/oauth-authorization-server`、`/authorize`、`/token`、`/register`、`/revoke`
     + protected-resource 元数据）；`/consent` 浏览器同意页（允许/取消）；`/health`；`/mcp` 反向代理：
    OAuth 校验通过 → 换成引擎 Bearer（`upstream_legacy_token` 与引擎 secrets 同源）；旧版 ChatGPT
    Bearer 常量时间比对直通；本机匿名仅回环；401 带 `resource_metadata`；
    不写审计/日志 → 任何令牌零落盘。
  - 编排：`ServiceCoordinator` 非 LOCAL 模式在 codex 就绪后拉起网关（端口探测 8786），公网域名取
    隧道固定 URL，失败清理，停止顺序 Codex→桥→网关→隧道；`component_states` 含 gateway。
  - 依赖：`httpx>=0.28`；`pyproject` 已更新；测试 `test_oauth.py` 27 项 + 文档，全量 **181 全绿**。
  - 待用户：CF 路由 Service URL 8787 → 8786（单次）→ Gemini 走 OAuth 授权流程真机验收。
- 2026-08-08 · **Phase 8 真机修复 + 验收通过**：
  - 公网 502 根因：路由已切 8786，但安装版仍是 Phase 7 构建（dist 19:24 < gateway.py 20:41），8786 无监听。
    修复：重建 PyInstaller（spec 已含 oauth_provider/gateway）+ ISCC 重编 + 静默重装 + 重启桌面。
  - 冻结崩溃 `unable to configure formatter 'default'`：PyInstaller 内 uvicorn dictConfig 无法动态导入
    `uvicorn.logging.DefaultFormatter` → 网关 `uvicorn.Config(log_config=None)`（仅网关，1 行）；
    重建重装后桌面「开始服务」正常。
  - **真机全链路 ✅**：`netstat` 8786 LISTENING；localhost metadata 200；
    `https://mcp.shiningsugar.shop/mcp` 匿名 401（`WWW-Authenticate` 带 `resource_metadata`，502 消除）；
    带引擎令牌 POST /mcp initialize → CodexPro 0.29.0 SSE 200。
    剩：Gemini 自定义 MCP 填入同一 URL 走 OAuth 授权流程（用户操作）。
- 2026-08-08 · **Gemini 静态 OAuth Client 路径**（DCR 保留不动）：
  - `oauth_provider.get_or_create_gemini_client(redirect_uri, rotate_secret=)`：预注册 confidential client
    （client_name "Gemini Spark"、`client_secret_post`、grant_types authorization_code+refresh_token、
    response_types code、单 scope、PKCE S256）；同 URI 复用同一 client_id；rotate 仅换 secret（旧值立即失效）；
    记录加密存 SecretsStore，redirect_uri→client_id 映射键 `LocalDevMCPBridge/OAuthStaticUriLookup:`。
  - CLI：`python -m local_dev_mcp_bridge.gemini_oauth --redirect-uri <URI> [--rotate] [--print-secret]`
    —— secret 默认不打印；桌面「服务配置」新增「Gemini OAuth 凭据…」对话框（URI 输入、生成/更新、
    重新生成、Client ID 复制、Secret 掩码+复制，绝不落日志/config）。
  - 测试 `test_gemini_oauth.py` 11 项（创建/复用/轮换/精确 redirect/全流程 PKCE/错误 secret/错误 URI/
    code 绑定 redirect/重启持久/零明文落盘）；全量 **192 全绿**；重建+重装完成。
    剩余：用户在 Gemini Custom Connected App 粘贴凭据后真机验证。