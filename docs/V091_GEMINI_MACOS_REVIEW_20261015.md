# 0.9.1 Gemini OAuth 与 macOS 兼容核验 — 2026-10-15

## 1. 状态与范围

本报告记录 D:\Environment\mcp 原工程本轮实测和增量修复，不是正式发布回执。目标版本为 0.9.1；读取的维护 HEAD 为 81ea677，分支 release/v0.8.9.6，现有源码版本元数据为 0.9.0。已有 0.9.0 UI、scope 和运行态修复保持，不重复实现。原 durable 为 lr_mv7j94y4_0b4532683719。

本轮未修改原 GPT 访问码、插件配置、域名、OAuth 主人身份流程或项目权限；未创建正式 tag/Release，未安装或主动启停项目。运行根快照出现变化：先为 mcp 专用根未运行、D 根在线；后为 mcp 专用根恢复、D 根未运行，其余四根在线。诊断沿当时已运行的最具体合法入口进行，不能把这项变化归因为本轮执行了重启，也不能沿用“始终六根在线”的旧描述。

## 2. OAuth 断点：已知事实与未知原因分开

### 实际用户序列

2026-10-15 00:35:42 UTC：DCR /register 201。
00:35:54：/authorize 302，已分类为 consent_page。
00:35:55：/consent GET 200。
00:35:57：/consent POST allow、302。
该序列之后未观察到能够关联到此次授权的令牌交换。Google 页面的报错不能单独证明服务器某项参数错误或 Google 本身存在 Bug。

本轮完整读取当日日志又发现 00:53 的 GET /token 405 和 00:54 的 POST /token 401，所以不能写成“当日整个日志没有 /token”。这些较晚请求没有足够调用方关联证据，不视为 Google 的授权码交换。

### 端点连通性证据

- WJP loopback 和公网各两轮无有效凭据探针：POST /token 返回 401、application/json、invalid_client，没有 cf-mitigated=challenge。
- 独立 GRW Windows 电脑也执行了相同的严格条件检查；只有全部条件匹配才 exit0，工具结果确认为 exit0。没有复制凭据，也没有更改 GRW 服务。
- 独立 Linux 沙箱请求在其自身 DNS 解析阶段失败，不用于推断目标域名失效，不计作外部连通验证。
- 公网授权 metadata 的 issuer、authorization/token/registration endpoints 均保持同源。loopback 读取返回公网 issuer 是当前设计，不应误当成发行者地址错误。

结论：/token 并非普遍不可达；仍未排除 Google 来源特定的 WAF/Bot/Challenge 策略、Google 回调会话/账号状态或 Google 后端实现问题。真正区分这些分支需要同一授权窗口的浏览器结果与 Cloudflare Security Events，而不是关闭全站防护再试。

原始脱敏回执：
- .ai-bridge/v091-validation/boundary-0215.json
- .ai-bridge/v091-validation/boundary-final-0247.json
- 最终探针 task c1059449-bd7c-41a2-bc16-79b341046f83，completed / exit0。

### 本地协议缺陷

测试覆盖复杂 state（含加号、斜杠、百分号、Unicode 等）和保留已有 query 的回调。公开客户端与 client_secret_post 的同意、换码、刷新流程通过，没有复现这一组 state 在编码往返后变形。

另外确证两个独立缺陷：
1. 固定版本 SDK 在识别 Basic Header 之前要求表单重复提供 client_id。标准 Basic-only 客户端因而得到 401；五组 state 及一次冲突后正常重试用例暴露同一缺陷。
2. 重复 OAuth 身份参数被表单折叠后接受，重复 client_id 用例返回 200 而不是参数错误。

oauth_transport.py 中已有请求级认证适配候选，但尚未接入 gateway.py 创建的 /token、/revoke 路由；本轮接线没有写入，因此不能称上述两个缺陷已修复。保留原始七项失败，不放宽资源绑定、PKCE、注册认证方式或身份约束。尤其 Google 本次登记的是 client_secret_post，Basic 缺陷不是“原请求未到达网关”的已证实原因。

## 3. 本轮实际完成的 macOS 增量

沿已有 macOS 候选继续修复，而非创建另一套桌面程序：

- macos_app.prepare_environment：空值/空白配置目录变量现在使用正确的原生或旧配置目录，不再留下空配置路径。
- macos_app.bundle_for_executable：Info.plist 根对象不是字典时平稳返回不可识别，避免 .get 引发启动崩溃。
- 私有运行时 PATH 注入保持幂等，重复初始化不重复添加同一目录；明确配置的目录与 Node 路径保留。
- packaging/macos.spec 加入 CodexPro package.json，保留 Node 项目包语义；打包清单和运行时归档提取有独立回归。

新增测试：tests/test_v091_macos_bootstrap.py、tests/test_v091_macos_packaging.py。
旧行为分别 7 failed / 2 passed、1 failed / 4 passed；修复后与既有 Mac 测试合计 26 passed。

当前 Mac 候选支持 arm64/x86_64 分包命名与资产匹配、Application Support/旧配置目录选择、Finder 私有运行时、.app spec 与原生 CI 定义。尚未取得两个架构的实际 .app 构建/运行/安装回执，也未完成 Developer ID 签名或 Apple 公证。当前 Mac 更新入口为下载后替换，不是自动原位热更新；Windows-MCP 和 Windows 管理员 broker 不作为 Mac 功能。

锁文件为 PySide6 6.11.1，其官方 Qt 6.11 文档列出 macOS 13+ 和 arm64/x86_64。候选产品当前最低版本常量 14.4 属于保守设置，不应解释成 Qt 6.11 的硬下限；产品最终最低系统版本仍需要匹配运行时及原生测试证明。原有 AES-GCM 当前用户存储继续使用，不宣称已经新增 Keychain 后端或跨 Windows DPAPI 凭据迁移。

## 4. 诊断与验证辅助代码

新增 scripts/diagnose_oauth_boundary.py：最多读取 64 MiB/150000 行日志、单行有界、只保留 40 条枚举化事件；只发无效测试授权码，不注册客户端、不同意授权、不取得有效令牌。输出仅包含状态、端点类别、已知错误和挑战标记，不输出原始 state/code/secret/Location。异常 JSON 字段类型进行防御性检查。

scripts/validate_local.py 修复 Windows 外层 stdout/stderr 编码，避免包含 Unicode 的用例输出经过 GBK 时损坏验证器结果。每轮验证使用独立项目内 TEMP/配置/缓存与输出目录。诊断脚本再次实际运行和 Ruff 均通过；未生成或宣称独立诊断单元测试通过。

## 5. 最终验证证据

| 验证 | 结果 | 回执目录或任务 |
|---|---|---|
| 完整 pytest，未排除用例 | 715 passed / 7 failed / 3 skipped | full-review-0240；task 1f67a218-bb35-4d9b-9cb1-42c47a81b433 exit1 |
| Mac 启动/配置/包清单 | 26 passed | mac-final-0239；task 164e6536-d983-4952-919c-2bdbe7cf1434 exit0 |
| Ruff src/tests/本轮脚本 | All checks passed | ruff-final2-0244；task 59b3f336-86c4-4341-8f85-494445173695 exit0 |
| Pyright Darwin | 0 errors / 0 warnings | types-darwin-final-0239；task 4786493a-f49d-4919-b4b4-6eab55818551 exit0 |
| Pyright Windows | 0 errors / 0 warnings | types-windows-final-0250；task 32d5caf0-78a2-40a3-91ad-d37ff3210e94 exit0 |
| Pyright Linux | 0 errors / 0 warnings | types-linux-final-0250/receipt.json 和 output.log；checker exit0 |

上述目录均在 .ai-bridge/v091-validation 下。Windows/Linux Pyright 提示有工具新版本，不属于类型错误或类型警告。三平台静态检查在 Windows 上执行，不是 Linux/Mac 原生运行证明。Linux 检查的落盘结果已恢复；原 D 引擎任务的最终归属另按 durable 回执核验，不能用服务切换猜测终态。

## 6. 尚未完成的交付条件

- OAuth 认证适配接线与七项失败回归修复。
- 真实 Gemini 回调成功、有效令牌交换与工具调用；实际 Cloudflare 来源事件和 Gemini Keep Activity 状态尚未读到。官方页面所说的 Keep Activity 不能简单替换成账号总开关“Web & App Activity”。
- Mac arm64/x86_64 原生打包、冻结完整 MCP/权限/安装验收；签名、公证与 Mac 自动更新按实际交付范围分别验收。
- 0.9.1 版本一致性、同源 CI/正式资产、commit/tag/Release、逐根活动任务核验后的 WJP/GRW 更新；GRW 保留既有专用中转。

本轮没有正式发布、安装或热重启回执。工作树保留原用户修改和本轮候选；不把正在进行的工作标为 completed。

## 7. 一手依据

- Google Custom Connected Apps：DCR 与 Keep Activity/个人账号要求。https://support.google.com/gemini/answer/17209137
- Google Account Linking：回调需保持 state 值，Google 收码后从后台调用 token endpoint。https://developers.google.com/identity/account-linking/oauth-linking
- RFC 6749 §2.3.1、§3.2、§4.1：Basic 与表单认证、参数和授权码交换。https://www.rfc-editor.org/rfc/rfc6749
- MCP Authorization：资源绑定与协商。https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- Cloudflare Security Events：按实际事件核验边缘动作。https://developers.cloudflare.com/waf/analytics/security-events/
- Qt 6.11 for macOS：与本工程锁文件对应。https://doc.qt.io/qt-6.11/macos.html
- PyInstaller macOS：原生架构、.app、签名能力及边界。https://pyinstaller.org/en/stable/feature-notes.html
- Google 官方开发者论坛同类一手报告，只有现象参考，不作为本机根因结论。https://discuss.ai.google.dev/t/gemini-spark-custom-mcp-oauth-stops-after-302-callback-and-never-calls-token/177327
