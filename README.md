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
  在桌面窗口直接选择需要开发的本地项目，无需手写路径配置。支持**多项目并行**运行，每个项目拥有独立的 CodexPro 引擎进程和端口。

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

- **按项目独立配置**
  每个项目独立记忆权限、客户端类型、连接方式、端口、Git 参数、访问令牌、Cloudflare Token 与 Gemini Redirect URI；切换项目不会互相覆盖。

- **四种连接方式**
  Cloudflare Named Tunnel、ngrok 固定域名、Quick Tunnel 临时地址、仅本机均可直接在桌面选择。

- **权限模式**  
  支持只读、项目工作区、完全访问。桌面端默认选择 **完全访问（危险）**（`system + full_system`），首次实际启动仍需要一次性风险确认。

- **状态与诊断**
  项目表 1 秒级刷新真实状态；控制区展示 Codex / Gateway / Tunnel / Windows 桥组件状态，并提供一键连接诊断和真实 MCP 自测。

- **可选 Windows 控制能力**  
  可通过 Windows-MCP 扩展桌面操作、应用控制、PowerShell、文件系统等能力。

- **审计与脱敏**  
  提供工具调用日志、进程日志，并对 Token、Secret、Password 等敏感值进行脱敏。

- **桌面化运行**  
  PySide6 单窗口界面，一键启动 / 停止本地引擎、OAuth Gateway 和 Tunnel。

---


## v0.8.0 单域名 Hub 路由与桌面可靠性

- **一个固定域名、一个 OAuth App、任意设备/工作区**：OAuth 授权页不再绑定单个工作区。ChatGPT 只连接主 Hub 的固定 URL，连接后用 `devbridge_switch_device` / `devbridge_switch_workspace` 按会话切换目标。
- **自动默认**：只有一个在线设备或一个运行工作区时自动选择；本机同时运行多个项目时优先使用当前公网入口项目，避免每次连接都询问。
- **禁止同一 Named Tunnel 多机复用**：主 Hub 的 Cloudflare Tunnel Token 只允许主 Hub 使用。远端设备使用 Quick Tunnel/ngrok/独立域名作为回传链路，避免 OAuth `Client ID not found`。
- **一次配对、长期记忆**：首次配对成功后，Hub 地址、设备目录与心跳凭据安全持久化；双方电脑或软件重启后自动恢复心跳，不需要再次配对。同一个“配对码 + device_id”在成功后的 **1800 秒（30 分钟）**内可幂等重试并返回同一凭据，解决首次 HTTP 响应丢失场景。
- **单实例桌面**：同一 Windows 用户只能运行一个 MCP DevBridge；首次升级会清理旧版本遗留的重复进程。
- **服务状态修复**：稳定状态会清除残留 busy 标记，READY 项目的“停止服务”始终可点；托盘退出提供即时反馈和清理 watchdog。
- **复制反馈**：地址、令牌、Gateway 地址、Gemini 凭据等复制按钮短暂变绿并显示“复制成功”。
- **内置更新**：启动后与每 **12 小时**检查 GitHub Release。仅发现新版本时显示右上角更新图标，点击可查看说明、下载、校验 SHA-256、静默安装并自动重启。v0.8.0 之前的版本仍需手动安装 v0.8.0 一次。
- **安装包自带运行组件**：正式 Windows 安装包内置固定版本 Node.js、uv/uvx 与 cloudflared，普通用户无需预装这些组件或修改 PATH；启动和诊断会自动检查完整性。可选 Windows 控制首次启用时由内置 uvx 获取锁定版本的 Windows-MCP，不做系统级静默安装。

### 多设备正确拓扑

```text
ChatGPT / Gemini
       │
       │  唯一固定 URL
       ▼
https://mcp.example.com/mcp
       │
       ▼
主 Hub（唯一持有该 Named Tunnel Token）
       │
       ├─ 本机工作区 A / B / C
       │
       └─ 远端设备 ── Quick Tunnel / ngrok / 独立域名
                    └─ 远端工作区 X / Y
```

**不要**把主 Hub 的 `cloudflared.exe service install ey...` Token 复制到朋友电脑。那会把两台机器变成同一 Tunnel 的 replicas，使 OAuth 注册和授权请求可能落在不同机器，导致 `Client ID not found`。

---

## v0.7.2 默认异步命令任务

> **v0.7.1 已被 v0.7.2 取代。** v0.7.1 首次发布后真机验收发现任务目录绑定在单个 MCP Server/session 实例：`bash` 返回 task_id 后，另一次 MCP 调用可能无法找到任务。v0.7.2 将 `BashTaskManager` 提升为 CodexPro 进程级共享，同时继续按 workspace 隔离，并新增跨 HTTP MCP session 回归测试。

- `bash` **不再暴露 `timeout_ms`，也没有固定执行时长上限**。每次执行命令都会立即创建后台任务并返回 `task_id`。
- 不再区分“普通命令”和“大型任务”，也删除了重复的 `start_task`。短命令和长构建统一走同一套任务模型。
- 任务管理只保留 `get_task / wait_task / list_tasks / cancel_task`：查看状态、短暂等待、列出任务或取消任务。
- `wait_task` 默认等待 15 秒、单次最多等待 30 秒，只是一次状态查询；它返回并不会停止后台任务，任务会继续运行直到自然结束或被取消。
- 增加 **600 秒编排看门狗**：如果某个任务 600 秒没有被任何 `get/wait/list/cancel` 调用接续，下一次读取会标记 `orchestrationStale=true` 并给出恢复提示。看门狗只识别“编排断了”，**绝不终止任务**。
- 输出使用有界滚动缓存：长时间大量日志不会无限占用内存，较早输出可能被省略，但任务不会因为日志量而被终止。
- 所有任务仍经过原有工作区边界、Bash session、权限档和危险命令拦截；`cancel_task` 会结束完整子进程树。
- 任务状态保存在当前 CodexPro 进程内，完成任务最多保留 24 小时 / 100 条。**关闭、升级或重启 MCP DevBridge 会结束仍在运行的任务，不做跨重启续跑。**

---

## v0.7.0 Multi-Device Hub 与新手体验

- **一个 ChatGPT MCP 地址可管理多台电脑**：主 Hub 维护设备目录；每个 MCP 会话可以独立切换目标电脑，不会影响朋友正在使用的另一个会话。
- **单设备自动选择**：当前只有一台电脑在线时自动使用它；多台在线时默认本机，并可用 `devbridge_list_devices / devbridge_switch_device` 切换。
- **远端 Quick Tunnel 自动更新**：朋友电脑可用 Quick Tunnel 加入 Hub；临时 `trycloudflare.com` 地址变化后会通过心跳自动更新到主 Hub，ChatGPT 仍只连接主 Hub 的固定 URL。
- **设备配对不要求开放家庭路由器端口**：两端都通过已有公网 MCP 入口通信；6 位一次性配对码只在内存存在 10 分钟，设备 Bearer/心跳凭据进入 Windows 凭据存储，不写入 `devices.json` 或日志。
- 新增顶层 **设备** 与 **使用手册** 页面。使用手册支持搜索、上一篇/下一篇和“帮我选择连接方式”，覆盖 ChatGPT、Gemini、Quick Tunnel、固定地址、多设备、权限、诊断和常见问题。
- 工作台/项目设置关键网络字段增加 `?` 上下文帮助；悬浮或点击显示非模态说明，帮助用户理解连接方式、域名、Tunnel Token 和公网入口端口。
- 工作台删除重复的“连接自测”卡和底层组件状态；自测合并进 **诊断**，诊断先给“可以正常使用 / 需要处理”结论，再给逐步解决方法。
- **日志真正接入正式链路**：运行情况读取当前选中项目的进程输出；操作记录由 Gateway 直接记录实际 `tools/call`；网络连接把 Gateway JSONL 加工成普通用户能理解的事件。

### Quick Tunnel 到底怎么用？

选择“Quick Tunnel 临时测试”并启动服务后，Cloudflare 会随机生成一个 `https://xxxxx.trycloudflare.com` 地址，MCP DevBridge 自动补上 `/mcp`。单机使用时把工作台显示的 MCP 地址复制到 ChatGPT / Gemini 即可；重新建立 Quick Tunnel 后地址会变化，需要在客户端更新。**如果这台电脑已经作为远端设备加入固定主 Hub，新地址会自动上报 Hub，不需要修改 ChatGPT 中的主 Hub URL。**

> 长期 Multi-Device 建议：主 Hub 使用 Cloudflare / ngrok 固定地址；远端电脑可以使用 Quick Tunnel。

---

## v0.6.0 桌面体验重构

- 顶层界面收敛为 **工作台 / 项目设置 / 诊断 / 日志 / 设置**，详细技术信息不再挤在主操作路径里。
- 多项目交互改为真正的**按项目状态控制**：一个项目运行不会锁住其它项目；运行项目自己的“停止服务”始终可用，未运行项目仍可启动和编辑。
- 常驻说明文字大幅精简，必要解释改为自然语言、Tooltip、诊断结果或高级设置。
- 新安装不会预置开发者机器上的项目路径、公网域名、Git、Gemini 等数据；没有项目时连接信息显示“选择项目后显示”。通用端口仍按既有规则自动分配，并可在界面修改。
- 点击标题栏“—”仍是普通最小化到任务栏；点击“×”默认隐藏到系统托盘。托盘图标可恢复窗口，右键可退出；“设置”中可以把关闭行为改为直接退出。
- 日志页合并了进程 / 审计 / Gateway 三类日志，减少顶层导航噪音。

---

## v0.5.0 桌面交互要点

- 项目表操作列即服务开关：停止时显示“启动服务”，运行时显示“停止服务”；状态 1 秒级刷新。
- “权限模式 / 客户端 / 连接方式”下拉框忽略鼠标滚轮，页面滚动不会误改配置。
- 选择“Gemini Spark”才显示 Gemini OAuth 配置；选择“ChatGPT 网页端”时自动隐藏。
- “服务控制”只保留一个动态启停按钮和“高级设置”；独立“停止 / 重启”按钮已移除。
- “连接诊断”页会检查项目、令牌、域名/隧道、端口、Gemini URI、ngrok 环境和引擎状态；项目已连接时会继续执行真实 MCP self-test。
- 关闭窗口时，子进程清理在后台线程完成，GUI 不再同步卡住。

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

桌面端默认选择：

```text
完全访问（危险）
```

即 `system + full_system`。这是本项目当前的产品默认值；第一次实际启动完全访问模式时仍会弹出一次风险确认。需要缩小权限范围时，可主动切换为“项目工作区”或“只读”。

### 4. 选择连接方式

可选择四种连接方式：`Cloudflare 固定地址`、`ngrok 固定地址`、`Quick Tunnel 临时测试`、`仅本机`。

如果只是本机调试，可以使用“仅本机”；Quick Tunnel 适合临时验证；如果希望 ChatGPT / Gemini 网页端长期连接，推荐：

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

- **公网入口端口（Gateway）**：第一个项目通常从 `8786` 开始分配，后续项目自动避让。当前项目可在桌面“访问令牌与 MCP 地址”区域修改、检测占用、恢复默认；
  修改后**必须同步**将 Cloudflare Tunnel 的 Service URL 改为
  `http://localhost:<新端口>`，否则公网连接会失败（界面会醒目提示）。
- **项目内部端口**：CodexPro 从 `8787`、Windows-MCP 从 `28731`、Gateway 从 `8786` 起为每个项目独立分配；「高级设置…」只修改当前项目。Legacy backend `8765` 仍是全局兼容端口。服务运行期间锁定端口编辑。
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

# 多项目并行开发

MCP DevBridge 支持同时管理多个项目：

1. 在“项目列表”点击「添加项目」注册多个本地目录；项目表只保留“名称 / 路径 / 状态 / 端口 / 入口 / 操作”六列。
2. 每个项目拥有**独立的 CodexPro 引擎、Windows 桥、Gateway 端口与配置**；项目的 Bearer/Cloudflare Token 也独立加密保存。
3. 不再使用“启用”勾选和桌面启动自动恢复；需要哪个项目，直接点击该行“启动服务”。状态会实时更新为“启动中 / 已连接 / 停止中 / 失败”。
4. 同一时刻可有一个完整公网入口（Tunnel + Gateway），其它项目的 CodexPro 引擎仍可并行运行；Gateway 按 MCP session/workspace 将请求路由到目标项目。
5. **ChatGPT 和 Gemini 可以同时操作不同项目**；通过 `switch_workspace` 只切换当前 MCP session，`list_projects` 查看全部项目与运行状态。
6. 服务配置、Git 参数、端口、客户端类型、MCP 地址和自测结果均跟随当前项目；切换回来会恢复原值。

项目非敏感配置持久化于 `projects.json`；敏感值只进入 Windows Credential Manager / DPAPI SecretsStore，不以明文写入 JSON。

## Shell 与命令执行

Windows 上 Shell 默认优先级：

1. **pwsh.exe**（PowerShell 7）
2. **powershell.exe**（Windows PowerShell 5.1）
3. **cmd.exe**
4. **Git Bash**

**WSL Bash 不会被自动选择**，仅当用户明确指定时才会使用（WSL 的 Linux 工具链无法保证运行 Windows 项目脚本和开发工具）。使用 `shell_info` MCP 工具可查看所有可用 Shell 及其类型、路径和版本。桌面「开发环境检测」按钮也可一键确认 Shell / python / git / pytest / pyright 是否可调用。

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

适合希望把文件访问和命令范围限制在当前项目目录内的场景。

允许在当前选中的项目范围内：

- 读取和搜索；
- 写文件；
- Edit / Patch；
- Git；
- 受控命令执行 —— 命令首词限定为开发工具白名单
  （pytest / pyright / ruff / mypy / git（完整子命令）/ npm / uv / python 等，危险命令硬拦截）；
- 测试和构建；
- 本地开发进程管理。

## 系统权限（完全访问模式，桌面默认）

允许更高风险的系统级能力（对应命令档位 `full_system`：任意命令，首次启用需风险确认）。

如果启用了 Windows-MCP，AI 还可能获得：

- PowerShell；
- 应用控制；
- 文件系统；
- 进程管理；
- 注册表；
- Windows UI 自动化等能力。

> [!NOTE]
> 桌面端「权限模式」已与命令执行档位合一：
> **只读** = read_only + safe、**项目工作区** = workspace + developer、**完全访问（桌面默认）** = system + full_system；
> 无独立档位选择（`--execution-profile` CLI 参数与引擎映射仍独立保留）。

> [!NOTE]
> Windows 桥接工具按权限模式过滤：`默认 / 只读` 下只放行
> `desktop_ui` 白名单（点击、输入、快照、应用等 UI 操作），
> PowerShell / 注册表 / 文件系统等系统级工具会被拒绝；
> 仅 `完全访问` 模式放行全部工具（`system_full`）。
> 每次调用还会与桥端实时工具清单（inventory）交叉校验。

> [!CAUTION]
> 完全访问意味着远程 AI 工具可能影响项目目录之外的电脑状态。  
> 桌面端当前按产品设计默认使用“完全访问（危险）”；首次实际启动仍要求一次性风险确认。若不需要系统级能力，可主动降级到“项目工作区”或“只读”。

---

# 命令执行档位（Shell Execution Profile，内部模型）

桌面端 UI 已与权限模式合一（见上），此处为后端/CLI 的档位定义；
对 `run_command / run_program / start_process`（本机工具）与 Codex 引擎的 bash 工具生效：

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
- 公网入口可经过 Cloudflare Named / ngrok / Quick Tunnel，三者统一终止在本机 Gateway；
- 公网请求需要 OAuth 或有效 Bearer；
- Bearer 与 Cloudflare Tunnel Token 按项目使用 Windows Credential Manager / DPAPI 加密保存；
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
3. 桌面默认“完全访问（危险）”；如不需要系统级能力，可主动降级到“项目工作区”或“只读”；
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
- **多项目并行开发**（每个项目独立 CodexPro 引擎与端口，GPT/Gemini 可同时操作不同项目）
- **Windows Shell 自动检测**（pwsh > PowerShell > cmd > Git Bash 优先级，WSL 不会自动调用）
- 日志与审计
- PyInstaller / Inno Setup 打包

欢迎提交 Issue、Bug 报告和兼容性测试结果。

---

# License

MCP DevBridge 项目代码按仓库声明的许可证发布。

第三方组件保留各自原始许可证与版权声明。  
请参阅：

[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)
