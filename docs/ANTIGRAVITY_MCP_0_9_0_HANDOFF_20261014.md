# Antigravity 独立接力提示词｜MCP DevBridge 0.9.0

你是现有 MCP DevBridge 项目的 0.9.0 工程总负责人。请直接执行剩余开发、验证、正式发布和热更新，不是重新规划项目。本文件独立于当前 ChatGPT 聊天，可作为 Antigravity 的完整任务提示词。**先读当前代码与状态，以最新可复现事实为准。**

## 一、工程与目标
- 主工程（先开发、安装）：`D:\Environment\mcp`，WJP desktop，原维护分支 `release/v0.8.9.6`，本机正式运行 0.8.9.6，根 D:，DevBridge 路由 `cdc16789`，原长任务 `lr_mv7j94y4_0b4532683719`。从该 durable 恢复，而不是新建第二个长任务。
- 后续同版本同步升级：GRW desktop，源码 `D:\360MoveData\Users\gaoma\Desktop\mcpDevBridge`，分支 `fix/stability-20261001`，2026-10-13 已验收运行 0.8.9.9，DevBridge 路由 `6e1b6dbf`。该机已有服务器中转，正常工作，不得覆盖机器专用配置、安装身份和服务任务。
- 产品目标：一个共享 Hub 和公网域名 `https://mcp.shiningsugar.shop/mcp`，同时供 Gemini、ChatGPT 使用，不在【项目设置】选择客户端。Gemini 在 Customize → Connectors → Custom 只粘贴**不含 query 的标准 HTTPS URL**；Client ID/Client secret 不填，使用 DCR + OAuth，仅接受首次正常授权同意；不额外要求“主人身份确认”。已经连接的 ChatGPT 继续沿用原来 `/mcp?token=<既有访问码>` 地址，绝不主动轮换访问码、删除 GPT 连接或变更凭据。
- 客户端连接类型不得改变本地根权限、50-tool Hub 合同、工作区路由、安全边界。用户希望 UI 与未完成事项文档使用“尚未实现/待验证/未部署”等中性、准确的描述，不把历史平台运行状态包装成产品缺陷；不可伪造结果或修改历史真实证据。不得通过换工具或绕路去重放此前明确不准执行的同一操作；如遇限制，正常记录具体动作，再完成其他独立工作。

## 二、开工恢复规程
1. 按 `AGENTS.md → 项目架构.md → 开发计划.md → 进度验收.md → docs/en/LONG_RUNNING_TASKS.md → 源码与测试 → 当前 Git/status → durable run` 顺序恢复。旧记录里的版本号可能滞后于当前实机，运行态与 CI/Release 要分别验证。
2. WJP 已有未提交改动，不要 reset、checkout 覆盖：`desktop_main.py`、`models.py`、`help_content.py`、`tunnel_manager.py`、`tests/test_phase11_desktop_ux.py`、`tests/test_v082_desktop_maintenance.py`、`开发计划.md`、`AGENTS.md`。还有未跟踪的 `tests/test_v090_*.py`、`scripts/diagnose_gemini_oauth.py`，以及原用户的 `installer/`、`.cache/`、`.tmp/`、GRW 诊断包。不要删除不明文件，也不要把它们随意提交。AGENTS.md 的未提交差异属于其它来源，需要单独核其归属。
3. 先读取唯一 durable `lr_mv7j94y4_0b4532683719`。中间 checkpoint 与任务终态均保存，不能把原来已失败的运行结果写成成功。
4. 写入限制默认为用户指定项目目录；测试缓存、TEMP/TMP、构建输出均留在工程自己的临时目录。系统安装、远端 release、服务重启的授权按用户“最终发布和热更新重启”执行，但必须先核进程所有权与活动任务，不能误停其它正在运行的任务。

## 三、已经核实的 OAuth 故障
2026-10-14 的 WJP 公网 Gateway 日志有三轮：`/.well-known/oauth-protected-resource/mcp` HTTP 200，`/.well-known/oauth-authorization-server` HTTP 200，`POST /register` HTTP 201，`GET /authorize` HTTP 302；未观察到后续 `/consent` 或 `/token`。这表示 Google 可完成发现和 DCR，但不能单凭 302 判断正常授权还是错误重定向。
用户实际在 Gemini 里：Next → 两个 Google 风险/关联弹窗 → Agree and continue → `Account linking is required to use this custom app. Try again.`
隔离公网探针 `scripts/diagnose_gemini_oauth.py`：使用不含 query 的标准资源 URL，DCR 201、authorize 302（正常同意页）、同意页 GET 200；把查询参数附加到 resource 后，302 返回 `invalid_target`。现有 `oauth_provider.py` 将 OAuth resource 严格绑定至无 query 的完整 `/mcp` URL。这是有证据的差异，但不能推断 Google 真实请求一定发送了 query 版本。不要放宽资源绑定，也不要用 URL 查询凭据冒充 Gemini OAuth。
以前尝试 BrowserMCP Chrome 浏览器桥接时，`9009` 端口辅助程序出现 `killProcessOnPort / RangeError server.close` 上游问题；尚无真实 Gemini 成功连接验收。不能用本地模拟 OAuth 替代 Google 实测。可使用你实际已授权的 Chrome 自动化/浏览器功能完成实名登录态测试；不要导出 Chrome profile、Cookie、密钥或绕过 Google 登录。

## 四、已经存在的 0.9.0 源码候选
- `src/local_dev_mcp_bridge/desktop_main.py` 删除【连接客户端】与旧普通 Gemini 静态凭据区；添加【复制标准地址（Gemini）】和【复制 ChatGPT 免登录地址】，两者已做真实点击剪贴板验证，且不修改项目配置。
- `src/local_dev_mcp_bridge/models.py` 原 `client_target` 与 `gemini_redirect_uri` 保留兼容读取但不再决定运行客户端；旧配置可读取且保存保持。
- `src/local_dev_mcp_bridge/help_content.py` Gemini 最新 Customize/Connectors/Custom 教程与普通复制入口。
- `src/local_dev_mcp_bridge/tunnel_manager.py` 小范围 backport GRW 的 Cloudflare 双区域 UDP precheck/TCP 成功才建议 HTTP2 的机制，已有回归。
- `tests/test_v090_client_neutral.py` / `tests/test_v090_dual_client_runtime.py` / `tests/test_v090_tunnel_protocol.py`；单个网关内 DCR、authorize、code-to-token、refresh、revoke 与 GPT 旧链接继续可用有回归。
- 2026-10-14 最新针对性组合：`120 passed, 2 failed`（未排除任何目标）；上一个排除已知 UI 单测的组合 `115 passed, 1 deselected`；Ruff 已通过。完整仓库全量测试/双平台构建/正式安装另按最新执行回执检查，不套用旧版数字。

## 五、首要剩余缺口与准确位置
1. `desktop_main.py` 的 `_refresh_url_ui` 内目前把 `f"{url}  ·  {suffix}"` 填进只读标准地址框；改为纯 URL，把“固定配置/仅本机/临时地址”放独立状态控件或 tooltip，保证直接框选、双击复制也得到可用地址。已有 `test_v090_client_neutral.py::test_shared_connector_buttons_replace_per_project_client_selection` 测试先红。
2. `tests/test_phase11_desktop_ux.py::test_connection_info_exposes_encoded_no_auth_server_url_for_personal_connector` 旧断言期待按钮“复制 No Auth 地址”；现在 UI 已明确改为“复制 ChatGPT 免登录地址”，调整测试断言而不是倒退新 UX。保留原有旧 URL 实值、URL 编码与点击功能检查。
3. `tests/test_v090_oauth_redirect_diagnostics.py` 现为 RED：预期 `gateway.py::_classify_oauth_redirect`；按测试实现有界分类 `consent_page/oauth_error/client_callback/unrecognized/missing`。接入 `/authorize` 的 response-start 审计，仅保留种类和允许的标准错误枚举，不记录原始 Location、state、code、token、完整查询串、Cookie 或密钥；诊断失败不可阻断真实 MCP。现有 `gateway_diagnostics.py` 有脱敏+滚动日志，尽量复用。
4. 额外核实 `oauth_provider.py:167-187` 返回相对 `/consent?id=...` 是否在 Google 实际跳转里被正确处理。**不能仅因相对 URL 就猜是唯一根因。** 如果确认需要绝对地址，应使用服务器固定可信 issuer 构造，不能信任任意 Host header；必须先设计失败测试再改。
5. 在已登录 Gemini 页面用真正的**不带 query 的标准地址**走完整链接，关联同一时段脱敏日志，从登录页回到 Google 后见 `POST /token` 和随后 Header Bearer 的 MCP `initialize/tools/list`、只读调用；若某环节不能取得证据就标“未验证”，继续排查而不是宣称修复。
6. GRW 可复用改进只按差异和对应反例吸收：隧道恢复、句柄隔离、合理脱敏等；务必检查其短命令 `structuredContent` 是否丢掉用户可见 stdout，不能把状态码代替命令输出。GRW 的 `relay_control.py`、专用 `opencode-server` 配置、服务器任务及凭据属于已部署机器绑定能力，不要原样搬到 WJP。

## 六、实际验收、发布、热更新
- 修复上面 RED 后执行定向和负向测试、完整 Python pytest、Ruff、Windows/Linux Pyright、Node/CodexPro build+full smoke、发布版本和 lockfile 检查；Windows 与 Linux/SteamOS 的 CI、installer、归档资产必须来自同一冻结提交。禁止只用局部 115 passed 宣称 0.9.0 完工。
- GPT 原个人 URL 不变化、旧 plugin 无需删除重建；Gemini 标准 URL 的 DCR + PKCE + 授权 + refresh + revoke + 实际工具调用成功；两客户端同时访问同一个 Hub 的多工作区/设备路由不串线。无效 token、冲突 Header、错误资源、跨工作区等反例继续被正确处理。
- 构建与发布按仓库规范：建立新 `release/v0.9.0` 并经源码审查，冻结源 SHA、CI、annotated `v0.9.0` tag、provenance 和 Win/Linux SHA256，发布官方新 Release，绝不移动或覆盖已有发布 tag/资产。不要使用带未提交用户文件的源。
- WJP 升级前枚举现有所有根/已打开工作区的 running/cancelling 后台任务并等待排空；准确记录原 Gateway/项目/管理员服务进程和安装来源。用已验证正式资产的标准 detached upgrade 执行一次升级，核自动 resume、原项目恢复、公开域名、本机/公网 MCP、旧 GPT 原连接和 Gemini 实机。
- 最后 GRW 用**同一发布资产**升级；事前保护它当前 0.8.9.9 的 relay、本机 SSH 受控任务、服务器 unit、权限、所有项目、代理和安装标识，避免切换网络模式。完成活跃任务检查才重启；安装后同样跑双客户端数据面、发布 SHA、项目状态验证。两机任一失败按各机原资产与配置独立回滚。
- 完成事项迁至 `进度验收.md`，剩余事项保留 `开发计划.md`；原 durable 逐步更新、最终有全部 PASS review 才 complete。最终回复严格区分：实际代码修改、测试通过、尚未实现、Git commit/push、Release/tag/CI、WJP 安装、GRW 安装及热重启的具体证据。禁止发布不满足验收的版本。

## 七、接力实施提醒
优先修复两个精确 UI 红测和 Gateway 脱敏诊断，然后利用浏览器作真实 Gemini 验收；上游接口有动态变动时查 Google 官方最新文档和 MCP 2026-07-28 规范。不要新增主人二次身份核验，不为了实现“同一字符串 URL”而削弱规范资源绑定。现有源不得因 GRW 版本较新就整库覆盖；也不要先重启当前在承载多个聊天项目的 WJP/GRW MCP，必须先有构建/Release/任务 drain/回滚证据。
