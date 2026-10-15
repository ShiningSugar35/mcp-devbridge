# Gemini 逐工具确认：定位、源码修复与验收边界

日期：2026-10-15。工程：D:\Environment\mcp。沿用原 durable：lr_mv7j94y4_0b4532683719。目标版本0.9.1；本报告不是正式发行或安装回执。

## 1. 当前问题与真实连接状态

用户已确认Gemini完成账号关联并能读写D盘。共享页 https://share.gemini.google/ME3KgMTmAhrx 的只读提取结果显示十次工具调用：run_command → devbridge_list_workspaces → run_command（指定D路由）→ devbridge_get_current_workspace → devbridge_list_workspaces → write → devbridge_switch_workspace（D）→ devbridge_switch_workspace（mcp）→ run_command（PowerShell）→ run_command（cmd）。合计4次命令、1次写入、3次只读查询、2次切换；最后显示读写删除成功。共享页没有足够中间返回值来证明每一步执行成功，不采用提取器“未见错误”等同“全部命令成功”的概括。

这不是重新要求OAuth账号关联。Google消费者产品官方说明，当前任何写操作都要求人工确认；MCP服务端没有一个能替客户端永久同意所有操作的通用开关。不要以工具描述或伪造只读注解代替用户确认。

## 2. 三类原因与证据

### 2.1 Gateway追加的九个工具缺少操作注解

修复前源码 _PYTHON_TOOL_DEFS 九项全部缺annotations，嵌入的41项CodexPro工具已有该字段。真实公网initialize/tools-list只读审计确认同样缺失，合计50个唯一工具。缺失包括用户投诉的工作区列表及当前工作区查询，客户端不能由其声明确定只读性质。

MCP缺省值为readOnlyHint=false、destructiveHint=true、idempotentHint=false、openWorldHint=true。这些是客户端风险提示，不改变服务器的实际权限。Google Enterprise另有明确文档使用readOnlyHint跳过只读确认，但它与消费者Gemini不是同一个产品；本轮不能据此承诺消费者端刷新后一定零弹窗。

### 2.2 写操作确认由Gemini控制

本次用户的命令实际创建和删除文件；run_command也可执行其他改变系统或访问网络的命令，因此不得把整个工具标为只读。write同理。切换工具修改会话路由，保留非只读语义。修复目标是提供真实操作信息、减少不必要的调用，而非隐藏写操作。

### 2.3 嵌套PowerShell引起变量提前展开，增加重试风险

当前run_command已经运行PowerShell。用无副作用样例复现：外层再传powershell -NoProfile -Command "$value = 7; Write-Output $value"，exit1并出现“=不是命令”；直接传$value = 7; Write-Output $value，exit0并返回7。符合Microsoft对双引号变量展开的说明。这证明用户展示的调用形式存在解析问题，但不把它自动当作共享页中每一次调用的失败原因。

另做了一次真实MCP数据面验证：在工程内专用验证目录，以绝对cwd传递一次原始PowerShell脚本，用CreateNew独占创建随机命名临时文件，写入、回读、finally删除，仅操作本次创建文件。一次run_command返回exit0、0.33秒，ReadBackMatched=true、TemporaryFileDeleted=true。没有先列出/切换工作区。该验证来自当前ChatGPT连接，不是Gemini弹窗数量实测。

## 3. 已完成的源码变更

文件src/local_dev_mcp_bridge/gateway.py：
- devbridge_list_workspaces / devbridge_get_current_workspace / devbridge_list_devices / devbridge_get_current_device：readOnlyHint=true、destructiveHint=false、idempotentHint=true、openWorldHint=false。
- run_command / run_program / shell_self_test：readOnlyHint=false、destructiveHint=true、idempotentHint=false、openWorldHint=true。shell_self_test会启动可配置的外部程序和Python模块，不误标为纯读取。
- devbridge_switch_workspace / devbridge_switch_device：非只读、非破坏、保守非幂等、封闭域；保留会话变更语义。
- initialize追加一次性执行说明：raw script、absolute cwd、不要重复嵌套解释器、不要每次先查询/切换、优先专用读取工具、保持客户端确认、执行结果未知时先检查状态再决定是否重试。保留已有上游instructions与serverInfo.version，不解释或改写实际命令。

工具仍为50个，名称、输入参数及原schema指纹不变。注解变更另用独立指纹审计，避免只比较旧schema哈希看不出是否加载新版本。没有改动权限检查、OAuth资源校验、访问码、域名、自动重试和现有连接配置。

新增scripts/audit_tool_annotations.py：只允许本机既有端点与用户既有公网端点，复用原凭据但只在内存中用于请求，禁止重定向；只读取初始化与工具清单，不调用任何工具。输出仅名称及布尔效果字段；不输出凭据、工具自由文本和私有元数据。脚本支持源码候选和运行端分别核验，输出限项目内新文件。

## 4. 验证回执

所有下列目录位于.ai-bridge/v091-validation：

| 验证 | 结果 | 回执标识 |
|---|---|---|
| 注解旧行为 | 12 failed / 1 passed | annotations-red-0610 |
| 新执行说明旧行为 | 2 failed / 13 deselected | guidance-red-0616 |
| 注解、执行说明、审计、稳定合同、路由、双客户端 | 74 passed | approval-final-targeted-0626；task238d27ff-0c6b-4ee8-aaaa-cf8749605ac1 |
| 全仓pytest，不排除用例 | 742 passed / 7 failed / 3 skipped | approval-suite-0620；task44eb7dc8-5e4e-4561-ad14-37c69ba1b436 |
| Ruff src/tests及新增脚本 | All checks passed | approval-lint-final-0630 |
| Pyright src/tests及新增脚本 | 0 errors / 0 warnings | approval-types-final-0626 |

新增tests/test_v091_tool_annotations.py和tests/test_v091_annotation_audit.py共27项用例。覆盖JSON/SSE、幂等注入、不改未知工具、写入仍非只读、50工具合同、上游元数据保留、输出去敏和端点约束。

公网更新前注解审计：annotations-public-before-0611.json，missing_annotations=9，annotation_fingerprint=f512c75a8526fed8c25b7f62d7511774aaa6ac2af8671bb898535c445e08ea28。
源码候选审计：annotations-source-after-0622.json，missing_annotations=0，annotation_fingerprint=2a08b0f0c1b1e5ad485cfd66a3796db715379f21acbebadc78566e29840adab3。
候选通过原schema_fingerprint=489cd30e8e8676ad382c752bed7c46ba9bc2f308fa2d98b2a699793d0c5ee780的既有合同测试；上述公网审计只记录单独的注解指纹，不额外声称重算了公网完整schema指纹。公网初始化返回的0.29.0是所保留的引擎协议身份版本，不用来证明桌面应用安装版本。

## 5. 当前发布边界与后续验证

本轮源码专项通过，但完整工作树仍有tests/test_v091_oauth_protocol.py的7项旧失败：Basic-only客户端身份解析与重复身份参数处理。认证适配仍未接入运行路由，本轮该处接线没有写入成功。没有隐藏或排除这些失败，没有将Gemini当前client_secret_post可用等同所有OAuth分支通过。

0.9.1正式资产、原生Mac arm64/x86_64构建与安装仍需完成。由于正式门未通过，本轮没有commit/push、正式tag/Release、安装或热重启。已有用户工作树保留；当前登记运行根是C和D，不按旧“六根”列表恢复已删除的项目。

部署后必须从真实端点重新取tools/list确认missing_annotations=0，再让Gemini获取新清单；消费者产品的刷新入口和缓存行为以实机为准，不能照抄Enterprise的Reload custom actions路径，也不默认删除重建现有连接。分别记录只读查询和一次明确授权的短写操作的实际确认次数，不能把源码74通过包装成“十次已降到一次”。

## 6. 一手资料

- Google消费者Gemini自定义应用：当前写操作需手动确认。https://support.google.com/gemini/answer/17209137
- Google Enterprise自定义MCP：Configure annotations to skip user confirmation。仅作服务端注解机制参考，消费者端另验。https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server
- MCP官方工具注解说明：缺省保守、注解是提示而非权限执行机制。https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/
- MCP工具规范与客户端交互自由度。https://modelcontextprotocol.io/specification/2026-07-28/server/tools
- Microsoft PowerShell引号和变量展开。https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_quoting_rules?view=powershell-7.5
