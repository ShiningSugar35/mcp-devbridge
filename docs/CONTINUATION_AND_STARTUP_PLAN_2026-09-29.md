# MCP DevBridge：35 分钟安全续接与批量启动治理方案

- 日期：2026-09-29；调研范围：D:\Environment\mcp。
- 状态：**开发设计，尚未实现、尚未进行真实宿主续轮验收**。
- 本次交付为 L0 调研/文档；后续涉及运行态、持久化、权限和跨平台的实施按 L3 验收。
- 设计交付 run：`lr_mum2irpo_5fc5128eac3f`。它不执行开发，不替代历史 `lr_mtfcuqcl_6abdc2169c31`，也不把历史 S8–S10 改成完成。
- 核心决策：复用既有 LongRunStore、BashTaskManager、ProjectManager 和 Hub；分别补齐续接控制面与项目启动生命周期，不再增加一套聊天历史库、LLM 执行框架或小时级子排程。

## 1. 需求与边界

### 1.1 用户目标

复杂任务由 ChatGPT Regular Chat 发起。在本轮尚未完成且接近 35 分钟时，保存可复核交接点，释放本轮执行权，由 MCP 管理后续恢复和接力，直到已有验收门真正完成。减少对聊天上下文、人工输入“继续”和 ChatGPT Scheduled Task 的依赖。同时修复桌面“启动所有项目”长时间卡顿、部分失败及失败原因不清楚的问题。

35 分钟是本项目根据用户体验设置的安全预算，不是 OpenAI 官方公布的固定 ChatGPT turn timeout。不得以增加 MCP 请求超时、持续 SSE 心跳或反复 status 查询来宣称突破宿主限制。

### 1.2 必须分别交付的三种能力

| 能力 | 交付含义 | 本次判断 |
| --- | --- | --- |
| 本地执行连续性 | 已授权命令、测试或已启动进程不因聊天断开而丢失；重连取回同一事实 | 既有基础可复用，需补持久化与所有权硬化 |
| Regular Chat 新推理轮 | 前轮结束后，由受支持入口触发真正的新模型轮，并重新调用正确工作区 MCP | 官方 MCP Apps follow-up 可做能力验证，尚未在当前账号实测 |
| 离线无人值守推理 | 浏览器关闭、组件被回收后仍能自动唤起新的 ChatGPT 推理轮 | 本次未取得当前账号/连接器可用证据；不可标称已支持 |

本地计时器只会调度代码，不能自己变成语言模型。Native MCP Tasks 解决异步工具结果获取，并不单独证明可以启动新的 ChatGPT 推理轮。两者与宿主唤醒必须拆开。[R1]

沿用历史用户约束：不默认转用 OpenAI API 付费、Codex CLI/Work 配额；不读取 ChatGPT cookie/token、不接私有网页 API、不用 DOM 抓取或模拟自动确认绕过宿主能力。除非用户明确改变约束，不以换计费渠道冒充解决方案。

### 1.3 非目标

不重做已完成的 durable fallback、terminal receipt、分页详情或 Windows bridge 状态解耦；不扩大 Gateway 的项目所有权；不实现通用分布式调度平台；不新增 Redis/Temporal 常驻依赖；不把 SouthBird、QQBot 或微信的业务规则硬编码进 MCP；不自动扩大权限、重启承载其它会话的 MCP 引擎、重放结果未知的发布/发送动作。

## 2. 已核验背景与证据分级

已阅读 `AGENTS.md`、`项目架构.md`、`开发计划.md`、`进度验收.md` 的近期相关验收段、`docs/en/LONG_RUNNING_TASKS.md`，并读取相关 Python/TypeScript 源码和历史续接 run。工作分支为 `release/v0.8.9.4`；开工时 `AGENTS.md` 已有非本任务改动，以及用户原有未跟踪目录，不纳入本任务提交。

文档的“当前状态”存在历史版本文字不一致：架构与后部验收记录含 0.8.9.4，部分页首/接管清单仍写 0.8.9.3。实施前必须用 Git 源 SHA、实际冻结 EXE/载荷 SHA、运行进程与端口核验，不以任一旧标题决定当前部署事实。本次不据此更改安装或重启。

### 2.1 启动问题：源码已证实的结构性缺陷

| 编号 | 源码位置（此次读取的行号） | 已确认事实 | 影响与证据边界 |
| --- | --- | --- | --- |
| B1 | `elevation.py:430–488` | `_BrokerRuntime.spawn_codex()` 在全局 `self.lock` 内执行 `manager.start()` 和 `manager.wait_ready()` | 外层虽然并发，管理员 broker 内部仍串行等待就绪 |
| B2 | `elevation.py:490–507, 838–839, 934–955` | `child_status()` 竞争同一全局锁；`ElevatedCodexProManager.state` 属性同步 RPC，单次 child_status timeout 为 3 秒 | 单个慢项目可阻塞其它项目状态查询；属性读取并非廉价快照 |
| B3 | `desktop_main.py:3131–3190` | `_poll_status()` 在桌面刷新链路读取项目 state，并多次刷新项目表等 | UI 可等待上述 RPC；这是实际冻结的强源码依据，尚未做本轮现场计时复现 |
| B4 | `desktop_main.py:2496–2535` | 已有最多 8 个 worker，但必须等所有 project future 返回才启动共享连接 | 一个慢/失败项目会拖延健康项目的公网可用时间；不能用“补线程池”修复 |
| B5 | `elevation.py:39–40, 752–794, 1004–1006` | bootstrap 25 秒、RPC 120 秒等预算分散；高权限 wait_ready 忽略传入 timeout | 缺少从排队到最终结果的统一 deadline；锁等待可能放大排队超时 |
| B6 | `project_manager.py:676–708` | `start_enabled()` 有另一套并发入口并跳过部分异常 | 启动路径重复，部分失败容易缺乏一致结果模型 |
| B7 | `desktop_main.py:2328–2353, 2688–2694` | 单项目启动 worker 内调用读取 Qt 控件的 `_current_options()` | 需把配置快照留在 GUI 线程，避免跨线程访问 QWidget |

可能的故障链：慢项目占 broker 全局锁 → 其它 spawn/status 排队 → GUI 同步读取状态被拖住 → 个别请求预算耗尽 → 部分失败 → 批量屏障继续延迟共享连接。此链可由源码解释，但不能声称已精确复现用户最近一次点击或证明所有失败均出于同一原因。

### 2.2 续接基础：应复用及必须修正的地方

| 编号 | 位置 | 结论 |
| --- | --- | --- |
| C1 | `docs/en/LONG_RUNNING_TASKS.md`、`longRunOps.ts` | 已有 plan/checkpoint/evidence/review/terminal 门；无需重新开发任务记忆 |
| C2 | `longRunOps.ts:236–248` | `atomicReplace()` 遇 EEXIST/EPERM/ENOTEMPTY 时会先删旧目标再 rename；中间崩溃可能失去最后一份 canonical 状态。是风险路径，不是已发生损坏的证据 |
| C3 | `longRunOps.ts:252–269` | runLocks 只在单进程内串行化；不能保护另一个引擎或 Python 进程直接写同一文件 |
| C4 | `longRunOps.ts:452–463` | 纯 checkpoint 当前不递增 workRevision；应保留此行为，不能把每次心跳变成过期 Review |
| C5 | `server.ts:1369` | `nativeMcpTasksExtension = not emitted by baseline connector...` 是源码硬编码字符串，不是本次真实客户端协商记录 |
| C6 | 当前 `server_config` | 引擎 registeredTools=38、toolCards=false；Hub 的 50-tool 合同另有自己的作用域。不能据此声称当前 ChatGPT 已渲染续接组件 |

当前源码已有 Windows bridge 可选能力与核心 READY 解耦、owned process tree 清理及 generation guard；`elevation.py:383–417` 已有 Job Object 机制。后续应复用并核验运行版本，不把这些已有能力重复列为“新增实现”。

## 3. 官方研究结论与技术裁决

### 3.1 宿主续轮：先做真实能力门，不先建大调度器

OpenAI 当前文档明确给出 MCP Apps `ui/message`，以及兼容别名 `window.openai.sendFollowUpMessage`，用于组件向对话发送 follow-up。新实现优先标准 `ui/*` 桥，并按能力检测而非宿主名字分支。[R2]

这条路径与私有网页自动化不同，值得在当前 Regular Chat 做最小闭环。它仍依赖宿主所承载的 UI：MCP Apps 规范允许宿主要求消息确认，也允许初始化后任意阶段回收组件。因此不得直接把前台 follow-up 宣称为浏览器关闭后的离线唤醒。[R3]

2026-07-28 MCP 规范已将 Sampling 标为 deprecated，并明确新实现不应采用。不能以旧 sampling/createMessage 方案作为此次新底座。[R4]

当前计划优先顺序：

1. 真实宿主能力观测和官方 UI follow-up POC。
2. 独立交付安全断点、唯一执行权、后台任务恢复。
3. 只有 POC 通过，才开放所验证场景中的自动续轮。
4. 没有可用推理入口时进入 `WAITING_FOR_HOST`，保留本地任务与断点；不假称后台 Agent 正在继续思考。

### 3.2 Native Tasks 的位置

将来在双方真实 opt-in 时，可把既有 task/run 身份映射到 Native Tasks；不能复制第二套成功真值。使用当前扩展定义的 `server/discover` / per-request capabilities / `tasks/get` 等协议，不照搬旧 draft。未声明能力的客户端继续普通工具级 fallback，不能接收到不认识的 task-shaped response。[R1]

该适配不是基础工作包的前置依赖，也不是“自动新建 ChatGPT turn”的替代品。

### 3.3 技术选型

采用小型、可测试的应用级状态机与协议适配器，复用当前持久层。借鉴 durable workflow 的断点和副作用隔离原则，但不引入完整工作流平台。[R7]

Qt 主线程只做 UI 和快照消费；慢 I/O 由 worker 执行，通过 queued signal 传递结果。[R5] Python Future timeout 或 `cancel_futures` 不能当作已经停止运行中的工作；取消与进程回收必须单独建模。[R6]

重试使用显式 operation ID，而不是凭相同参数推断“同一个动作”。对已提交但响应丢失的副作用先对账；不得自动重放。[R8]

## 4. 目标架构：控制面、执行面、宿主适配解耦

```text
ChatGPT Regular Chat / 已验证的官方 MCP App UI
       │ 短工具调用：register / checkpoint / yield / resume
       ▼
既有 Hub 路由、认证、权限与工具白名单
       ▼
项目 CodexPro：ContinuationCoordinator（新增，小模块）
       ├── LongRunStore（唯一任务事实源，增量扩展）
       ├── Ownership / Window policy（纯规则、CAS、fencing）
       ├── BashTaskManager（复用实际命令与 terminal receipt）
       └── HostContinuationAdapter（能力可选，不直接改文件）
                    │ 官方消息请求、回执与下一轮 ACK
                    └── 无能力/组件丢失 → WAITING_FOR_HOST

桌面 Qt UI ── 配置快照/异步信号 ── BatchLifecycleService
                                  ├── ProjectManager（复用）
                                  ├── broker 每项目生命周期
                                  └── shared Hub/Tunnel 独立 single-flight
```

建议新增模块边界（名称为实施建议，不是当前已存在 API）：

- `third_party/codexpro/src/continuation/`：contract、window/ownership rules、coordinator、host capability observation；业务事实仍交由 LongRunStore 写入。
- `src/local_dev_mcp_bridge/project_lifecycle.py`：无 Qt 的批量启动计划、bounded admission、结果快照与事件；由现有 desktop/auto-restore/recovery 入口复用。
- 官方续接 UI 使用独立、最小资源，不给每一个数据工具挂渲染模板；仅负责展示/暂停/请求续轮，不持有项目写权限或完整凭据。[R2]

Gateway 继续只是 dispatcher。Python 不直接修改 Node 的 long-run JSON；不把两个主机上的项目状态合并成隐式全局 workspace。统一逻辑身份至少包含 device/project/canonical-root/run；当前 workspace handle 必须绑定到它，不从相对路径猜目标。

## 5. 35 分钟续接协议

### 5.1 开工登记，而非第 35 分钟才第一次调用

模型接受复杂任务时立即创建或恢复同一 durable run，并登记本轮 invocation/window、工作预算、允许的路径和动作、交接能力、下一安全点。第一次 admission 即启动 MCP 侧计时和恢复契约。

推荐策略参数：`handoff_target=35m`，`closeout_reserve=5m`，正常 `checkpoint_interval<=5m`，长操作的事件边界另存 checkpoint。30 分钟后不再开始无法在余量内进入安全点的大修改；35 分钟前完成 yield/readback。它是上限纪律，不是至少工作满 35/40 分钟；提前完成直接验收，单操作阻塞也不空等凑时长。

MCP 未必知道用户点击发送的准确时刻。以首次已认证 admission 的服务端观察时间计时，记录起点来源；有可靠宿主时间戳才校验使用。不得伪造实际触发时间。进程内使用 monotonic clock，恢复时结合 UTC 时间、process/boot identity、任务观察；遇系统休眠或时钟跳变先对账，不凭墙钟差强抢锁。

只靠模型“记得第35分钟”不鲁棒：工具结果需返回剩余预算/建议交接，服务端在窗口预算耗尽后拒绝旧 owner 发起新的受控变更，但允许有界只读观察、保存断点和收口。服务端不能强制宿主结束思考；必须如实区分已停止新动作与聊天 turn 已结束。

### 5.2 交接事务

```text
ACTIVE
  → QUIESCING（停止接新修改，核验现有任务）
  → HANDOFF_PREPARED（完整断点和 operation intent 已持久化并读回）
  → READY_FOR_RESUME（旧 owner 释放，允许领取）
  → HOST_MESSAGE_REQUESTED（仅已验证宿主适配器）
  → 新 invocation 提交有效 resume ACK / 原子领取
  → ACTIVE（同一 run，新 window/owner epoch）
```

新一轮收到的是“继续这个已保存任务”的最小消息，不能让自然语言消息自带额外权限。真正恢复时重新读取 run、项目 AGENTS、最新决策、Git diff/SHA、活动任务与未完成 acceptance；不依赖上轮聊天摘要作为唯一事实。

需要保存的交接内容：objective/范围与禁止项、plan/work revision、当前 step、已完成证据引用、测试真实结果、未解决缺陷、active task IDs、Git 基线/自有路径及改动 SHA、阻塞分类、下一条具体安全动作、预期 operation ID。不要保存模型内部思考过程、cookie、完整群聊或无界聊天历史。

`HANDOFF_PREPARED` 写失败时，旧窗口不声称交接成功，新窗口不得开工。HTTP 200 只表示请求完成，不代表下一轮推理已经开始；必须等新模型轮对正确 MCP run 的 ACK。

yield 回执也不证明旧 ChatGPT turn 已结束。P0 必须验证宿主的安全续发时机、消息是否入队/中断当前 turn，以及新 turn 的可观察证据；不能以固定 sleep 或前轮自己的 status 调用冒充“新一轮已启动”。若没有可靠的官方消息/turn 边界语义，自动适配器不能通过，只保留明确受限的交接状态。

运行中 coordinator 可以接收 checkpoint、后台 terminal receipt 和宿主事件后立即推进；无就绪执行入口时退避等待，不每小时盲开新窗口。进程内扫描只观察真实 deadline/租约和事件，不把反复读取当作工作进展。

### 5.3 唯一写入者与竞争处理

LongRunStore 保持单一写入权；所有 coordinator/UI/Python 的变更请求通过这个服务。扩展 operation revision、owner epoch、expected revision，身份与 canonical root 校验同现有 PathGuard。业务 workRevision 与控制面变化分开，保持纯心跳不使 Review 过期。

单进程 Promise 锁不等于跨引擎保护：同一 canonical root 若因重启/重叠根被两进程承载，必须有唯一 storage authority 的进程间互斥与恢复证明；仅靠 Map 或先读后写 JSON 不合格。实现优先沿用现有权威引擎路由，只有证明其不能保障唯一性时才增加最小跨进程锁，不引入分布式数据库。

对注册的续接会话，所有受控写入、spawn、发布入口检查 owner epoch；旧 epoch 即使迟到也拒绝。历史窗口超时只表示待恢复，不证明旧进程已经停止写文件。存在运行中的外部写进程时，观察或进入 quiesce；不能靠换 lease 使仍运行的 shell 自动失去文件系统权限。无法 fence 的命令必须等其停止/安全点或把输出隔离后审核，不能盲目接管。

冲突证据必须是有效 lease、确切 RUNNING/PENDING task 或同路径并发变更。COMPLETED/FAILED/CANCELLED 和陈旧状态标签不算活跃冲突。自己本轮创建的 task 也不能反过来成为“别人正在施工”的退出理由。旧窗口未正常 closeout 时追加 abandoned 记录，保留 last activity，不补造 ended_at 或 effective_minutes。

跨项目资源只对真正共享的 Windows GUI、GPU 或部署通道按需做 exclusive lease；不同项目的普通读取不互斥。资源借用需身份、时限、释放与取消；不要用“整个 mcp-grw 忙”阻止一切工作。

### 5.4 本地后台任务、重试及终态

长命令仍用既有 BashTaskManager，保留原 task ID/terminal receipt。窗口结束不自动终止它；下一轮只能观察或恢复同一任务，不能重复启动。未经注册且带副作用的未知命令不自动执行。

同因操作最多两次尝试后停止盲试，转做已授权安全子任务；重试次数应跨窗口保存，不能每轮清零重新撞同一故障。可恢复网络错误用退避；权限拒绝、登录/验证码、需要确认、未知副作用结果分别停在对应状态，不能靠换通道绕过。

建议状态分类：`WAITING_FOR_HOST`、`WAITING_FOR_RESOURCE`、`BLOCKED_INPUT`、`RETRY_AT`、`QUIESCING`、`ACTIVE`、`COMPLETED/FAILED/CANCELLED`。状态是精确原因，不全部用“失败”或“运行中”。命令退出为 0 不代表需求验收完成；最终仍通过既有最新 workRevision 的 Review 与 long_run_complete。

任务完成后停止该 run 的续接请求并生成一次 completion event；通知失败不回滚业务完成、也不重新跑开发。仅阶段门通过、人工动作、重要缺陷/风险、最终完成时提示。elapsed time、实际观察到的活动与后台执行时间分开记录，不能把墙钟差直接称为“有效开发分钟”。

## 6. 持久化、API 与兼容性

### 6.1 先修交接事实源

移除 `atomicReplace()` 的“删旧再 rename”回退。采用保留上一份有效状态的替换/恢复策略，写临时文件后 flush/fsync，校验后替换；Windows 共享占用仅有界重试，不删除旧 canonical 文件。若文件系统不支持所需持久语义，应返回明确写失败并保持旧状态。目录同步等细节按 Windows/Linux 实现能力分别验证，不能只因函数名 atomic 就承诺断电安全。

故障注入覆盖临时文件写完、flush 前后、替换前后、EPERM、磁盘满、同时读、进程被终止；每次启动只能读到完整旧状态或完整新状态，或明确报告可恢复错误，不能拿空文件/不完整状态继续。terminal receipt 与 handoff receipt 在返回 accepted 前落盘；幂等记录与本地状态迁移尽量同一权威提交，外部效果仍需单独对账。

### 6.2 工具合同

拟提供逻辑动作 `continuation_register/status/yield/resume/pause/cancel`，优先经已有 `codexpro` wrapper 的受控 action registry 暴露。实际名字与分组在兼容性门确定，本文不把它们写成现已可调用工具。

使用 wrapper 不等于绕过工具权限或发布审核：新 action 必须有路由、schema、权限映射、读写属性、调用范围和负向测试；不能把启动后续执行藏在只读 status 中。resume 消息中的 token 只能是短期、单用途、run-bound 的交接引用，不能携带长期认证值或扩大范围。

如官方 UI 关联需要改 tool/resource metadata 或不可兼容参数，走明确的 catalog version、客户端刷新/审批和回滚。OpenAI 对已批准 app 的工具/输入可能使用冻结快照，服务端改完不等于客户端自动拿到新能力。[R9] 不以“仍是50个工具”冒充 fingerprint/权限合同完全没变。

### 6.3 可观测性与存储预算

复用 Flight Recorder 和 bounded detail retrieval；新增 window/operation 事件只保留必要脱敏标识、阶段耗时、错误类别与证据引用。保留已有 512 KiB run、50 steps、200 checkpoints、20 review rounds 等硬边界；运行中的 ownership、未确认操作和终态去重依据不得被普通日志轮转误删。高频 process heartbeat 放有界运行态快照，持久化节流并在重要边界立即刷盘。

容量超限明确暂停新 admission，不覆盖旧记录。运行中按事件推进；启动时只扫描已登记的有界 run 集合，不每秒扫描所有磁盘或全仓 JSON。P 个项目、R 个活跃 run 的常规快照/扫描 O(P+R) 足够，不用复杂索引换脆弱性。

## 7. “启动所有项目”的修复设计

### 7.1 管理员 broker：拆掉全局等待，不拆安全边界

全局锁仅用于 children/operation registry 的短暂预留和查询；慢 spawn/readiness 在该项目自己的生命周期锁和 generation 下运行。共享 broker 的 UAC 注册/启动仍 single-flight，但每次批次只做一次必要引导，不让每个项目反复查询计划任务。

按 operation ID + project/config revision 复用相同启动；先持久/登记归属再发起进程。响应丢失时先查询同一 operation，不再 launch 第二份。端口被未知进程占用时拒绝，不按进程名扫杀。每个 child 的所有权、PID 创建时间/代次及 Job Object 关联在可控的最早阶段建立，失败清理只作用于该 child，不 force-stop 共享 broker 或健康项目。

`child_status()` 读取缓存快照，不能等待另一个项目就绪；异步观察更新快照。启动与停止竞态按项目 generation 串行裁决，旧 waiter 不能改新进程的状态。READY 需要对应 generation 的协议/data-plane 验证，不能仅凭 PID 存活。

### 7.2 桌面：主线程零慢 I/O

GUI 线程复制项目设置与连接配置，worker 只使用不可变快照。移除 state 属性中的隐式网络访问，新增明确异步 refresh；界面刷新只消费内存快照和 queued signals。健康探针、凭据读取、计划任务查询、磁盘检查不在 paint/poll 路径执行；一次异步刷新未完成时不叠加刷新队列。[R5]

必须能逐项目显示：排队、授权/共享服务准备、进程启动、协议自检、可用、可选能力降级、失败阶段与原因。单项目完成立即解除其 busy，不等待整个批次。提供“仅重试失败项目”和“取消待启动项”，不逼用户先停止全部健康项目。

### 7.3 批次：有界并发、独立共享连接、统一 deadline

将单启动、批量启动、自动恢复统一调用无 Qt 的 BatchLifecycleService/ProjectManager 原语；保留不同入口策略，例如升级恢复仍可配置为单并发。不要再次复制一套线程池入口。

初始启动并发建议 2，使用 1/2/4 的同条件基准决定默认值，不盲设 8。队列有容量与明确排队状态；有高权限项目时预热 broker；共享 Hub/Tunnel 只启动一次，可与项目启动并行或在首个健康根出现时启动，不再等待最慢项目。根 READY、Hub 本机 READY、公网 READY、Windows bridge READY 分开呈现。

端到端 monotonic deadline 覆盖排队、bootstrap、spawn、readiness 和数据面验证，各层只使用剩余预算。是否把人工 UAC 等待排除出自动预算需单列状态，不混入普通超时。设置批次上限及每项目上限；默认秒数在基准后定，不以无限延长解决根因。

超时并不意味着 future/进程已经终止。[R6] 取消待启动项可撤销队列；运行中项进入有界清理或观察状态，实际结束前不得释放真实并发名额或启动替代实例。单项目失败不回滚健康兄弟项目。

临时连接故障允许预算内重试；配置/权限/路径错误立即分类失败；supervisor 加有界重试计数、退避与熔断，禁止每30秒无限重启。已有用户任务活跃时先降级观察/诊断，不为一次 health 超时主动整树重启。

## 8. 实施工作包与依赖

| WP | 工作包/主要触点 | 交付与退出门 | 依赖 |
| --- | --- | --- | --- |
| P0 | 基线、宿主能力 POC、启动基准；server capability observation、隔离 fixture | 区分源码/安装态；获取真实 client capabilities；官方 follow-up 至少两次新模型轮 ACK；前台/后台/关闭/重载场景分别给结论。若能力不足，标明受限范围并停止该适配分支投入 | 无 |
| P1 | broker/global-lock、GUI 快照、batch lifecycle；elevation.py/project_manager.py/desktop_main.py | RED 复现 → 修复 → GREEN；慢项目不阻塞其它 status/UI；首个健康项目无需等批次；失败可单独重试 | 基线取证；不依赖宿主 POC 成功 |
| P2 | LongRunStore crash safety、唯一 authority、CAS/owner epoch、回执 | 旧状态不会因失败替换丢失；多进程/重叠根不能同时写；未知动作不重放 | 基线；可独立开发 |
| P3 | ContinuationCoordinator、35m policy、受控工具动作、后台任务接续 | 开工即登记；预算内进入安全交接；同 run 同 task 续接；错误/暂停/终态和资源上限可验证 | P2 |
| P4 | 官方 MCP Apps HostContinuationAdapter，Native Tasks 另行能力门 | 当前 Regular Chat 真实多轮闭环；组件 teardown/审批/断网正确停在 WAITING_FOR_HOST；不切换计费或私有通道 | P0 对应能力通过 + P3 |
| P5 | 故障注入、跨平台、真实长时验收、文档、发布 | latest revision Review；Windows/Linux 同源包；目标根 active-task drain；安装后原根和新能力逐项验证 | 待发布声明范围内 P1–P4 通过 |

建议施工顺序：P0 做小规模事实门，与 P1 源码缺陷修复并行；P2→P3→P4；最后 P5。若宿主分支阻断，P1/P2/P3 可形成明确“启动修复+安全交接基础”版本，但不得把它发布成“关浏览器也能自动推理直到完成”。旧 S8–S10 的重新开启必须明确重订当前验收和授权范围，不能沿用历史 done 字样跳过真实宿主门。

## 9. 验收矩阵（以下数字是拟定目标，不是本次测量结果）

### 9.1 续接与安全

- 虚拟时钟覆盖 0/30/35/40 分钟、休眠、时钟回退和重启；35分钟前交接策略必须可重复验证，不靠人工盯表。
- 当前目标 ChatGPT 环境完成至少三个真实模型执行窗口、两次自动交接，至少跨过两个真实 35 分钟预算边界，第三窗口有正确 run/root 的 MCP ACK；另测一次前轮意外结束与重连。加速虚拟时钟测试不能代替这一长时验收。只看到 follow-up 请求成功，不算 PASS。
- 截断网络分别发生在 request 前、效果后/response 前、checkpoint 中；受控 mutation duplicate=0，错误 device/root=0；UNKNOWN 不猜成功。
- 进程崩溃与替换失败后，完整旧状态或新状态仍可读取；并发读取不使 canonical 状态被删除；start/handoff receipt 写失败不放行新执行。
- 新 owner 领取后旧 epoch 写入全拒绝；已有写进程未结束时不凭 lease TTL 盲抢；历史 completed task 不造成永久阻塞。
- review 必须覆盖当前 workRevision；heartbeat 不使 review 过期，真实代码/计划变更会使旧 PASS 失效。
- 连续同因失败两次停止该操作；跨窗口不清空失败次数；人工审批拒绝不能触发替代通道。
- 完成后不再发续接请求、不复跑验收；通知去重；UI 被回收时仍保留 canonical 状态且明确等待宿主。

### 9.2 启动与性能

- GUI 定时器/绘制路径零网络、零 subprocess；线程断言测试阻止 worker 访问 QWidget。参考机器负载下 UI event-loop lag 目标 p95<100ms、max<250ms，记录负载与采样方法。
- 给 A 注入 10秒/30秒 readiness 延迟，B/C 的状态查询与启动不等待 A 的全局锁；broker status fixture 目标 p95<100ms。
- 同条件测试 1/2/4 启动并发，报告首个健康根可用时间、全部完成时间、各阶段 p50/p95、CPU/RSS/句柄、失败分类；不只报总耗时。
- 路径不存在、端口冲突、一次 canary 超时、可选 bridge 失败、公网不可用分别注入；其它健康项目继续服务，UI 正确区分根/本机Hub/公网状态。
- 连点/重复请求 100 次只产生一个相同 operation；取消后旧 waiter 不会把新 generation 改成 READY/ERROR；请求超时后晚到进程不变成无人管理的孤儿。
- 原 READY 项目和用户活动 task 不因批次重试、取消队列或可选能力失败被停止。
- 状态/日志/队列在容量上限内；负载结束后的资源趋势回落，无持续增长。不得拿刚重启后 RSS 更低当作泄漏已修复的证据。

### 9.3 发布门

先 targeted 红灯与修复，再相关集成/权限/恢复测试；发布时按 AGENTS 的 L3 全量 Python、Ruff、双平台 Pyright、TypeScript build/smoke、版本/lockfile、Windows/Linux 同源构建、真实管理员与 MCP 路由、升级恢复验收。SDK/工具快照变更需要独立兼容性证据。

计划内维护前检查目标引擎全部 opened workspace 的 running/cancelling task；有活动即延后该根更新，不取消别人的任务。只用已验证资产、保持历史 tag/Release 不变；源码通过与运行安装通过分别记账。

## 10. 迁移、回滚与交付范围

新的 continuation schema 可选启用并有版本号；旧 run 原状可读，迁移必须保留最后有效状态，未知新版本拒绝写入。新功能默认关闭，在同一允许范围 fixture/单项目通过后逐步开放。宿主适配器可单独关闭，不影响文件工具或既有 task 查询。

不要让旧二进制继续写已迁移且它不理解的状态。回滚前 drain 新 owner，恢复兼容状态或保留新 run 只读；无法安全降级时仅关闭自动续接，继续由新版本读取既有事实。不以删除 run、强杀所有引擎或 reset 工作树完成回滚。

本轮只交付此方案和 `开发计划.md` 中待实施入口；未运行 UI 冷启动复现、未部署适配器、未修改线上模型/账号/权限、未做三窗口 soak。不能将本文的验收目标或历史项目的测试数写成本轮通过结果。

## 11. 权威来源（2026-09-29 核验）

- [R1] Model Context Protocol — Tasks extension overview： https://modelcontextprotocol.io/extensions/tasks/overview 。依据：异步任务句柄、双向显式能力、状态/结果取回及协作取消；不等于通用模型唤醒。
- [R2] OpenAI — Add UI to your MCP server： https://developers.openai.com/plugins/build/chatgpt-ui 。依据：标准 ui/message 与兼容 follow-up alias、能力检测、数据/渲染分离。
- [R3] MCP Apps specification： https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx 。依据：消息请求、宿主可要求确认、UI teardown 与生命周期；前台桥不能假定永久存活。
- [R4] MCP 2026-07-28 Sampling： https://modelcontextprotocol.io/specification/2026-07-28/client/sampling 。依据：Sampling 已 deprecated，新实现不应采用。
- [R5] Qt — Threads and QObjects： https://doc.qt.io/qt-6/threads-qobject.html 。依据：GUI 线程限制、worker 与 queued connections。
- [R6] Python 3.12 — concurrent.futures： https://docs.python.org/3.12/library/concurrent.futures.html 。依据：executor 等待、取消未开始 future 与实际运行中工作的区别。
- [R7] LangGraph — Persistence： https://docs.langchain.com/oss/python/langgraph/persistence 。借鉴 checkpoint 恢复原则；不据此增加框架依赖。
- [R8] AWS Builders' Library — Making retries safe with idempotent APIs： https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/ 。依据：显式请求身份、重试与不确定结果对账。
- [R9] OpenAI — Developer mode and MCP apps in ChatGPT： https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt 。依据：工具/输入冻结快照及更新兼容；具体账号能力仍以实际连接器核验为准。

## 12. 续接开发入口

开发 Agent 首先读取 `AGENTS.md`、`项目架构.md`、`开发计划.md`、近期 `进度验收.md` 和本文；核对 Git/source/install 三种状态，保留别人改动。先完成 P0 的真实能力/启动基准，不因硬编码 capability 字符串跳过验证；P1 启动修复不等待宿主能力门。实施状态写 `开发计划.md` 和届时唯一执行 run，完成项按规范迁移到 `进度验收.md`。本设计 run 的 completed 只表示方案已交付，绝不代表 P0–P5 已实施。
