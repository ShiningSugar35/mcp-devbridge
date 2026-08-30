# MCP DevBridge

MCP DevBridge 是一个面向本地开发场景的桌面桥接工具，用于把用户明确授权的本机目录通过 Model Context Protocol（MCP）提供给 ChatGPT、Gemini 等支持 MCP 的客户端。

它负责本地工具、权限、路由、认证和连接管理，**不提供模型推理，也不调用 OpenAI / Gemini 模型 API**。模型侧的可用功能、确认流程、配额和套餐限制仍由对应客户端平台决定。

## 主要能力

- **多项目根同时在线**：可同时启动多个目录或磁盘根，按 `path`、`cwd`、后台任务和 workspace handle 自动路由到正确项目。
- **明确的权限边界**：提供只读、项目工作区和完全访问等模式；路径访问使用 canonical/real-path 语义，防止 `..`、symlink/junction 等越界。
- **统一 MCP Hub**：Local 模式和公网模式都通过共享 Gateway 暴露稳定的 MCP 入口，不要求客户端先选择“入口项目”。
- **公网连接**：支持 Cloudflare Named Tunnel、ngrok、Quick Tunnel 等方式；本地引擎和 Gateway 默认只监听 loopback。
- **OAuth / Bearer 认证**：公网连接通过 Gateway 认证，敏感凭据使用系统安全存储或受保护的本地 fallback，不写入普通配置文件和日志。
- **后台任务与耐久长任务**：长命令通过后台 task 执行；多阶段工作可使用 durable long-run 状态机保存步骤、证据、审查和恢复状态。
- **连接诊断与局部恢复**：项目引擎、Gateway 和 Tunnel 分层探测并按责任层恢复，避免单个项目故障触发无关组件的全局重启。
- **跨平台发行链**：Windows 10/11 为主要桌面平台，同时提供 Linux / SteamOS Desktop Mode 的用户目录安装与构建支持。

## 工作方式

```text
ChatGPT / Gemini / MCP Client
            │
            │ Streamable HTTP / MCP
            ▼
     OAuth / Bearer Gateway
            │
            ├── active root A ──> CodexPro
            ├── active root B ──> CodexPro
            ├── active root N ──> CodexPro
            │
            └── optional remote device
```

所有处于运行状态的项目根地位平等。一次工具调用会根据实际路径、cwd、task affinity 或显式 workspace handle 选择目标项目；如果证据不足且存在歧义，系统应明确失败，而不是猜测目标根。

更完整的运行架构见 [`docs/en/ARCHITECTURE.md`](docs/en/ARCHITECTURE.md)。

## 安装

### Windows

1. 打开 [GitHub Releases](https://github.com/ShiningSugar35/mcp-devbridge/releases/latest)。
2. 下载最新稳定版 `MCPDevBridge-Setup-<version>.exe`。
3. 运行安装程序并选择安装目录。
4. 启动 MCP DevBridge。

正式 Windows 包包含运行所需的私有 Node.js、uv/uvx 和 cloudflared 运行时，不要求把这些组件额外加入系统 `PATH`。

### Linux / SteamOS Desktop Mode

从 [GitHub Releases](https://github.com/ShiningSugar35/mcp-devbridge/releases/latest) 下载：

```text
MCPDevBridge-Linux-x86_64-<version>.tar.gz
```

解压后运行随包提供的安装脚本：

```bash
tar -xzf MCPDevBridge-Linux-x86_64-<version>.tar.gz
cd MCPDevBridge
./install.sh
```

默认安装到：

```text
~/.local/opt/MCPDevBridge
```

也可以指定其它用户可写目录：

```bash
./install.sh --target-dir "$HOME/Applications/MCPDevBridge"
```

SteamOS 建议在 Desktop Mode 下使用用户目录安装，不修改只读系统基座。

平台兼容细节见 [`docs/en/COMPATIBILITY.md`](docs/en/COMPATIBILITY.md)。

## 快速开始

1. 启动 MCP DevBridge。
2. 添加要授权的目录。需要让某个磁盘下的子目录都可路由时，可以直接添加磁盘根，例如 `C:\` 或 `D:\`。
3. 为项目选择权限模式和连接方式。
4. 启动一个或多个项目，或使用“启动所有项目”。
5. 在桌面应用中获取 MCP 连接信息，并配置到支持 MCP 的客户端。
6. 后续调用直接使用目标绝对路径，或继续使用已经获得的 workspace handle；通常不需要手工切换“当前项目”。

例如同时启动：

```text
C:\
D:\
```

则这些路径都可以由同一个 Hub 自动路由：

```text
C:\Users\...\project
C:\Program Files\...
D:\Environment\mcp
D:\other-project
```

## 连接方式

| 模式 | 说明 | 典型用途 |
|---|---|---|
| Cloudflare Named Tunnel | 固定公网域名 | 长期主 Hub |
| ngrok 固定域名 | 固定 ngrok hostname | 已有 reserved domain 的环境 |
| Quick Tunnel | 临时随机公网地址 | 测试或临时设备连接 |
| Local | 仅本机 loopback | 无公网暴露的本地开发 |

公网模式只暴露经过认证的 Gateway；CodexPro、可选 Windows bridge 等内部服务保持本机监听。

## 权限与安全

MCP DevBridge 能够向远端 MCP 客户端开放高权限的本地开发能力。请只添加你愿意授权的目录，并根据实际需求选择最低必要权限。

核心安全边界包括：

- workspace/read-only 模式下使用 canonical path containment 限制文件访问范围；
- 防止 `..`、symlink/junction 等路径逃逸；
- 公网入口要求 OAuth/Bearer 认证；
- 凭据和 token 不写入普通日志、URL 或项目配置；
- 日志中的 command、content、patch 和 secret-like 字段会经过脱敏；
- 已知格式化磁盘、破坏引导、递归删除系统目录等高风险命令受硬限制；
- Windows 管理员能力使用受控的系统授权机制，不通过关闭或绕过 UAC 获得权限。

完整威胁模型、凭据存储和权限说明见 [`docs/en/SECURITY.md`](docs/en/SECURITY.md)。

## 更新

桌面应用可检查 GitHub 的稳定 Releases，并在存在可安装的新版本时执行升级。发布资产、版本元数据和校验值应来自同一个正式发布提交。

具体版本变化不在 README 中维护；请查看 [`docs/en/CHANGELOG.md`](docs/en/CHANGELOG.md) 和 GitHub Releases。

## 开发

### 环境

- Python 3.12+
- Node.js / npm
- `uv`
- Windows 发布构建需要 Inno Setup 6

### 本地开发

```powershell
uv venv --python 3.12
uv pip install -p .venv -e ".[dev,package]"

cd third_party/codexpro
npm ci
npm run build
cd ../..

.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m pyright --pythonpath .venv\Scripts\python.exe src tests
.venv\Scripts\python.exe -m pytest tests -q
```

Windows 完整构建可使用：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1
```

完整开发、测试和跨平台构建流程见 [`docs/en/DEVELOPMENT.md`](docs/en/DEVELOPMENT.md)。

## 文档

- [`docs/en/ARCHITECTURE.md`](docs/en/ARCHITECTURE.md) — 运行架构与路由模型
- [`docs/en/SECURITY.md`](docs/en/SECURITY.md) — 安全、权限和凭据模型
- [`docs/en/COMPATIBILITY.md`](docs/en/COMPATIBILITY.md) — Windows / Linux / SteamOS 兼容性
- [`docs/en/DEVELOPMENT.md`](docs/en/DEVELOPMENT.md) — 开发、测试和构建
- [`docs/en/LONG_RUNNING_TASKS.md`](docs/en/LONG_RUNNING_TASKS.md) — durable long-run 使用约定
- [`docs/en/CHANGELOG.md`](docs/en/CHANGELOG.md) — 版本历史

## 问题反馈

如果发现连接、安装、兼容或安全问题，请在 GitHub Issues 中提供可复现步骤、平台版本和必要的脱敏日志。请勿公开提交 token、OAuth code、访问凭据或其它敏感信息。

## License

本项目许可证见 [`LICENSE`](LICENSE)。第三方组件及许可证信息见 [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md)。
