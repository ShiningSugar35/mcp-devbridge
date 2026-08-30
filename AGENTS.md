# AGENTS.md — MCP DevBridge v0.8.9 维护指南

本文件是仓库内 AI/Agent 与工程师的最高优先级开发入口。当前维护线目标是 `release/v0.8.9`；v0.9+ 历史保留在远端，不得为了“补功能”把多 Agent runtime 重新混回本维护线。

## 1. 开工阅读顺序

1. `AGENTS.md`：硬约束、强制研发流程与开发入口。
2. `项目架构.md`：当前真实运行架构、路由、安全和平台边界。
3. `开发计划.md`：**仅当前未完成任务**的需求、根因、架构裁决、验收标准与发布门。
4. `进度验收.md`：已经完成且通过验收事项的实际实现/审查/测试/性能/发布证据，以及必要的失败返工审计。
5. `docs/en/LONG_RUNNING_TASKS.md`：数小时任务的 durable plan/checkpoint/review/rework 契约。
6. `docs/en/` 其它文件：公开英文架构、兼容、安全、开发与变更记录。

若文档与代码冲突，以**当前代码 + 可复现测试/运行证据**为准，并在同一变更中修正文档。不得靠聊天记忆、旧报告数字或上个版本的“已通过”结论替代本轮证据。

### 1.1 持久事实源与开发文档职责

- `开发计划.md` 只保存**当前尚未完成**的工作。任何新开发任务在实施前必须先写入，至少包含需求、已证实根因或待验证根因、方案/实施步骤、验收标准、风险与回滚；未完成、未验收或被阻塞的事项继续留在这里。
- 每个可独立验收的工作项一旦**实现完成且验收通过**，必须立即从 `开发计划.md` 删除对应已完成内容，并把完成事实、代码修改、测试、review、性能、构建/发布等证据迁入 `进度验收.md`；不得等到整轮发布结束后再批量清理。若后续未完成事项仍依赖该结果，`开发计划.md` 只保留最小依赖引用，不保留已完成施工记录。
- `进度验收.md` 不得把“计划做”“预计通过”“代码已写但未验收”等事项写成完成。实现中间 checkpoint 由 durable run 保存；完成项迁入时可保留与最终结论直接相关的失败、返工和重测证据，以形成可审计链。
- `项目架构.md` 只维护**当前代码与正式运行态对应的真实架构**，不保存未来计划、施工过程或已经失效的旧架构；历史变更证据放入 `进度验收.md` / CHANGELOG 等历史记录。
- 多阶段或预计超过约 2 分钟的长任务除维护上述三份文档外，还必须同步维护唯一 durable run。durable run 负责执行状态、checkpoint、后台 task、review/completion gate，不替代 `开发计划.md`、`项目架构.md` 或 `进度验收.md`。
- 断连、新会话、Connector 重连、上下文压缩或聊天上下文丢失后，必须按 **`AGENTS.md → 项目架构.md → 开发计划.md → 进度验收.md → durable run`** 的顺序恢复事实和执行状态；随后再用当前代码、worktree 与可复现测试校验冲突。聊天记忆不是事实源，禁止要求用户重新编写/粘贴超长接管提示词来恢复项目状态。
- 如果上述事实源之间不一致，禁止擅自把未验收事项提升为“已完成”；应保留其未完成/阻塞状态，先通过代码、日志、测试、构建或发布资产重新裁定并修正文档。

## 2. 所有开发任务必须执行的强制流水线

除纯问答、纯解释和用户明确要求的只读诊断外，任何功能、修复、重构、性能、安全、发布任务都必须按以下顺序执行。不得因为任务“看起来简单”跳过根因、计划、审查或测试；确实不适用的步骤应在 `开发计划.md` 写明“不适用 + 原因”。

### Phase A — 需求分析师：还原真实需求

- 先读本文件、`项目架构.md`、`开发计划.md`、`进度验收.md` 和当前 git/worktree 状态。
- 把用户描述拆成：现状、期望行为、非目标、兼容约束、安全边界、平台边界、可观测验收结果。
- 对 bug 必须优先建立**可复现证据**；能现场复现则复现，不能复现则记录缺失条件，禁止把推测写成根因。
- 不重复询问用户已经给出的事实。若信息不足但仍可安全推进，采用最小假设并在计划中显式记录。

### Phase B — 架构师：根因定位 + 权威调研

- 沿真实调用链定位：入口 → 路由/状态 → 权限 → 执行 → 持久化/网络 → 返回值。先证明问题在哪一层，再设计方案。
- 对重大架构、协议、OS 权限、安全、第三方 API/SDK 行为，必须联网检索**当前权威来源**。来源优先级：官方规范/厂商文档/上游仓库与 release notes > 高认可生产实践 > 社区经验。
- 至少比较“修表象”“修根因”“更彻底但复杂”的方案，说明为什么选/不选；禁止无依据地照搬别人的架构。
- MCP 相关必须优先核对当前 Model Context Protocol 规范；Windows 权限必须优先核对 Microsoft Learn/Win32；第三方 SDK 必须核对当前版本文档。

### Phase C — 架构师/算法工程师：形成可落地方案

正式写代码前，`开发计划.md` 至少必须包含：

1. 需求与非目标；
2. 复现与根因；
3. 权威来源、关键结论和架构裁决；
4. 数据流/状态流/权限边界；
5. 向后兼容、迁移、失败策略与回滚；
6. 算法复杂度、磁盘 I/O、CPU、内存、线程/进程与网络成本评估；
7. 实施顺序；
8. **可自动化或可人工复现的验收标准**；
9. review/test/release 清单。

性能优化原则：先测/证明热路径，再优化；能用 `O(P)` 小集合线性扫描解决的问题，不为理论复杂度引入脆弱索引；所有缓存必须有明确一致性策略和硬上限；不得用无界 Map/日志/队列换性能。

### Phase D — 全栈开发：按计划实现并持续记证据

- 多阶段或预计超过约 2 分钟的任务必须先 `long_run_start`；每步完成用 `long_run_update` 写证据。
- 实现必须从 targeted regression 开始：bug 修复优先写能复现旧问题的测试，再修代码。
- 只做计划覆盖的最小充分改动；发现新根因需要改变架构时，先更新 `开发计划.md` 再继续。
- 工作项仍在实施或尚未验收时，其状态与中间证据留在 `开发计划.md` + durable run；不得提前写入 `进度验收.md` 冒充完成。工作项验收通过后，按 §1.1 立即迁移：`进度验收.md` 写入真实修改点、测试命令、输出摘要、review/性能、必要的失败返工、构建/发布 SHA，并从 `开发计划.md` 删除对应已完成施工内容；不得复制旧版测试数。
- 长命令只用后台 `bash`/task 运行；同步 `run_command`/`run_program` 只用于短探针。

### Phase E — 代码审查：defect-first，发现问题就返工

实现者视角结束后，必须切换为审查者视角，对照 `开发计划.md` 与本文件逐项检查：

- correctness / concurrency / state isolation / route precedence；
- security / permission / secret / IPC / path traversal；
- compatibility / Windows + Linux；
- failure handling / restart / rollback；
- complexity / memory / CPU / unbounded state；
- tests 是否真正覆盖 acceptance，而不是只测 happy path；
- 文档是否与代码一致；
- worktree 是否混入无关文件。

P0/P1/P2 或任何会导致验收失败的 finding 一律 `FAIL → 返工 → 重审`。review 后只要代码/文档发生影响行为的变化，旧 PASS 立即失效。禁止“带已知问题通过”。

### Phase F — 专业测试：按验收标准逐项验证

- 顺序建议：targeted regression → 静态检查 → 单元/集成 → 跨平台 typecheck → upstream build/smoke → 安全/负向 → 实机/live probe → 正式构建。
- 任何 acceptance criterion 失败都返回开发阶段修复；修复后相关 review 与测试必须重跑。
- 不允许只因“全量 pytest 绿”就跳过工作区、多会话、权限、管理员 token、真实 MCP 数据面等系统级验收。

### Phase G — 发布与上线闭环

只有 review + test 全部 PASS 后才允许：

1. 按需更新 `项目架构.md` / README / CHANGELOG；
2. 把本阶段新增且已经验收的发布/上线事实与证据写入 `进度验收.md`；
3. 复核 §1.1 的逐项迁移已经执行：**`开发计划.md` 只能保留尚未完成任务**；整轮全部完成时才写“当前无待开发任务”，不得留存已完成施工记录；
4. `git diff --check` + 最终 worktree 审计；
5. commit / push release branch；
6. annotated version tag；
7. GitHub Release 与 Windows/Linux 正式资产、checksum 必须来自同一源提交；
8. 使用**正式发布资产**升级/热更新当前 MCP；恢复原本应运行的根服务；
9. 核验桌面快捷方式、进程、端口、local/public health、真实 MCP tool call；
10. 最后执行 `long_run_review(pass)` + `long_run_complete`，再向用户声称完成。

若发布基础设施暂时不可用，必须明确停在具体门，不得用“代码已完成”冒充“正式上线已完成”。

## 3. 当前产品语义

MCP DevBridge 是 PySide6 桌面应用，通过 CodexPro、可选 Windows-MCP 与 Hub Gateway，把本地开发目录提供给 ChatGPT / Gemini 等 MCP 客户端。Windows 是主要桌面平台；Linux / SteamOS Desktop Mode 保持用户目录安装、运行、升级与构建链兼容。

### 3.1 多根路由是硬约束

- 项目列表中所有**运行中（READY）**的根目录同时 active，地位平等。
- 启动 `C:\` 后，`C:\Program Files (x86)\...`、任意 Git/非 Git 子目录均继承该根；启动 `D:\` 同理，无需为每个子目录单独注册。
- 不存在全局“主路由项目 / 入口项目 / 当前项目决定权限”。共享 Hub 独立于项目根；Gateway 是 dispatcher，不是项目 owner。
- MCP 2026-07-28 是 stateless core：现代客户端的跨调用 workspace 状态必须以显式 `workspace_id`/route handle 线程化；不得重新依赖隐藏 transport sticky session。
- `open_workspace` 返回的 opaque CodexPro workspace handle 是应用状态；pathless follow-up 应携带该 handle。Gateway 可同时返回 DevBridge project route handle 供兼容客户端使用。
- 绝对路径按**最具体的运行根**匹配；例如同时运行 `D:\` 与 `D:\Environment\mcp` 时，后者负责其后代路径。
- 路由优先级：显式 DevBridge override > task affinity > 本次绝对 path/cwd/patch/command > opaque workspace handle > legacy soft anchor > 无 handle 时的唯一相对路径 > 无状态 bootstrap fallback。
- 有有效 opaque workspace handle 时，相对路径只在该 workspace 内解析；不得拿相对路径再次与所有 active roots 竞争后把明确 handle判成歧义。
- 没有 handle/soft anchor 时，相对路径只有唯一定位才自动路由；多个根同名必须报歧义，禁止猜测。
- 一次工具调用明确引用两个不同 active root 的绝对路径时应拆分或显式选择，不得静默选择。
- bootstrap fallback 只为了 schema/初始化/真正无证据调用选一个上游；**绝不能写入或报告成 current workspace**。
- `devbridge_switch_workspace` / `devbridge_workspace_id` 保留为兼容、诊断与显式覆盖；它们不是普通 path-bearing 文件/Git/Shell 调用的必备前置步骤。

### 3.2 工作区与权限边界必须分离

- 在 `read_only` / `workspace` 模式，workspace 既是默认 cwd，也是文件访问安全边界；containment 使用 canonical/real path，不得只比较字符串前缀。
- `..`、junction、symlink 不能把受限模式文件操作或 cwd 带出目标根；写入不得写穿 symlink/junction。
- 在 `system/full_system`（UI“完全访问”）模式，workspace 只是**默认工作上下文/cwd**，不是文件系统安全边界；绝对路径允许访问 OS 当前用户/高权限引擎能访问的其它目录/盘符。
- 即便 system/full_system，blocked secret paths、日志 secret 脱敏、危险命令 hard deny、loopback/token 网络边界不得削弱。
- 根盘扫描遇到 `EACCES/EPERM` 等不可读目录应 warning + continue，而不是整次 tree/inventory/inspect 失败。
- Git 工具在给定路径向上发现最近 Git repo，因此磁盘根项目下嵌套仓库可直接使用。

### 3.3 Windows 管理员权限语义

- 应用 `full_system` policy **不等于** Windows elevated token。普通进程不能绕过 UAC 自动获得管理员 token。
- v0.8.9 采用 Microsoft 推荐模型：标准用户 UI/Gateway + 一次用户明确 UAC 授权注册的 elevated task/broker；后续 full-system 引擎通过受控 broker 获得高完整性 token，不逐命令弹 UAC。
- 禁止关闭/绕过 UAC，禁止使用 fodhelper/eventvwr/DLL hijack 等 bypass。
- elevated broker 只能 loopback/本机受控 IPC + 强随机 secret；接口必须最小化、请求/输出有界、可审计，不得成为一个裸露的“任意管理员命令公网 RPC”。
- 拒绝首次 UAC 授权时，系统必须明确说明 full-system admin capability 未启用；不得静默降级后仍显示“完全访问已生效”。

### 3.4 普通用户界面必须使用用户语言

- 工作台、项目设置、普通状态提示和错误弹窗只描述用户需要理解的概念与下一步操作；不得直接暴露 `UAC`、`broker`、`token`、`Gateway`、`full_system`、`IPC`、`CodexPro` 等实现术语。
- 技术术语可保留在诊断、日志、开发文档和代码中；若普通界面确有必要展示底层信息，应先转换成“管理员权限”“访问码”“连接服务”“开发服务”“完全访问”等用户可理解名称。
- 错误提示至少说明：发生了什么、当前影响、用户下一步可做什么。高级能力失败不得把基础连接能力一并锁死；能安全降级时必须提供显式恢复入口，并准确显示降级后的权限状态。

### 3.5 长任务执行纪律也是硬约束

- 多阶段任务或预计超过约 2 分钟的工作，在有写权限时先 `long_run_start`，把 objective、steps 与 acceptance criteria 持久化。
- 长命令只通过后台 `bash` 执行，并用 `long_run_id` / `long_run_step_id` 绑定；禁止把 build/test/install/upload 放进同步 `run_command` / `run_program` 等待数分钟。
- 每个阶段完成后 `long_run_update` 写 checkpoint/证据；无证据不能标 `done`。
- 实现完成后必须 `long_run_review`。FAIL 形成返工并重新审查；任何 review 后的工作变化都会让旧 PASS 失效。
- `long_run_complete` 是最终 return 门：步骤、证据、最新 PASS revision 与后台任务状态全部通过前，不得声称“一条龙已完成”。
- 浏览器刷新、Connector 重连、上下文压缩或新会话恢复时，先按 §1.1 的五级事实源顺序恢复；其中 `.ai-bridge/long-runs/<run_id>.json` 是**长任务执行状态**的最终事实源，用于恢复 step/checkpoint/task/review revision，但不得覆盖 `AGENTS.md`、当前真实架构或已验收文档事实。禁止靠聊天记忆猜进度或重新制造超长接管提示词。
- MCP/CodexPro 重启后旧 `task_id` unknown 时，必须提供明确终态证据后再 resolve。
- 目标仍可执行且无需真实用户输入/授权时，`wait_task` 返回 running 不能成为结束 turn 的理由；继续自动推进并定期给用户简短进展。
- **ChatGPT host 的单轮总执行上限不是 DevBridge 可配置项，OpenAI 当前公开文档也没有给出一个可依赖的固定分钟数。** 禁止把一次约 20–30 分钟的历史中断经验写成平台 SLA。应把可控超时与 host turn 生命周期分离：`run_command/run_program` 单次硬上限 20 秒；`wait_task` 无 progress token 时单次最多 30 秒，有可用 MCP progress token 时最多 120 秒；后台 `bash` 子进程本身无固定执行时限。
- 长任务默认采用“**短工具调用 + 无界后台执行 + 持久 checkpoint**”：普通轮询优先 15–30 秒，不用一个超长 MCP 请求赌 host 生命周期；后台任务继续运行，轮询超时绝不能取消它。每个阶段完成、任何 restart/install/release 等高风险边界前，以及连续执行约 5–10 分钟仍未形成新证据时，都要把事实写入 durable run，确保 host 即使结束当前 turn 也能零猜测恢复。
- 预计独立执行超过约 10 分钟且能由本地 executor 自主完成的 build/test/soak/release 子阶段，优先交给 `bash`、`execute-handoff/loop-handoff` 或等价本地持久 executor；ChatGPT 只做短轮询、审查和决策。不要让浏览器/MCP 请求本身承担长计算寿命。
- 缩短单次工具调用能显著降低“单个 MCP 请求超时/中间层断开”的风险，但**不能保证延长 ChatGPT host 的整轮生命周期**；单轮总生命周期若由平台结束，本地 DevBridge 无法自行创建新的 ChatGPT UI turn。目标是让这种平台边界只造成“自动执行仍在继续/下轮立即恢复”，而不是丢失工作或要求用户重新粘贴上下文。

## 4. Hub、设备与连接方式

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

- Gateway/Tunnel 是共享 Hub，不属于某个入口项目。
- 单独停止一个根不得影响其它 READY 根；最后一个运行根停止后才关闭共享 Hub/Tunnel。
- 多设备选择与本机多根选择是两层概念。目标设备内部仍按自己的 active roots 自动路由。
- `Local` 模式同样经过共享 loopback Gateway，只是不建立公网 Tunnel。

## 5. 安全与密钥

- 引擎、Gateway、legacy backend 默认只绑定 loopback；公网只能经明确 tunnel。
- 公开 URL 与凭据分离。禁止把 Bearer、OAuth secret、Cloudflare token 拼进 URL、日志或仓库。
- Windows：优先 Windows Credential Manager，兼容 DPAPI fallback。
- Linux/SteamOS：优先 desktop secret service；fallback 为用户配置目录 AES-GCM，加密 key/密文限制用户权限。
- 新项目桌面默认仍为 `system + full_system`（完全访问，危险），首次实际启用需一次风险确认；Windows 管理员能力另需一次正规 UAC broker 授权。
- `system/full_system` 允许系统级工作，但已知删盘/格式化/引导修改等危险命令硬拦截继续生效。
- 审计日志中的 command/content/patch 与 secret-like 字段必须脱敏。
- 新路径路由/系统访问不得削弱 PathGuard 的 canonical、symlink、blocked-glob 或 permission checks；system mode 的放宽必须是**显式 policy 分支**，不是删除 guard。

## 6. 关键源码

| 路径 | 责任 |
|---|---|
| `src/local_dev_mcp_bridge/desktop_main.py` | 桌面 UI、项目列表、全部项目启停、配置、首次 full-system/elevation 流程与升级接力 |
| `project_manager.py` | 项目 catalog、每根独立 `ProjectUnit`、端口和生命周期 |
| `app_state.py` | 共享 Hub 的 Tunnel/Gateway 编排；不得持有项目业务状态 |
| `gateway.py` | OAuth/Bearer、Hub MCP 代理、多根/任务/handle/设备无状态路由 |
| `engines.py` | CodexPro / Windows-MCP 进程管理与私有运行时解析 |
| `elevation.py` | Windows elevated task/broker 注册、认证、引擎/短命令委派（v0.8.9） |
| `platform_support.py` | Windows/Linux 平台差异、XDG/桌面路径、进程参数 |
| `secrets.py` | Windows Credential/DPAPI 与 Linux secret-service/AES-GCM |
| `update_manager.py` | GitHub Release 检查、资产选择、更新接力 |
| `third_party/codexpro/src/guard.ts` | workspace handle、PathGuard、systemAccess path policy |
| `third_party/codexpro/src/longRunOps.ts` | durable 长任务 plan/checkpoint/evidence/review/rework/completion 状态机 |
| `third_party/codexpro/` | 项目文件/Git/Shell/任务/分析主引擎 fork |
| `scripts/build.ps1` | Windows 测试、构建、PyInstaller、Inno Setup |
| `scripts/build_linux.sh` | Linux 测试、CodexPro smoke、PyInstaller、tar.gz |
| `.github/workflows/` | Windows + Ubuntu CI / release 构建 |

## 7. 开发与验证

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
.venv\Scripts\python.exe -m pyright --pythonpath .venv\Scripts\python.exe --pythonplatform Linux src tests
.venv\Scripts\python.exe -m pytest tests -q --disable-warnings
cd third_party/codexpro
npm run smoke
```

发布构建：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1 -Version 0.8.9
```

### Linux / SteamOS build host

```bash
uv venv --python 3.12
uv pip install -p .venv -e '.[dev,package]'
cd third_party/codexpro && npm ci && npm run build && cd ../..
bash scripts/build_linux.sh 0.8.9
```

Linux release 以 Ubuntu 22.04 构建保持较旧 glibc 基线；SteamOS Desktop Mode 真机是额外兼容证据，不能用“未真机”掩盖 CI/构建失败。

## 8. v0.8.9 发布门

发布前至少必须满足：

- `git diff --check` 通过，工作树只含有意变更；生成物、缓存、安装运行文件不得 track。
- Python Ruff、Windows/Linux Pyright、全量 pytest 通过。
- CodexPro TypeScript build、完整 smoke 通过；必须覆盖 stateless explicit handle、多根路由、systemAccess、PathGuard 安全回归和 durable long-run。
- Windows elevated broker：错误 token/非授权入口拒绝、首次授权、重复启动、stop/crash、真实 high-integrity 无破坏性探针通过。
- `uv lock --check`；PowerShell parser 与 Bash 发布脚本语法通过。
- Windows `build.ps1 -Version 0.8.9` 成功；安装器 payload 完整。
- GitHub Release workflow 同时产出 Windows installer 与 `MCPDevBridge-Linux-x86_64-0.8.9.tar.gz`。
- commit、branch push、annotated `v0.8.9` tag、GitHub Release 资产必须指向同一源提交。
- 使用正式资产升级当前实例后恢复 C:/D: 根，桌面快捷方式、Gateway health、真实 MCP route/full-system 数据面通过。
- v0.9.x tags/branches 是历史，不删除、不改写、不 force-push。

最终实测只写入 `进度验收.md`；完成任务从 `开发计划.md` 删除。