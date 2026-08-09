# MCP DevBridge

> **让 ChatGPT、Gemini Spark 等支持 MCP 的网页端，直接连接你的本地开发项目。**

MCP DevBridge 是一款面向 Windows 的可视化本地开发桥接工具。  
你在桌面选择一个项目目录并启动服务后，ChatGPT、Gemini Spark 等 MCP 客户端就可以通过一个固定的 HTTPS MCP 地址读取项目、搜索代码、修改文件、执行测试和 Git 操作。

它本身**不调用任何大模型 API**，也不需要你为 MCP DevBridge 配置 OpenAI / Gemini 模型 API Key。  
如果你已经在使用 ChatGPT、Gemini 等网页端订阅，又不希望为了本地 Coding Agent 额外长期购买大量 API Token，MCP DevBridge 提供了一条更直接的路径：

```text
ChatGPT / Gemini Spark
          │
          │ MCP over HTTPS
          ▼
     MCP DevBridge
          │
    ┌─────┴─────┐
    ▼           ▼
CodexPro     Windows-MCP
项目开发       可选系统控制
    │
    ▼
你的本地项目
```

> [!IMPORTANT]
> MCP DevBridge 只是本地工具桥接层，不提供模型推理，也不会绕过 ChatGPT、Gemini 或其他平台自身的套餐、额度、工具权限和安全确认规则。  
> 网页端是否允许写操作、是否要求确认、以及相关使用如何计入平台额度，都以对应平台当前规则为准。

---

## 为什么做这个项目？

很多 AI Coding Agent 能力很强，但通常需要单独购买 API Token。大型项目持续开发时，模型调用成本可能很高。

另一方面，很多用户已经订阅了 ChatGPT 或 Gemini，但网页端默认无法直接：

- 读取本地项目文件；
- 搜索整个代码仓库；
- 修改代码并应用 Patch；
- 执行测试、构建和 Git；
- 启动或停止本地开发进程。

MCP DevBridge 解决的就是中间这一层：

**把支持 MCP 的网页端 AI，安全地连接到你的本地开发环境。**

你不需要自己手写 MCP Server、HTTP 网关、OAuth、Cloudflare Tunnel、项目权限管理和桌面控制程序。

---

## 主要功能

- **可视化项目选择**  
  在桌面窗口直接选择需要开发的本地项目，无需手写路径配置。

- **固定 MCP 地址**  
  支持 Cloudflare Named Tunnel，例如：

  ```text
  https://mcp.example.com/mcp
  ```

  正常重启程序或切换项目后，公网地址不需要改变。

- **ChatGPT + Gemini Spark**  
  同一个 MCP DevBridge 可用于多个支持远程 MCP 的客户端。

- **本地文件开发能力**  
  支持读取、搜索、写入、编辑、Patch、目录操作等开发工作流。

- **Git 与命令执行**  
  支持 Git status / diff / commit / push，以及 PowerShell、测试、构建和长期进程管理。

- **OAuth + Bearer 认证**  
  公网入口支持 MCP OAuth 流程，同时保留 Bearer Token 兼容路径。

- **权限模式**  
  支持：
  - 只读
  - 项目工作区
  - 系统级权限

- **可选 Windows 控制能力**  
  可通过 Windows-MCP 扩展桌面操作、应用控制、PowerShell、文件系统等能力。

- **审计与脱敏**  
  提供工具调用日志、进程日志，并对 Token、Secret、Password 等敏感值进行脱敏。

- **桌面化运行**  
  PySide6 单窗口界面，一键启动 / 停止本地引擎、OAuth Gateway 和 Tunnel。

---

# 快速开始

## 方式一：安装 Windows 安装包（推荐）

普通用户建议直接使用 GitHub Releases 中的安装包，不需要自己搭 Python 项目环境。

### 1. 下载

打开：

**[GitHub Releases](https://github.com/ShiningSugar35/mcp-devbridge/releases)**

下载最新版本的：

```text
MCPDevBridge-Setup-x.x.x.exe
```

> 如果 Releases 页面暂时还没有安装包，可以使用下面的“从源码运行”方式。

### 2. 安装并启动

安装完成后打开：

```text
MCP DevBridge
```

### 3. 选择项目

在主界面选择你希望 ChatGPT / Gemini 操作的项目文件夹，例如：

```text
D:\Projects\my-project
```

建议第一次使用时选择：

```text
项目工作区
```

权限模式。

### 4. 选择连接方式

如果只是本机调试，可以使用本地模式。

如果希望 ChatGPT / Gemini 网页端长期连接，推荐：

```text
Cloudflare 固定地址
```

然后填写：

- 固定域名，例如 `mcp.example.com`
- Cloudflare Named Tunnel Token

### 5. 点击启动

启动成功后，程序会显示固定 MCP 地址：

```text
https://mcp.example.com/mcp
```

### 6. 在网页端添加 MCP

将这个地址添加到 ChatGPT 或 Gemini Spark 的自定义 MCP / Connected App 中。

之后正常使用时通常只需要：

```text
打开 MCP DevBridge
→ 选择项目
→ 点击启动
→ 去 ChatGPT / Gemini 开发
```

固定地址模式下，不需要每次重新生成 MCP URL。

---

# Cloudflare 固定地址部署

如果你希望网页端长期使用同一个 MCP 地址，推荐使用 Cloudflare Named Tunnel。

## 需要准备

- 一个 Cloudflare 账号；
- 一个托管在 Cloudflare 的域名；
- 一个 Named Tunnel；
- 一个固定子域名，例如：

```text
mcp.example.com
```

## 推荐结构

```text
https://mcp.example.com
          │
          ▼
Cloudflare Named Tunnel
          │
          ▼
127.0.0.1:8786
MCP DevBridge OAuth Gateway
          │
          ▼
127.0.0.1:8787
Local MCP Engine
```

当前 OAuth Gateway 默认监听：

```text
127.0.0.1:8786
```

因此 Cloudflare Public Hostname 的 Service 应指向：

```text
http://localhost:8786
```

MCP Endpoint 为：

```text
https://mcp.example.com/mcp
```

## 端口配置

- **公网入口端口（Gateway）**：默认 `8786`。在桌面主界面的
  “访问令牌与 MCP 地址”区域可以修改、检测占用、恢复默认；
  修改后**必须同步**将 Cloudflare Tunnel 的 Service URL 改为
  `http://localhost:<新端口>`，否则公网连接会失败（界面会醒目提示）。
- **内部组件端口**：CodexPro `8787`、Windows-MCP `28731`、Legacy backend `8765`。
  通过主界面「高级设置…」可改，默认无需操作；设置永久保存，服务运行期间锁定。
- 所有端口仅监听 `127.0.0.1`；启动前会检查端口占用，被占用时提示处理，
  不会偷偷改端口。
- 端口默认值集中在 `src/local_dev_mcp_bridge/constants.py` 的 `DEFAULT_*_PORT`。

正常情况下：

```powershell
curl.exe -i https://mcp.example.com/mcp
```

在没有认证信息时返回 `401 Unauthorized`，通常意味着：

```text
DNS
→ Cloudflare
→ Tunnel
→ 本地 Gateway
→ MCP 认证层
```

这条公网链路已经打通。

> [!WARNING]
> Cloudflare Tunnel Token、MCP Bearer Token、OAuth Client Secret 都属于敏感凭据。不要提交到 Git，不要粘贴到公开聊天或 Issue 中。

---

# ChatGPT 连接

在支持自定义 MCP 的 ChatGPT 环境中：

1. 启用相应的 Developer / MCP 功能；
2. 新建自定义 MCP App；
3. Server URL 填：

   ```text
   https://mcp.example.com/mcp
   ```

4. 根据当前 ChatGPT 界面配置认证；
5. 扫描并启用需要的 Actions / Tools；
6. 新建会话开始使用。

示例任务：

```text
读取当前项目的 AGENTS.md 和 README.md，
检查 Git 状态，
然后根据项目约束定位当前未完成任务。
不要修改代码，先给我开发计划。
```

或者：

```text
阅读相关代码并修复这个 bug。
修改后运行测试，
再用 Git diff 检查改动。
```

> ChatGPT 对 MCP 写操作、工具刷新、Action 快照以及确认机制的支持可能随套餐和产品版本变化。  
> “Allow all actions” 只会影响已经暴露给 ChatGPT 的工具，并不会替 MCP Server 自动新增工具。

---

# Gemini Spark 连接

MCP DevBridge 提供 OAuth Gateway，可用于 Gemini Spark 的 Custom Connected App。

一般流程：

1. 在 Gemini Spark 中添加自定义 Connected App；
2. MCP Server URL 填：

   ```text
   https://mcp.example.com/mcp
   ```

3. 按 Gemini 当前界面完成 OAuth / Client 配置；
4. 浏览器会打开 MCP DevBridge 授权页；
5. 确认项目目录和授权范围；
6. 点击允许；
7. 返回 Gemini Spark 使用。

MCP DevBridge 对外身份为：

```text
mcp-devbridge
```

显示名称：

```text
MCP DevBridge
```

---

# 权限模式

## 只读

适合第一次连接或代码审查。

允许：

- 查看项目；
- 读取文件；
- 搜索代码；
- Git 只读操作。

不允许直接修改项目。

## 项目工作区

**推荐日常开发使用。**

允许在当前选中的项目范围内：

- 读取和搜索；
- 写文件；
- Edit / Patch；
- Git；
- 受控命令执行；
- 测试和构建；
- 本地开发进程管理。

## 系统权限

允许更高风险的系统级能力。

如果启用了 Windows-MCP，AI 还可能获得：

- PowerShell；
- 应用控制；
- 文件系统；
- 进程管理；
- 注册表；
- Windows UI 自动化等能力。

> [!NOTE]
> Windows 桥接工具按权限档位过滤：`项目工作区 / 只读` 下只放行
> `desktop_ui` 白名单（点击、输入、快照、应用等 UI 操作），
> PowerShell / 注册表 / 文件系统等系统级工具会被拒绝；
> 仅 `系统权限` 模式放行全部工具（`system_full`）。
> 每次调用还会与桥端实时工具清单（inventory）交叉校验。

> [!CAUTION]
> 系统权限意味着远程 AI 工具可能影响项目目录之外的电脑状态。  
> 除非确实需要，否则优先使用“项目工作区”。

---

# 命令执行档位（Shell Execution Profile）

执行档位与权限模式正交：权限模式决定**能触碰的范围**，执行档位决定**命令如何跑**。
对 `run_command / run_program / start_process`（本机工具）与 Codex 引擎的 bash 工具同时生效：

| 档位 | 行为 | 适用场景 |
|---|---|---|
| `developer`（默认） | 命令首词必须是开发工具（pytest / pyright / ruff / mypy / git（完整子命令）/ npm / pnpm / yarn / bun / uv / python / tsc / eslint / cargo …）；危险命令硬拦截 | 通用开发工作流：AI 可运行测试、类型检查、lint、git 操作 |
| `safe` | 完全保留原有“项目内命令允许”行为（仅危险拦截；引擎端仍执行其安全 allowlist） | 需要保持旧行为的场景 |
| `full_system` | 任意命令；启用前需要**一次性风险确认**（桌面首次提示） | 完全受信任的 AI 客户端 |

**危险命令在任何档位都被硬拦截**（白名单无法绕过）：磁盘格式化（`format C:`）、
分区操作（`diskpart`）、关机重启（`shutdown/reboot`）、引导配置（`bcdedit`）、注册表删除
（`reg delete`）、`msiexec / cipher / takeown / icacls`，以及递归删除指向盘根或系统目录的
`rm -rf /`、`del /s C:\`、`Remove-Item -Recurse C:\Windows` 等。

Shell 选择：默认顺序 pwsh > Windows PowerShell > cmd > Git Bash，**WSL Bash 永不当默认**
（它运行 Linux 工具链，无法保证执行 Windows 项目脚本）。桌面提供「开发环境检测」按钮，
可一键确认 Shell / python / git / pytest / pyright 是否可调用（对应 MCP 工具 `shell_self_test`）。

---

# 从源码运行

## 环境要求

基础开发环境：

- Windows 10 / 11 x64
- Python 3.12
- Node.js 20+
- Git
- uv
- npm

固定公网连接还需要：

- `cloudflared`

Windows 系统控制为可选能力，依赖 Windows-MCP 及其对应运行时要求。

## 1. Clone

```powershell
git clone https://github.com/ShiningSugar35/mcp-devbridge.git
cd mcp-devbridge
```

## 2. 创建 Python 环境

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip uv
.\.venv\Scripts\uv.exe pip install -e ".[dev,package]"
```

## 3. 构建 CodexPro 引擎

```powershell
cd third_party\codexpro
npm ci
npm run build
npm run smoke
cd ..\..
```

## 4. 准备 cloudflared

如果需要 Cloudflare Tunnel，将官方 `cloudflared.exe` 放到：

```text
.tools\cloudflared.exe
```

如果只使用本机模式，可以暂时跳过。

## 5. 启动桌面程序

```powershell
.\.venv\Scripts\python.exe -m local_dev_mcp_bridge.desktop_main
```

---

# 开发与测试

运行 Python 测试：

```powershell
$env:PYTHONIOENCODING="utf-8"
.\.venv\Scripts\python.exe -m pytest tests -q
```

Ruff：

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
```

Pyright：

```powershell
.\.venv\Scripts\pyright.exe
```

CodexPro：

```powershell
cd third_party\codexpro
npm ci
npm run build
npm run smoke
```

构建 Windows 程序前，应先确保 CodexPro 的：

```text
third_party/codexpro/dist
third_party/codexpro/node_modules
```

均已经生成。

---

# 项目架构

```text
┌─────────────────────────────────────┐
│          MCP DevBridge GUI          │
│              PySide6                │
│                                     │
│  项目选择 / 权限 / Tunnel / 日志     │
└─────────────────┬───────────────────┘
                  │
                  ▼
        ┌───────────────────┐
        │ ServiceCoordinator│
        └───────┬───────┬───┘
                │       │
                │       └───────────────┐
                ▼                       ▼
        CodexPro Engine             Windows-MCP
        项目 Coding 能力             可选系统能力
                │
                └──────────┬────────────┘
                           ▼
                    OAuth Gateway
                     127.0.0.1
                           │
                           ▼
                  Cloudflare Tunnel
                           │
                           ▼
               ChatGPT / Gemini Spark
```

更详细的实现说明：

- [Architecture](docs/en/ARCHITECTURE.md)
- [Security](docs/en/SECURITY.md)
- [Compatibility](docs/en/COMPATIBILITY.md)
- [Development](docs/en/DEVELOPMENT.md)
- [Changelog](docs/en/CHANGELOG.md)

中文开发文档：

- `项目架构.md`
- `AGENTS.md`

---

# 安全说明

MCP DevBridge 的目标就是让远程 AI 能够操作本地开发环境，因此安全边界非常重要。

当前设计包括：

- MCP 引擎只监听 `127.0.0.1`；
- 公网入口经过 Cloudflare Tunnel；
- 公网请求需要 OAuth 或有效 Bearer；
- Token 使用 Windows Credential Manager / DPAPI 保存；
- 认证比较使用 constant-time comparison；
- 失败认证有限速；
- 日志对 Token / Secret / Password / Cookie 等字段脱敏；
- Workspace 模式限制项目文件访问范围；
- 高风险工具提供 destructive metadata；
- Windows-MCP 仅作为本地可选桥接后端，并按权限档位过滤工具（`desktop_ui` 白名单 / `system_full`）；
- 端口默认值集中维护（`constants.DEFAULT_*_PORT`），启动前检查占用，不做静默换端口。

建议：

1. 不使用时停止 MCP DevBridge；
2. 不公开分享 MCP URL + 凭据；
3. 默认使用“项目工作区”；
4. 定期轮换 Bearer / OAuth 凭据；
5. 不要把 `.env`、SSH Key、Cookie、Tunnel Token 提交到 Git。

详见：

[SECURITY.md](docs/en/SECURITY.md)


---

# 上游项目与致谢

MCP DevBridge 是一个独立的桌面集成项目，本项目构建在以下优秀的开源项目之上：

### CodexPro

- Upstream: `rebel0789/codexpro`
- 用途：本地项目 Coding MCP Engine
- License: MIT

MCP DevBridge 内维护了一个受控 fork，用于增加 Windows Bridge 等集成功能。

### Windows-MCP

- Upstream: `CursorTouch/Windows-MCP`
- 用途：可选 Windows 系统与 UI 控制后端
- License: MIT

详细版本、修改和第三方许可证：

[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)

---

# 当前状态

**MCP DevBridge 目前处于早期 Beta 阶段。**

已经完成的核心路径包括：

- Windows GUI
- 本地项目选择
- CodexPro Coding Engine
- 文件 / Git / 命令 / 进程工具
- Cloudflare 固定 MCP URL
- Bearer 认证
- MCP OAuth Gateway
- Gemini Spark 接入
- ChatGPT MCP 接入
- Windows-MCP 可选桥接
- 日志与审计
- PyInstaller / Inno Setup 打包

欢迎提交 Issue、Bug 报告和兼容性测试结果。

---

# License

MCP DevBridge 项目代码按仓库声明的许可证发布。

第三方组件保留各自原始许可证与版权声明。  
请参阅：

[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)
