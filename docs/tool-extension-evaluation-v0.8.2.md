# MCP DevBridge v0.8.2 工具扩展评估

> 目标：提高 ChatGPT / 本地 Agent 的开发、检索、调试与交付质量；同时控制工具 schema、依赖体积、凭据面与上下文噪声。调研时间：2026-08-21。

## 结论

v0.8.2 不应把更多大型 MCP Server 静态常驻到同一工具列表。DevBridge 已覆盖项目文件、语义仓库分析、Git、本地命令、后台任务、Windows 控制、多工作区/多设备路由；继续静态叠加同类工具会增加选工具歧义与上下文成本。

后续扩展采用“缺口能力 + 按需启用 + 动态发现”的原则：默认工具面保持精简；只有任务需要时才加载外部能力。优先级如下。

## P0：建议下一版本优先接入

### Context7 MCP — 实时开发文档

- 仓库：https://github.com/upstash/context7
- 价值：为框架/SDK/API 查询提供与版本更接近的官方文档上下文，减少模型使用陈旧 API 或幻觉接口。
- 适合 DevBridge：远程 MCP，几乎不增加本地发行体积；与现有文件/Git/Shell 能力互补。
- 接入策略：按需 profile 启用；API Key 存 SecretsStore；默认不常驻 tools/list。

### GitHub MCP Server — 远端仓库协作

- 仓库：https://github.com/github/github-mcp-server
- 价值：补齐本地 Git 之外的 Issues、Pull Requests、Actions/CI、远端仓库元数据与代码评审工作流。
- 适合 DevBridge：当前本地 Git 工具不能替代 GitHub API 层。
- 接入策略：使用最小权限 token；优先借鉴官方 `tool-search` 做动态工具发现，而不是一次暴露全部 GitHub 工具。

## P1：按场景启用

### Playwright MCP — 浏览器端到端验证

- 仓库：https://github.com/microsoft/playwright-mcp
- 价值：网页交互、表单、可访问性快照、端到端 UI 验收。
- 约束：工具数量与页面快照会消耗上下文；对于纯编码任务，Playwright 官方也建议考虑 CLI + Skills 以减少 MCP schema/快照成本。
- 接入策略：只在 Web/UI 项目 profile 开启；不与 Chrome DevTools MCP 同时默认启用。

### Chrome DevTools MCP — 前端调试与性能分析

- 仓库：https://github.com/ChromeDevTools/chrome-devtools-mcp
- 价值：Console/Network/Performance/DevTools 级调试，适合定位前端性能、请求和浏览器运行时问题。
- 接入策略：与 Playwright 二选一按任务加载；E2E 自动化优先 Playwright，性能/网络诊断优先 DevTools。

### MarkItDown MCP — 文档转 Markdown

- 仓库：https://github.com/microsoft/markitdown
- 价值：把 Office/PDF/HTML 等内容转换成更适合 LLM 消费的 Markdown，提升资料阅读和产出链路。
- 约束：`markitdown[all]` 会增加 Python 依赖；默认服务无认证并建议仅绑定 localhost。
- 接入策略：可选组件，不进入 v0.8.2 核心包；只允许 localhost，并复用 DevBridge 路径/权限边界。

## 不建议默认接入

### Serena

- 仓库：https://github.com/oraios/serena
- 优点：LSP/符号级语义检索、编辑与重构能力很强。
- 当前决定：不默认接入。DevBridge 内置 CodexPro 已有仓库分析、symbol/reference/impact 搜索、精确编辑、Git 与后台任务；Serena 自身也会在已有 Agent harness 中关闭重复的基础文件/Shell 工具。此处重复度过高。

### MCP reference servers

- 仓库：https://github.com/modelcontextprotocol/servers
- 用途：参考实现和协议示例。
- 当前决定：不把 reference server 当生产依赖直接捆绑。Fetch/Filesystem/Git 等多数能力已被 DevBridge 覆盖；官方也将这些仓库定位为参考/示例性质。

## 推荐架构：动态工具发现，而不是继续堆工具

后续建议新增 `ToolProfile / ToolRegistry`：

1. 默认只暴露 DevBridge 核心工具。
2. 外部工具按 `docs / github / browser / document` profile 注册。
3. 先用一个轻量 discovery/search 入口检索可用能力，再临时挂载目标工具集。
4. 每个 profile 有独立凭据、权限、超时、日志与健康检查。
5. 同类工具互斥或降权，避免 File/Git/Shell/Browser 的重复 schema。
6. 生产默认配置不自动联网安装第三方 MCP；由用户明确启用后再安装/连接。

这比把几十到上百个工具全部常驻 `tools/list` 更符合 DevBridge“长任务稳定、低上下文噪声、可审计”的目标。
