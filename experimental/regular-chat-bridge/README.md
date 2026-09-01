# Regular Chat Bridge 实验区

本目录保存 v0.9.0 研发中形成、但未进入正式 MCP DevBridge 产品面的 Regular Chat 研究，以及 `python-prototype/` 中的 Python supervisor/CLI/UI 历史原型与测试资产。

正式边界：

- 默认桌面入口、正式 CLI、wheel、PyInstaller、Windows/Linux build 与 installer 不引用本目录。
- Node 控制器源码位于 `third_party/regular-chat-controller/`，同样不在正式构建链中。
- 本目录不得保存浏览器 profile、cookie、token、完整认证状态、真实账号输出日志或 Playwright browser binary。
- 当前 ChatGPT Connector 未协商 native MCP Tasks；本实验不能被表述为正式 host 无人值守续轮能力。

已验证的状态机、fixture 和恢复算法可在未来受支持的 host capability 出现后复用。正式决策与平台阻断见仓库根目录 `开发计划.md`、`进度验收.md` 和 `项目架构.md`。
