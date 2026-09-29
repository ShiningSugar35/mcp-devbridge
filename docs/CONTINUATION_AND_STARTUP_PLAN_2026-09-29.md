# MCP DevBridge：Scheduled 优先的35分钟续接与批量启动修复

> 2026-09-29 修订2，依据用户本轮明确纠偏与开发授权。此修订取代15627d0中的方案裁决；历史正文在Git保留。状态：实施中，未安装。唯一实施run：`lr_mum3o77z_15248c497e21`。原设计run已完成，仅是文档；旧Regular Chat浏览器实验run不重新执行。

## 1. 最终范围与裁决

用户只要求**浏览器保持打开时**，复杂ChatGPT Chat任务避免在一个超过40分钟的turn里硬撑：正常30分钟开始收口、35分钟前保存进度；没做完就由一条1小时频率的ChatGPT Scheduled任务续接，持续直到既有验收真正完成。浏览器关闭、离线推理、永续同一UI会话、Native MCP Tasks均不是本次前置条件。

主路径为 **ChatGPT Scheduled + MCP持久状态/窗口协议**；官方MCP Apps follow-up只保留可选加速，不能作为主路径开发阻断。35分钟是用户选定的工程预算，不是OpenAI公布的固定时限。修改主路径必须实际执行用户目标，不能仅返回“继续”建议或把后台shell还活着当作GPT仍在开发。

不默认改用API付费、Codex CLI/Work配额；不读取ChatGPT登录密钥、不接私有网页API、不模拟绕过审批。不引入Redis/Temporal、第二任务数据库、第二个LLM编排平台或小时级子排程；不碰SouthBird/QQBot/微信的业务文件。

## 2. 官方交叉验证与真实接口边界

- [R1] OpenAI UI文档明确：标准`ui/message`与兼容`window.openai.sendFollowUpMessage`用于组件请求后续消息。它运行在宿主承载的组件中，不是MCP服务端可以脱离组件直接调用的ChatGPT任务API。
- [R2] MCP Apps规范明确：Host可要求消息确认；UI会发生teardown；成功消息响应与工具/业务执行完成分离。即使浏览器开着，也未取得“超时后的组件必然继续存活/下一模型轮可靠启动”的保证。因此follow-up适合验证正常短交互续轮，不承担本次唯一恢复时钟。
- [R3] OpenAI Scheduled文档明确支持一次/周期任务、符合条件付费计划的每小时任务，以及可用连接应用；是否可用受账号、应用、权限限制，需审批的动作可能暂停。任务创建在Project内也不能直接读取Project附件，因此计划、代码和进度必须从实时MCP工作区取回。
- 本账户已读取的其他排程证明存在小时级Scheduled配置，但`last_run_time`只证明触发记录，不能单独证明MCP写入与测试成功。本次验收要记录Scheduled invocation实际读写同一run的回执。
- 已核验文档没有提供可供本项目直接调用的“创建当前账号Scheduled任务”公共服务端API。本会话确实有ChatGPT的automations创建/更新能力，故实现分工为：MCP准备结构化排程请求；ChatGPT调用宿主Scheduled工具；把真实返回ID/频率/状态回写MCP。不得写一个假HTTP endpoint声称MCP可以直接创建排程。

**结论**：采用Scheduled主路径是工程取舍，不宣称follow-up绝对不可行。没有直接验证长时恢复的接口不进入主依赖。主路径无需等待follow-up POC通过；保留有界、默认关闭的可选适配说明。

来源：
- [R1] https://developers.openai.com/plugins/build/chatgpt-ui
- [R2] https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx （ui/message、ui/resource-teardown、生命周期）
- [R3] https://help.openai.com/en/articles/10291617-scheduled-tasks-in-chatgpt （创建、频率、连接应用、权限、Project文件限制）
- [R4] https://doc.qt.io/qt-6/threads-qobject.html （GUI线程/queued signals）
- [R5] https://docs.python.org/3.12/library/concurrent.futures.html （future取消与实际运行区分）
- [R6] https://nodejs.org/docs/latest-v22.x/api/fs.html （文件同步与替换的实际语义）

## 3. 源码根因与可复用部分

### 批量启动

1. `elevation.py:_BrokerRuntime.spawn_codex`在全局children锁中执行start和wait_ready。外层线程池虽然有8线程，管理员服务内部仍串行；child_status/health共享同锁。
2. `ElevatedCodexProManager.state`属性同步调用child_status，最长等待3秒；GUI `_poll_status`、列表刷新和路由快照会读取该属性，形成UI/控制面阻塞。
3. `desktop_main.py:_start_all_projects`必须等所有future结束才启动共享Hub，健康项目被最慢项目拖住。
4. 单项目worker读取`_current_options()`中的Qt控件；自动恢复/批量启动入口重复、预算分散、失败收口不一致。

上述有当前源码依据；尚未主动停止用户项目复现最近一次点击。用隔离fake进程/锁/Qt测试复现，不打断当前MCP。

### 持久化/续接

- 复用LongRunStore、BashTaskManager及terminal receipt、compact status、按需详情和Review gate；不重做已验收能力。
- `longRunOps.ts:atomicReplace`遇EPERM等错误先删除旧状态再rename，存在丢失最后有效状态的窗口；必须移除删除旧目标的回退。
- LongRunStore现有锁是进程内Promise队列，不代表多进程共享锁。所有续接修改必须走同一个权威引擎/Store；跨进程ownership保护另有负向测试，不让Python直接写run JSON。
- 纯checkpoint不增加workRevision，这是现有正确行为；调度心跳/绑定等控制状态必须与业务工作版本分离。
- `server.ts`中的nativeMcpTasksExtension是硬编码策略说明，不能当作当前客户端声明实测。

## 4. 主路径契约

```text
初始ChatGPT Chat
  → 恢复/创建唯一long run，登记本轮窗口
  → 实际开发/测试 + 有界checkpoint
  → 预计不能在预算内结束或到30–35分钟
  → MCP续接请求（不是真正创建Scheduled）
  → ChatGPT查同一run已绑定排程；无则创建唯一小时任务
  → MCP绑定真实automation ID/回执
  → 保存交接/结束本轮

同一Scheduled每小时触发
  → 读实时MCP规范/run/Git/tasks
  → 对账并领取新窗口，记录真实触发回执
  → 从原step/task继续，绝不另建run或重复创建排程
  → 30–35分钟交接；下一次同一周期任务续接

run完成或用户取消
  → 保持run终态，MCP返回停用确切ID的控制请求
  → ChatGPT停用该Scheduled并回写结果
  → 停用失败只重试控制动作，绝不重新开发
```

已明确是多窗口任务时，可在首轮提前建立同一续接任务，避免第35分钟前宿主突发中断；用户本次已授权按小时接力，本项目据此预先建立，不要求等满35分钟。普通短任务不创建排程。

### 4.1 新能力最小接口

最终源码复用既有`codexpro(action=long_run_update,args={run_id,continuation:{...}})`进行控制状态更新，`long_run_status`返回只读continuation投影。已取消独立continuation_status/continuation_update alias，不新增Gateway权限面。内部操作按schema区分request_schedule、bind_schedule、schedule_failed、open_window、yield_window、pause、resume、scheduler_disabled、scheduler_enabled；控制payload不能与普通业务update字段混合。新能力目前仅源码/隔离引擎已验证，尚未安装到本机正式实例。

- status返回预算、绑定状态、窗口状态、下一动作与`host_action_required`，不发网络请求或暗中启动操作。
- 请求返回稳定request_id和最小prompt模板，带绝对项目路径/run身份，不把长业务状态塞排程prompt。
- 绑定必须记录真实宿主回执，但服务器只能证明“收到宿主声明”，不能伪装独立验证OpenAI内部任务状态。首次Scheduled回调单独记录，创建成功≠执行成功。
- 创建结果未知时先按request_id/run标记对账，不自动新建第二条；失败明确unavailable/needs_reconcile，不写bound。
- 每个run最多一个active binding；一个小时任务足够重复续接，不依赖Scheduled轮内能再次创建子任务。
- 函数扩展必须同时接入Gateway真实动作权限映射、路由、只读拒绝和alias tests；不能把写操作藏进只读工具，不能仅因工具总数仍50就跳过合同验证。

### 4.2 窗口、重入和真实工作

从本次服务端admission观察时间计时，不伪装知道用户点击发送时间。30分钟收口提示、35分钟yield目标；剩余时间进入工具结果提示。每个invocation至多一个窗口，幂等重复open不能顺延deadline或变第二窗。

窗口有owner/invocation ID、递增epoch、start/deadline、最后真实活动、结束原因和next_checkpoint。该owner用于协调续接状态，不宣称它替代OS文件权限或隔离任意外部shell。旧轮仍有同路径活跃写入时不靠TTL强抢；只读观察可继续。过期未收口先查task，未知task保守对账；旧completed/failed/cancelled不是冲突；自己的新task不是别人的冲突。

过期窗口确可接管时保存abandoned及最后活动，不补写虚假ended_at。业务证据、checkpoint和控制心跳区分；不将elapsed wall time宣称有效开发分钟。正常前5分钟必须有实质开发/测试；不为填窗口重复读状态或sleep。

### 4.3 故障与完成

长测试继续使用原task ID，可跨窗口；不因轮询请求超时取消后台进程，不重复跑同一测试。操作同因失败最多两次后停止盲试，失败计数跨窗口持久；有其他安全子任务则切换，无则报告准确阻塞。密码/验证码/权限批准不自动绕过。

只在实际步骤完成、最新业务revision Review通过、terminal receipt闭合后完成run。完成状态派生disable请求；停用未成功时保留终态和绑定，不复跑业务。自动任务缺MCP权限时明确未核验，不用历史对话补造结果。

## 5. 存储与低耦合实现

续接状态作为现有LongRunState的有界可选字段，不另建第二canonical数据库。不保留原始聊天或模型内部思考。状态最多当前窗口+有限历史窗口，未确认的排程操作/活跃owner不得被普通checkpoint轮转删除。

安全写入先写独占临时文件、flush/fsync、替换；替换临时失败仅有界重试，始终保留旧目标。故障时明确失败和可复原事实。旧run兼容读；新字段不能被旧二进制悄悄丢弃，发布时需版本保护/回滚门，不让旧/新引擎同时写迁移状态。

控制状态写入同Store串行锁并核对root/run/revision；当前源码已补项目内跨进程锁文件，并通过双Store、PathGuard/junction、初始化失败和stale-reaper竞态反例。锁恢复无法安全判定时fail-closed要求人工/维护对账，不引入Redis或第二事实库。

每次status O(当前run大小)，受既有512KiB上限；窗口历史与字段单独限额；不每秒扫描全盘。纯控制面变更不使业务Review过期，业务变化仍正常失效Review。沿用脱敏、PathGuard、危险命令、call-stage policy。

## 6. 启动修复实施

### 6.1 Broker

全局锁仅预留/读取children、project lock和停止epoch；start/wait_ready放到每项目锁，健康/状态不等待其他项目。进程生成后立即登记ownership及Job Object，再做readiness，失败只清理自己的实例。same-project start/stop串行，旧结果不得覆盖新generation。全局停止先关准入并等自有启动收口，不能并发漏掉late child。

### 6.2 GUI及共享连接

state属性只返回内存快照；实际RPC刷新由既有supervisor或有界worker驱动，主线程只消费信号。配置快照在GUI线程冻结，worker不读QWidget。单项目完成即显示，无需整批结束。

共享Hub启动与项目启动解耦，single-flight复用现有ServiceCoordinator；首个健康根不等最慢根。保留核心/本机连接/公网/可选Windows能力独立状态。失败只重试该项目，不重启健康兄弟项目。

### 6.3 有界资源与兼容

启动并发默认候选2，经1/2/4同条件基准后定；不要盲增8。预算贯穿排队、broker准备、进程、协议验证；人工授权等待独立状态。Future timeout不等于实际工作退出，不能提前释放permit或重复spawn。引入无Qt批次服务时复用现有ProjectManager，不再造多个控制入口。[R4][R5]

## 7. 工作包及验收

| 阶段 | 交付 | 验收 |
|---|---|---|
| S1 | 本最终方案/主排程绑定 | Scheduled工具返回真实ID；删除错误范围；不以follow-up阻塞 |
| S2 | 安全写入/续接状态 | RED→GREEN覆盖EPERM保留旧状态、重复绑定、窗口重入、过期对账、终态 |
| S3 | MCP alias/权限/提示与宿主配合 | 普通status只读；绑定/请求准确；35分钟策略虚拟时钟；无外部Scheduled假API |
| S4 | broker/GUI/批量启动 | 慢A不阻塞B/status；主线程零RPC；首根不等全部；同项目竞态无重复进程 |
| S5 | 集成/故障/真实Scheduled接力 | 至少2次不同Scheduled invocation对同run回调；不重复业务；TypeScript/Python及权限门 |
| S6 | 文档/commit/push/受控发布 | AGENTS L3全门、Windows/Linux同源、active-task drain、安装后真正可用 |

实现可先交付窄路径安全写入与状态机、broker锁修复，再接入UI/真实接口；未完成项继续登记，不用一次源码单测冒充整项目交付。

拟定性能目标（尚非实测）：GUI事件循环p95<100ms、max<250ms；隔离broker状态查询p95<100ms；单慢项目注入10/30秒，其它项目不持全局锁等待。报告测试环境、首根就绪时间、全部耗时、失败分类及资源趋势。不用重启后RSS变小声称泄漏修复。

## 8. 回滚与开发纪律

旧run和旧工具行为默认兼容；续接模式显式登记后启用。有未知schema/版本时拒绝写而非静默删字段。关闭续接只停止排程/控制动作，不能删run或停止无关命令。跟踪源码SHA、测试和安装态，源码写完不说已上线。

只写D:\Environment\mcp，自有测试TEMP/TMP/cache在项目内；保留AGENTS.md已有修改及未知目录；每个阶段验收后按规范移入进度验收，计划只保留未完成。所有长命令绑定同run。发布前检查目标根所有opened workspace活动任务；有活动延后维护，禁止整树杀掉其它会话。

本轮已明确授权立即开发。该run的小时Scheduled用于接力这一实现，不恢复旧浏览器实验run，不创建平行子任务；真正完成后停用确切绑定排程。后续应从最新checkpoint继续，不重复研究本节已裁决的范围。
