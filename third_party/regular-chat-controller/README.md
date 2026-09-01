# Regular Chat Controller（实验资产）

这是 MCP DevBridge v0.9.0 研发线中的 **隔离实验组件**，用于验证浏览器会话所有权、turn exactly-once、completion detection、故障恢复和资源上限。它不是 v0.8.9 正式运行链的一部分，也不被默认桌面、CLI、wheel、PyInstaller、Windows/Linux build 或 installer 引用。

This package is an **unreleased experimental implementation** and test asset. It **must not be used** as MCP DevBridge's production ChatGPT continuation path unless the documented host-capability, policy, live-recovery, review, and release gates are all satisfied.

## 当前实现

- Node.js / TypeScript stdio sidecar。
- Playwright persistent context，使用 DevBridge 独立 profile；不复用用户日常 Chrome/Edge profile。
- 一个 durable run 对应一个 owned tab 和一个 conversation identity。
- provider-session schema v2：只保存 identity、hash、send/response state 与发送前 assistant turn count，不保存完整 prompt/response 或认证数据。
- `never_sent → send_started → send_confirmed/ambiguous` 单调状态机；有副作用 turn 无法证明时 fail-closed。
- 多信号 completion detector：新 assistant turn、generation control、composer、final controls、内容 hash/stable window 与网络状态共同判定。
- tab、owned browser、controller restart 与短网络降级的同会话恢复；duplicate send 必须为 0。
- stdio JSON-lines RPC 有请求大小、排队、超时和响应顺序约束。
- profile lock 使用独立的有限等待 recovery guard；不会删除 Chromium/Chrome/Edge 的 `SingletonLock`。

实验 Python supervisor、CLI/UI 原型和历史测试位于：

```text
experimental/regular-chat-bridge/python-prototype/
```

它们被刻意放在正式 `src/local_dev_mcp_bridge` 包之外，只作为归档原型与设计证据保留，不进入正式测试收集或构建链。

## 安全与隐私边界

- 不读取、打印、提交或持久化 cookie、Authorization header、access/refresh token、密码或浏览器认证数据库。
- profile、browser binary、session、lock、日志、trace 与 test result 是本机运行态，并被 Git 忽略。
- 登录、验证码、MFA、安全确认、付费或权限确认必须由用户本人完成。
- conversation URL 只允许存在于本机 provider-session；普通诊断仅显示短 hash。
- selector 全部集中在 adapter；全部 selector 失效时暂停，不静默切换新 chat。
- 本组件不得用于替代未协商的 MCP Tasks，也不得被当作 ChatGPT host 已能自主创建下一轮 assistant turn 的证据。

## 精确锁定的工具链

- Node.js 22+
- TypeScript 5.9.2
- Playwright 1.62.1
- `@bybrave/proper-lockfile2` 5.0.0

Playwright 浏览器 binary 不随 npm 包安装。实验 fixture 使用 `PLAYWRIGHT_BROWSERS_PATH` 指向 DevBridge 当前用户目录中的独立缓存。

## 命令

```text
npm ci --ignore-scripts
npm run typecheck
npm run build
npm run test:unit
npm run test:fixture
npm test
```

`test:unit` 不访问网络；`test:fixture` 同样不访问外网，但需要安装与 Playwright 1.62.1 对应的 Chromium revision。浏览器缺失时测试明确失败，不会 silently skip。

## 正式发布门

实验源码可以保留并继续测试，但以下条件满足前不得进入正式产品：

1. 当前目标客户端明确协商受支持的异步 host/task capability。
2. 服务条款和产品边界允许相应自动化行为。
3. 真实 Windows 测试证明同 conversation 多轮、browser/controller/DevBridge restart、短网络故障和长会话均无重复发送或身份漂移。
4. 独立 review 对最新 revision 给出 PASS。
5. 正式 wheel/installer 内容审计明确包含该能力，并提供 feature/capability gate 与回滚路径。
