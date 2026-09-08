# v0.8.9.1 ChatGPT 连接与资源维护报告

日期：2026-09-08。执行记录：`lr_mts500qy_180389c5770b`。本报告为源码冻结时的可复核结果；正式发布与本机升级事实追加在文末，不以源码测试代替安装验收。

## 结论与归因边界

本轮确认并修复了 MCP 本地缺陷，而不能把所有 ChatGPT 错误一概归为 OpenAI。高权限 broker 的一次状态查询超时原先会永久标记引擎 ERROR，使 supervisor 跳过仍可能健康的 HTTP/MCP 数据面并重启该项目。真实 supervisor 日志在 2026-09-08 00:27:17–00:27:34 UTC 记录三个根因 broker TimeoutError 被重启，03:42:43 UTC 又出现两根一次失败。隔离反例复现该放大机制；修复后，查询失败保留最后确认状态并短退避重查，明确退出或真正的数据面连续失败仍进入原恢复路径。最初 broker 为什么超时尚无充分证据，不能把这个放大机制解释成所有超时的最初来源。

本会话也观察到平台提示“因 OpenAI 无法确定请求的安全状态，已拦截此工具调用。”；该调用没有可读的进程结果，后续原生调用可用。它证明本次可见平台拦截，不能单凭这句提示断言请求是否到达 Gateway，更不能把它与另一条流恢复超时报错混为一谈。

本轮后段项目计划补充了准确报错字符串 `ChatGPT stream recovery polling timed out`；本执行流未直接取得截图图像或对应浏览器网络 trace。该字符串在本地 Python 产品源码与 CodexPro TypeScript 源码中精确搜索均为零命中。按文案可判断它描述 ChatGPT 客户端/宿主的会话流恢复轮询超时，而不是本项目直接定义并抛出的同名错误；但本地 MCP 断连、网络或平台端异常都可能成为上游触发因素，不能由文案单独证明唯一根因。本轮没有检索到 OpenAI 对该精确字符串的官方定义；官方通用故障指南也要求区分网络/浏览器/服务端因素，持续问题需带时间戳的网络和控制台证据（https://help.openai.com/en/articles/7996703-troubleshooting-chatgpt-error-messages）。因此不宣称已经彻底修复这条平台提示，不修改工具风险声明绕过平台检查。

## 已实施修复

| 范围 | 原问题 | 处理 |
| --- | --- | --- |
| 管理员进程状态 | 一次观察失败永久 ERROR，可能误重启 | 保留最后确认状态、1秒观察退避；数据面仍作独立裁决；明确退出仍 ERROR |
| 入口鉴权 | 格式错误/重复 Header 可降级到 URL/匿名分支 | Header 存在即权威，错误 fail-closed；合法 URL/Header/OAuth 兼容 |
| 协议分派 | 在正文中搜索 initialize/tools-list 关键词 | 只用已解析 JSON-RPC method；代码/文本内容不改变处理分支 |
| 输入与错误 | 一次性无界读 body、非法 params 抛异常、错误丢 id | 鉴权后 8 MiB/15秒有界读取、明确 envelope 校验、保留合法请求 id |
| 上游响应 | 特殊缓冲分支缺容量/断流处理 | 统一 absolute deadline/8 MiB/单次关闭；网络中断可关联；不自动重放写入 |
| 固定工具清单 | 每次序列化、解析、排序、哈希同一清单 | 仅缓存不可变清单 bytes 与摘要，逐调用编码 id；50 tools/说明/指纹不变 |
| 诊断存储 | legacy Gateway 日志无独立轮转、嵌套和文本脱敏不足 | 递归脱敏/移除整段 URL query；16 KiB entry、4 MiB文件+1备份、7天留存、锁和 fail-open |
| 发布版本 | 三段版本门不识别用户指定的四段版本 | 五版本源0.8.9.1；正确区分 release 第四段与 postN，兼容历史 fixed/post |
| 测试治理 | fake supervisor 故障写入真实日志 | 为原测试隔离 LOG_DIR；历史 fake PID1001→1002不计为真实故障 |

保留固定50-tool合同、原调用阶段权限、SSE事件边界、durable任务与按需详情取回；没有压缩/删除源码上下文。暂停的 S8–S10 和 Regular Chat 实验未恢复。新模块仅分离入口协议和诊断职责，复用已有上游读取/关闭实现，没有第二个任务系统、后台writer线程或无界缓存。

## 验证证据

协议反例：18 failed → 修复后核心全量553 passed/3 skipped。扩展边界补齐后，最终源码全量 **570 passed、3 skipped，130.49秒**；Ruff通过，Windows与Linux Pyright均0 errors/0 warnings，五源版本门通过。批次 `b25905a0-bd8c-467f-9968-938198253fa1`。

误重启反例 `9a69438c-e426-4e8a-8541-7906af2f65ab` 为4 failed/1 passed；修复后与原稳定性联合 `6b4140cb-c55a-400e-8805-d1b762d33216` 为11 passed。四段版本反例先6 failed/4 passed，修复后已包含于最终全量。跳过项是平台条件测试，不标成运行通过；跨平台构建继续在对应操作系统验证。

CodexPro build成功；完整npm smoke任务 `94f80eee-2264-45d8-b064-6ff1d1f5e4aa` exit0，覆盖HTTP、权限、异步task、durable终态/重启/取消观察、详情按需取回及release guard。

固定合同 fingerprint：`489cd30e8e8676ad382c752bed7c46ba9bc2f308fa2d98b2a699793d0c5ee780`。

## 资源测量，不夸大成端到端保证

复测：`python scripts/benchmark_gateway_catalog.py --iterations 1000 --output <项目内路径>`。

| 相同1000次工具清单处理 | 修改前 | 最终修改后 |
| --- | ---: | ---: |
| wall | 1671.3517 ms | 4.3404 ms |
| 单次峰值分配 | 427306 B | 71561 B |
| 返回体（id=0） | 73834 B | 71008 B |
| 工具数量 | 50 | 50 |

单次分配峰值减少约83.25%，wire减少约3.83%（无损移除JSON格式空白）。原CPU计时1656.25ms；最终短测试读数0.0ms落在Windows进程CPU计时分辨率内，不能写成零CPU。静态cache会增加约71KB常驻字节；该权衡适用于反复读取稳定目录，**不是全部MCP调用/公网网络/桌面RSS按该比例提速或下降**。

安装前2026-09-08 12:49:42 +08:00采样2秒：桌面PID324 RSS198.480MiB，CPU约单核1.5625%/全机0.1953%，19线程/1011句柄；父进程关系显示5个项目Node由PID35620持有，另有本实例cloudflared由桌面PID324持有。没有按名字批量杀进程。快照由 `scripts/snapshot_bridge_resources.py` 在安装目录内按executable筛选，无命令行或凭据内容。两秒采样不能证明长期无泄漏，共享页使RSS不能当作独占内存相加。

存储改进是限制今后 legacy Gateway 日志增长，不声称已释放历史若干GB。旧依赖dist-info/历史release和用户installer资产没有可靠归属及重建清单，不进行盲删。

## No Auth 使用与安全取舍

保留用户的个人 URL capability 方式，并不等于服务器“没有鉴权”。完整连接URL等价于持有访问能力的凭据；不要公开、写入截图/报告/issue/普通日志，泄露后应轮换。客户端省去OAuth握手不代表平台工具确认、配额或安全检查会消失。多用户、组织共享、细粒度撤销或需要标准OAuth互操作时，仍应使用OAuth/受支持Header方式。此次不把个人capability模式包装为OpenAI官方认证方案。

旧 `0.8.9.post1` 二进制的更新解析器不识别四段标签；本轮须通过正式 `0.8.9.1` 安装器升级，不能依靠新源码让旧程序自动改变。新版本修复未来四段版本的发现/排序；历史tag/资产保持不动。

## 核对过的主来源

- OpenAI Developer mode：https://developers.openai.com/api/docs/guides/developer-mode
- OpenAI Plugins authentication：https://developers.openai.com/plugins/build/auth
- MCP transports（2026-07-28）：https://modelcontextprotocol.io/specification/2026-07-28/basic/transports
- TypeScript SDK官方迁移：https://ts.sdk.modelcontextprotocol.io/v2/migration/support-2026-07-28
- Python Packaging版本规范：https://packaging.python.org/en/latest/specifications/version-specifiers/

## 升级停止范围补强

执行前审查发现原 worker 按进程名全局停止 MCPDevBridge 实例。本轮改为核验当前安装目录的 exe，只选择指定桌面 PID 与当前配置记录的高权限 broker，并在停止前检查创建时间；未知目录、父进程不匹配或身份改变则拒绝继续，其他安装目录/无路径信息的进程不入选。纯筛选器与 PowerShell AST 反例3 failed→3 passed（`bd67c01b-8f80-4fb6-9d14-9af856f0229d`→`af1be3b5-3f3a-4d15-8d4b-03c68047ab28`），测试没有停止真实进程或运行安装器。

首轮候选 `833792ad008079478bb53161a2464988a28c1618` 的 Actions `34188745711` Windows/Linux 均成功。最终发布重新构建包含停止范围修复的提交，不把旧候选资产作为最终资产。

## 正式发布与本地更新

正式 Release `v0.8.9.1` 已于 2026-09-08T05:38:05Z 发布为 latest，source/tag 为 `08ef5a8a7dd6b3f8e04d529b8808a648a59eb332`，同源 Actions 为 `34189621202`。Windows573 passed/3 skipped，Linux569 passed/7 skipped，双平台类型/Ruff/TS及正式安装载荷门通过；这些结果只覆盖具名冻结提交，不包括后来新增的并发反例。

Windows安装器99,287,552B，SHA-256 `aea4bea498d5b49c373cd814b3a94a8b53e86c3a59e03881ff69056565711ada`；Linux包157,228,990B，SHA-256 `0dd3ceba648d0f2f72c690bdd57d08389042a0da7b6a516e89e2c731b38d8e70`。六项资产的远端digest/size与本地、CI一致；`release-provenance.json`和校验文件随Release发布，旧tag/资产未改写。

本地于2026-09-08 **13:40:15 +08:00**完成原目录受控安装更新，不是零中断热替换。升级前枚举五引擎六workspace，确认其他任务全部终态，再交独立worker执行。`upgrade-result.json`记录ok=true、根恢复5/5、resume_consumed=true、管理员broker就绪；安装路径仍为`D:\Environment\mcp`，五个根及system权限保留。当前ChatGPT会话在短暂502后已经重新连接。

已直接从实际安装EXE的冻结PYZ读取产品版本**0.8.9.1**，不是依赖源码的版本常量；EXE SHA `0e1f13618c653c489840af49e88c211f1949f87b9cac7c226a462d11712d206c`。安装内的升级脚本与审查源码hash相同。桌面PID12112、brokerPID33012。公网与loopback的URL/Header请求共四次均200且50工具fingerprint一致；正确URL加错误Bearer/Basic/空Header的六次请求均401。握手engine_version=0.29.0是CodexPro版本，不是桌面版本。真实读写自检为9pass/3warn/0fail，警告来自full bash和Git忽略目录中的写探针状态，不标成全项通过。

## 安装后资源观察与尚未关闭的问题

安装后两秒采样桌面RSS147.582MiB、13线程/793句柄，broker89.457MiB；安装前桌面198.480MiB、broker21.910MiB。缓存、进程生命周期和同期负载不同，不能据此宣称整体内存优化已达到某个比例，更不能把重启后内存下降当作泄漏修复证据。短采样CPU0.0%同样不代表长期零占用。

`_internal`前后均2804文件，326,688,295→326,689,971B（约311.55MiB），五组重复dist-info仍存在。这次没有实际释放这些历史存量；落地的存储改进是约束今后Gateway诊断日志增长。没有盲删用户`installer/`、历史Release、额外进程或不能确认归属的依赖目录。

**本轮全面审查不能标记PASS。** 收尾时读取到并发会话新增的 `tests/test_v0891_local_tool_concurrency.py`，本执行流复核源码并独立运行其中两项健康响应反例：任务`6951fbe8-68c5-4cf4-a842-99d1b6ae0e7c`结果 **2 failed / 2 deselected**。原因是 `_exec_local_tool` 在共享事件循环中同步执行 `run_command/run_program`，慢命令期间其他协程得不到调度。这项P1并未被此次发布修复，不能拿此前573项通过来覆盖新增失败，也不能据此把没有时间戳/trace的截图错误唯一归因到它。

这一遗漏需要有界线程执行、取消等待后仍持有运行worker容量、异常与过载正确释放、权限/UAC及停止恢复验证；具体反例、方案和未完成门保留于`开发计划.md` §8.2.1。新测试未被覆盖/删除或偷偷加入已冻结发布；未重新移动tag或静默替换资产。当前耗时操作走既有`bash`加`wait_task/get_task`，不把需要回调同一Hub的脚本放在同步`run_program`中；这只是现有可用路径，不等于P1已经消失。

因此，本轮准确状态为：**核心修复、正式发布、原目录安装及基本真实连接验证已完成；新增同步并发缺陷和原截图唯一根因尚未收口，最终总体审查非PASS。**
