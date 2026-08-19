"""Beginner-facing help copy for the desktop UI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ManualTopic:
    id: str
    title: str
    keywords: tuple[str, ...]
    html: str


HELP_CONNECTION_INFO = """
<b>这里是给 ChatGPT / Gemini 使用的连接信息。</b><br><br>
<b>MCP 地址</b>相当于“这台开发电脑在网上的入口地址”；<b>访问令牌</b>相当于这扇门的密码。
把两项按教程填入 ChatGPT 或 Gemini 后，网页端才能调用本机的开发工具。<br><br>
地址是否长期不变，取决于你在“项目设置 → 连接方式”中的选择。
"""

HELP_GATEWAY_PORT = """
<b>这是公网连接进入 MCP DevBridge 后使用的本机端口。</b><br><br>
大多数用户不需要修改它，保持默认即可。只有端口被其它软件占用，或你在 Cloudflare / ngrok
中已经手工指定了不同端口时才需要调整。<br><br>
可以把端口理解成一栋楼里的“房间号”：公网地址负责找到这台电脑，端口负责找到 MCP DevBridge。
"""

HELP_CONNECTION_METHOD = """
<b>四种方式的区别：</b><br><br>
<b>Cloudflare 固定地址</b>：适合长期使用。地址固定，电脑或软件重启后 ChatGPT 通常不用重新配置。
需要你有一个 Cloudflare Tunnel 和固定域名。<br><br>
<b>ngrok 固定地址</b>：也是长期固定地址，适合已经在使用 ngrok Reserved Domain 的用户。
需要安装 ngrok 并准备固定域名。<br><br>
<b>Quick Tunnel 临时测试</b>：最省事，不需要 Cloudflare 账号或自己的域名。启动后软件会得到一个临时
trycloudflare.com 地址，复制到 ChatGPT 就能测试。但每次重新建立 Quick Tunnel 地址都会变化，
单机使用时需要在 ChatGPT 里换成新地址。加入 Multi-Device Hub 后，远端电脑的新地址会自动上报 Hub，
ChatGPT 仍只连接主 Hub 的固定地址。<br><br>
<b>仅本机</b>：完全不把服务暴露到互联网，只适合本机程序测试。网页端 ChatGPT / Gemini 无法从互联网访问它。<br><br>
<b>不知道怎么选？</b> 第一次体验用 Quick Tunnel；确定长期使用后优先 Cloudflare 固定地址；
只做本机调试选“仅本机”；已有 ngrok 基础设施再选 ngrok。
"""

HELP_PUBLIC_HOSTNAME = """
<b>公网域名就是你希望长期使用的互联网地址。</b><br><br>
例如你配置了 <code>mcp.example.com</code>，最终 MCP 地址通常就是
<code>https://mcp.example.com/mcp</code>。<br><br>
只有 Cloudflare 固定地址和 ngrok 固定地址需要填写。Quick Tunnel 会自动生成临时地址，“仅本机”不需要域名。
"""

HELP_TUNNEL_TOKEN = """
<b>这是 Cloudflare Named Tunnel 用来证明“这台电脑有权启动这个隧道”的凭据。</b><br><br>
它不是 ChatGPT 的登录密码，也不是 MCP 的访问令牌。通常从 Cloudflare Tunnel 配置中复制一次即可。
MCP DevBridge 会把它加密保存在 Windows 凭据存储中，不会写入项目文件或日志。<br><br>
只有选择“Cloudflare 固定地址”时需要它。
"""


MANUAL_TOPICS: tuple[ManualTopic, ...] = (
    ManualTopic(
        "first-use",
        "第一次使用：5 分钟完成连接",
        ("第一次", "开始", "安装", "项目", "启动"),
        """<h2>第一次使用：5 分钟完成连接</h2>
        <ol>
        <li>在“工作台”点击 <b>添加项目</b>，选择你真正要开发的项目文件夹。</li>
        <li>打开“项目设置”，确认权限模式。默认“完全访问”能力最完整，但请只在你信任的电脑和账号上使用。</li>
        <li>选择连接方式。第一次体验建议 <b>Quick Tunnel 临时测试</b>；长期使用建议 Cloudflare 固定地址。</li>
        <li>回到“工作台”，点击 <b>启动服务</b>。等状态变成“可以使用”。</li>
        <li>复制“连接信息”中的 MCP 地址和访问令牌，按“连接 ChatGPT”或“连接 Gemini”教程填写。</li>
        <li>最后到“诊断”运行一次完整检查。看到“可以正常使用”后再开始开发。</li>
        </ol>""",
    ),
    ManualTopic(
        "chatgpt",
        "连接 ChatGPT 网页端",
        ("chatgpt", "gpt", "plugin", "app", "网页", "连接"),
        """<h2>连接 ChatGPT 网页端</h2>
        <p>先在 MCP DevBridge 的工作台启动一个公网连接，然后复制 <b>MCP 地址</b> 和 <b>访问令牌</b>。</p>
        <ol>
        <li>在 ChatGPT 中启用 Developer Mode，并在 Apps / 自定义应用设置中添加远程 MCP。</li>
        <li>把 MCP 地址粘贴到远程 MCP 地址位置。ChatGPT 不能直接访问只监听本机的地址，所以必须先用 MCP DevBridge 建立公网连接。</li>
        <li>如果界面要求 Bearer / Authorization，填入工作台复制的访问令牌。</li>
        <li>保存后回到对话，选择刚添加的 MCP DevBridge。</li>
        <li>可以先让 ChatGPT “列出当前项目”或“读取 README”，确认连接正常。</li>
        </ol>
        <p><b>权限提示：</b>ChatGPT 是否允许 MCP 执行写入/修改操作取决于你的套餐和工作区权限；如果只能读取而不能修改，不一定是 MCP DevBridge 故障。</p>
        <p><b>Quick Tunnel 用户：</b>如果软件重启后生成了新的临时地址，单机模式下需要回 ChatGPT 更新地址。</p>""",
    ),
    ManualTopic(
        "gemini",
        "连接 Gemini",
        ("gemini", "oauth", "redirect", "connected app"),
        """<h2>连接 Gemini</h2>
        <p>Gemini 的 Custom Connected Apps 需要你的账号已经获得 Gemini Spark 对应功能；Google 还会检查年龄、账号类型、地区和 Keep Activity 等条件，实际可用范围以 Gemini 页面为准。</p>
        <ol>
        <li>在“项目设置”把“连接客户端”改为 <b>Gemini Spark</b>，先启动该项目的公网服务。</li>
        <li>在 Gemini 网页端打开 Settings & help → Connected Apps，在 Custom apps for Spark 中添加 MCP 地址。</li>
        <li>如果 Gemini 提示服务器不支持自动注册，展开 Advanced features；从 Gemini 复制 Redirect URI 到 MCP DevBridge。</li>
        <li>点击“生成 / 更新凭证”，把 Client ID 和 Client Secret 填回 Gemini，然后按页面提示完成授权。</li>
        <li>授权时选择一个正在运行的项目。未运行的项目不会被静默授权。</li>
        </ol>""",
    ),
    ManualTopic(
        "connections",
        "怎么选择连接方式",
        ("连接方式", "cloudflare", "quick", "ngrok", "仅本机", "域名"),
        f"""<h2>怎么选择连接方式</h2>{HELP_CONNECTION_METHOD}
        <h3>快速判断</h3>
        <ul>
        <li>只想先试一下 → <b>Quick Tunnel</b></li>
        <li>准备每天使用 → <b>Cloudflare 固定地址</b></li>
        <li>公司/个人已经有 ngrok Reserved Domain → <b>ngrok 固定地址</b></li>
        <li>完全不需要网页端，只在本机测试 → <b>仅本机</b></li>
        </ul>""",
    ),
    ManualTopic(
        "quick",
        "Quick Tunnel：最快的临时连接",
        ("quick", "trycloudflare", "临时", "重启", "换地址"),
        """<h2>Quick Tunnel</h2>
        <p>它适合“我现在只想马上连通一次”。不需要 Cloudflare 账号，也不需要购买域名。</p>
        <ol>
        <li>项目设置 → 连接方式 → Quick Tunnel 临时测试。</li>
        <li>点击启动服务，等待软件生成一个 <code>https://xxxxx.trycloudflare.com/mcp</code> 地址。</li>
        <li>复制这个地址到 ChatGPT / Gemini。</li>
        </ol>
        <p><b>代价：</b>临时地址不是你的固定地址。重新建立 Quick Tunnel 时会换一个随机地址。
        如果这台电脑已经加入 Multi-Device Hub，软件会把新地址自动报告给 Hub，不需要修改主 Hub 在 ChatGPT 中的地址。</p>""",
    ),
    ManualTopic(
        "cloudflare",
        "Cloudflare 固定地址：长期使用",
        ("cloudflare", "固定", "tunnel token", "域名", "service url"),
        """<h2>Cloudflare 固定地址</h2>
        <p>这是推荐的长期连接方式。你给 MCP DevBridge 准备一个固定域名和 Cloudflare Named Tunnel，
        之后电脑重启、程序重启，ChatGPT 仍然使用同一个 MCP 地址。</p>
        <p>你通常只需要准备三项：固定域名、Tunnel Token、Cloudflare 中指向本机 Gateway 的 Service URL。
        “公网入口端口”没有特殊需求时保持默认。</p>""",
    ),
    ManualTopic(
        "ngrok",
        "ngrok 固定地址",
        ("ngrok", "reserved domain", "固定"),
        """<h2>ngrok 固定地址</h2>
        <p>如果你已经使用 ngrok 并拥有 Reserved Domain，可以让 MCP DevBridge 复用它。
        体验上和 Cloudflare 固定地址类似：ChatGPT 里的 MCP 地址不需要每次重启都修改。</p>
        <p>如果你从未用过 ngrok，没有必要为了 MCP DevBridge 专门从这里开始；Quick Tunnel 或 Cloudflare 通常更容易理解。</p>""",
    ),
    ManualTopic(
        "multi-device",
        "两台或多台电脑：Multi-Device Hub",
        ("多设备", "hub", "朋友", "电脑", "配对", "心跳", "client id"),
        """<h2>Multi-Device Hub 与双固定 App</h2>
        <p>v0.9.0 支持两种长期使用方式，可以同时启用。</p>
        <h3>方式 A：一个共享 Hub App</h3>
        <ol><li>主机用 <b>Cloudflare 固定地址</b>运行 Hub，例如 <code>https://mcp.example.com/mcp</code>。</li>
        <li>主 Hub 的 Cloudflare Tunnel Token <b>只部署在主 Hub 电脑</b>。</li>
        <li>朋友电脑先启动自己的公网回传（Quick/ngrok/独立域名），再用 6 位配对码加入主 Hub。</li>
        <li>首次成功后 Hub 地址、设备凭据和心跳身份会持久化；重启无需再次配对。Quick 地址变化会由 heartbeat 自动更新到主 Hub。</li></ol>
        <p>共享 Hub 下可以直接让 AI 用 <code>device_id</code> 查询某台电脑的工作区，不必依赖上一次 transport 的设备切换状态。</p>
        <h3>v0.9.1：SteamOS / Linux Desktop</h3>
        <p>SteamOS/Linux 使用原生桌面版，不需要 Wine/Proton。配置放在 XDG 用户目录；程序安装到用户目录并创建 Desktop Entry/autostart；Node.js 与 cloudflared 使用应用私有 Linux runtime。Windows 控制桥在 Linux 自动禁用，文件、Shell、Git、进程与 Agent 使用 Linux 原生工具。</p>
        <p>Linux 凭据优先写入桌面 Secret Service；不可用时进入 AES-GCM 加密 fallback。不要把 Tunnel Token 写进 shell 脚本或 projects.json。</p>
        <h3>v0.9.3：普通 ChatGPT Chat 多 Agent</h3>
        <p>Windows 上可以在“Agent 管理”里显式准备 <b>ChatGPT Chat Agent</b>。准备后，AgentPool 的 <code>auto</code> 会优先创建普通“ChatGPT / 聊天”子会话；子 Chat 不切 Work/Codex，而是直接调用你已经连接的 MCP DevBridge 完成本地任务。</p>
        <p>Deep Link 只负责创建 Chat 和预填 prompt；DevBridge 通过仅监听 <code>127.0.0.1</code> 的 CDP 精确点击真实“发送/停止”控件。它不读取 ChatGPT 登录 Token，也不调用私有 ChatGPT HTTP API。不使用时点击“恢复普通启动”即可去掉 CDP。</p>
        <p>子 Chat 的“我完成了”不算验收。每个任务必须通过 MCP 写入带 task id 的 JSON receipt 并 read-back；外部 AgentPool 验证 receipt 后才标记完成。Git 项目仍使用独立 worktree，非 Git/盘符目录继续 direct。</p>
        <p>为了兼容 C:/D: 盘符级服务和 ChatGPT transport 重建，子 Agent 每次 MCP 调用都固定携带 <code>devbridge_workspace_id</code>；不要依赖“上一次切到哪个工作区”的隐式状态。</p>
        <h3>v0.9.2：Agent 控制面与可靠性</h3>
        <p><b>Agent Pool 是底层执行器；Agent Orchestrator 是上层编排器。</b>单 Agent 用 <code>spawn_agent</code>，后续可用 <code>message_agent</code> 继续派指令；Team 用 <code>spawn_agent_team</code> 一次提交多个 worker。桌面右上角“Agent 管理”或 <code>Ctrl+Shift+A</code> 可查看状态、模型、目标目录、分支、输出，并执行消息、取消和清理。</p>
        <p>Git 项目默认使用独立 branch/worktree；盘符根或普通非 Git 目录在 <code>auto</code> 下会改用 <code>direct</code> 本地写入。Team 默认 <code>all_required</code>，真实 executor 还必须返回结构化成功回执，避免把“模型没做任务但进程正常退出”算成功。</p>
        <p>OpenCode 默认使用 <code>--pure</code> 隔离外部插件；未指定模型时使用免费的 <code>opencode/nemotron-3-ultra-free</code>。免费模型可能排队，因此适合保底而不是高时效任务。</p>
        <h3>v0.9.1：一台电脑多个项目怎么配</h3>
        <p>不要再给 C:/D:/E:/F: 四个项目分别填写同一个域名和 Tunnel Token。到工作台 → 连接信息 → <b>设备全局连接配置</b>，一次保存连接方式、固定域名和本设备专属 Tunnel 凭据；现有项目会同步，后续新增项目自动继承。</p>
        <p>Cloudflare Published Application 的 Service URL 对应的是这台设备唯一的 Gateway（默认 <code>http://localhost:8786</code>），不是每个项目各一套公网 Gateway。项目之间保持独立的是 CodexPro/Windows 内部端口。</p>
        <h3>方式 B：每台电脑一个固定 App（推荐两个人长期并行）</h3>
        <p>主机继续使用 <code>mcp.example.com</code>。朋友在 Cloudflare 新建<b>另一个独立 Tunnel</b>，例如发布 <code>jerry.example.com → http://localhost:8786</code>，并把这个新 Tunnel 自己的 Token 填到朋友电脑。</p>
        <p><b>两台电脑都写 localhost:8786 完全没问题</b>：localhost 属于各自电脑。真正禁止的是两台独立 Gateway 共用同一个 Tunnel UUID/Token；那会形成 replicas，Cloudflare 可能把 OAuth 请求送到不同机器。</p>
        <p>之后主机 ChatGPT App 连接 <code>https://mcp.example.com/mcp</code>，朋友自己的 App 连接 <code>https://jerry.example.com/mcp</code>，日常不需要切设备。朋友仍可保留 Hub 配对，因此共享 Hub 和独立 App 可以并存。</p>
        <h3>不要把 Quick URL 当长期独立 App</h3>
        <p>Quick Tunnel 的随机 URL 在重建后会变化。Hub 心跳能更新远端回传地址，但 ChatGPT 中一个直接绑定旧 Quick URL 的 App 不会自动改地址；长期独立 App 请换固定 Tunnel。</p>
        <p>如果首次注册的 HTTP 响应丢失，同一个“配对码 + 设备 ID”在成功后的 <b>30 分钟</b>内仍可幂等重试。</p>""",
    ),
    ManualTopic(
        "agent-pool",
        "Agent Pool：在一个聊天里并发开发",
        ("agent", "pool", "并发", "worktree", "opencode", "多智能体", "并行开发"),
        """<h2>Agent Pool</h2>
        <p>v0.9.0 可以把一个 ChatGPT 会话当作 manager/reviewer，在本机或指定远端设备排队多个实施 Agent，不需要为每个子任务手工新开 ChatGPT 对话。</p>
        <h3>执行与并发</h3>
        <ul><li>本地 worker 会探测真正可非交互运行的 <b>OpenCode CLI</b> 与 <b>Claude Code CLI</b>，不会把 Electron 桌面版误判成 worker。可用 CLI 会出现在 capabilities；模型账号认证、额度和 provider 健康在任务运行时验证。</li>
        <li>单次最多排队 64 个任务；默认真实同时运行 4 个，硬上限 16。其余任务留在队列中，不会为了“看起来并发”把电脑一次拉满。</li>
        <li>长任务返回 task id 后后台执行；用 list/get/wait/cancel 查看或停止。wait 单次最多 30 秒，不会占住 ChatGPT 的长同步请求。</li></ul>
        <h3>写入隔离怎么工作</h3>
        <p>Git 项目默认给每个写 Agent 创建独立 <code>git worktree</code> 和 <code>mcp-agent/&lt;id&gt;</code> 分支；主 checkout 不被子 Agent 直接编辑。盘符根和普通非 Git 目录没有 branch 概念，<code>auto</code> 会使用 <code>direct</code> 模式，因此并发任务应通过 <code>target_path</code> 尽量分到不同子目录，避免两个 Agent 同时改同一文件。</p>
        <p><b>注意：</b>worktree/direct 都不是 Windows 安全沙箱。OpenCode / Claude Code 进程仍继承当前用户权限；Agent Pool 不自动 push 主分支。</p>
        <h3>跨电脑</h3>
        <p>共享 Hub 下，Agent Pool 工具支持正式 <code>device_id</code>；spawn/spawn_batch 还支持 <code>project_id</code>。因此 manager 可以直接指定“在哪台电脑的哪个运行项目里启动 Agent”，不依赖上一条设备切换状态。</p>""",
    ),
    ManualTopic(
        "runtime",
        "安装与运行组件",
        ("安装", "依赖", "node", "uv", "uvx", "cloudflared", "环境"),
        """<h2>安装与运行组件</h2>
        <p>MCP DevBridge 正式安装包已经自带项目引擎所需的 <b>Node.js</b>、Windows 控制启动器 <b>uv/uvx</b> 和 <b>cloudflared</b>，普通用户不需要另外安装这些程序，也不需要手工修改 PATH。</p>
        <p>软件启动时会自动检查关键运行组件；“诊断”页也会显示组件状态。如果正式安装版提示 Node.js/uvx/cloudflared 缺失，通常意味着安装文件不完整，优先重新安装最新版，而不是自行修改系统环境。</p>
        <p><b>Windows 控制是可选功能。</b>首次启用时，内置 uvx 会联网获取项目锁定版本的 Windows-MCP；这一步只进入 uv 的用户缓存，不会把 Node.js/uv 全局安装到系统。</p>
        <p>“开发环境检测”里的 Python、Git、pytest、pyright 是给开发项目本身使用的工具链，不是启动 MCP DevBridge 桌面程序的前置依赖。</p>""",
    ),
    ManualTopic(
        "permissions",
        "权限与安全",
        ("权限", "只读", "完全访问", "安全", "令牌"),
        """<h2>权限与安全</h2>
        <p><b>只读</b>：适合只让 AI 看代码和分析问题。<br>
        <b>项目工作区</b>：AI 可以修改当前项目，但不会主动跨出项目目录。<br>
        <b>完全访问</b>：能力最完整，也意味着 AI 可以接触项目外的文件和系统命令。</p>
        <p>MCP 地址可以公开，但访问令牌应当像密码一样保管。MCP DevBridge 会把敏感凭据保存到 Windows 凭据存储，日志会做脱敏。</p>""",
    ),
    ManualTopic(
        "command-tasks",
        "命令任务怎么运行",
        ("任务", "命令", "异步", "构建", "打包", "task"),
        """<h2>命令任务怎么运行</h2>
        <p>MCP DevBridge 不再给命令设置固定执行时长。AI 每次执行命令时，系统都会立即创建一个后台任务并返回任务 ID。</p>
        <p>短命令通常很快完成，AI 会继续等待结果；构建、打包、长测试则可以一直在后台运行，直到自然结束或被取消。整个过程不需要先判断任务是“大型”还是“普通”。</p>
        <p>任务输出使用有界滚动缓存，长时间大量日志不会无限占用内存；较早的输出可能被省略，但不会因此停止任务。</p>
        <p><b>等待不会无限卡住：</b>单次等待默认 15 秒、最多 30 秒；如果一个任务连续 600 秒没有被 AI/客户端继续查看，系统会把编排状态标记为“需要接续”，但后台任务仍照常运行。下一次继续时会直接读取当前真实结果。</p>
        <p>注意：关闭、升级或重启 MCP DevBridge 会结束当前受管进程树，因此正在运行的任务不会跨软件重启继续运行。</p>""",
    ),
    ManualTopic(
        "diagnostics",
        "诊断与日志怎么看",
        ("诊断", "日志", "失败", "错误", "记录"),
        """<h2>诊断与日志怎么看</h2>
        <p>遇到“ChatGPT 连不上”时，先到 <b>诊断</b>，点击完整检查。顶部会直接告诉你“可以正常使用”还是“需要处理”。
        每个失败项都会给下一步，不要求你理解底层组件名称。</p>
        <p><b>日志 → 操作记录</b>回答“AI 刚才做了什么”；<b>日志 → 运行情况</b>回答“服务启动过程中发生了什么”；
        <b>日志 → 网络连接</b>回答“网页端的连接有没有到达这台电脑”。</p>""",
    ),
    ManualTopic(
        "faq",
        "常见问题",
        ("常见", "为什么", "连不上", "地址", "离线", "灰色"),
        """<h2>常见问题</h2>
        <p><b>为什么 Quick Tunnel 重启后连不上？</b><br>因为临时地址换了。复制工作台的新地址到 ChatGPT；如果它是 Hub 的远端设备则会自动更新。</p>
        <p><b>为什么某些设置在运行时变灰？</b><br>端口、权限和连接方式会决定服务如何启动，运行中修改会造成“界面和实际服务不一致”，所以只锁当前正在运行项目的这些启动参数。停止服务仍然可以点击，别的项目也不会被锁住。</p>
        <p><b>朋友电脑离线会怎样？</b><br>Hub 会把它显示为离线，不会把请求发过去。只有一台设备在线时会自动选择在线设备。</p>
        <p><b>为什么出现 Client ID not found？</b><br>最常见原因是把主 Hub 的同一个 Cloudflare Tunnel Token 又部署到了另一台电脑。同一个固定域名此时会把 OAuth 请求分流到不同机器，而每台机器保存的 OAuth Client ID 不同。请先停止远端电脑上的主 Hub Named Tunnel，改用 Quick Tunnel/ngrok/独立域名作为回传链路；然后在 ChatGPT 删除并重新创建一次主 Hub App，之后只保留这一个固定域名 App。</p>""",
    ),
)


def search_topics(query: str) -> list[ManualTopic]:
    needle = query.strip().casefold()
    if not needle:
        return list(MANUAL_TOPICS)
    return [
        topic
        for topic in MANUAL_TOPICS
        if needle in topic.title.casefold()
        or any(needle in keyword.casefold() for keyword in topic.keywords)
        or needle in topic.html.casefold()
    ]


def recommend_connection(*, internet_client: bool, long_term: bool, has_fixed_domain: bool) -> str:
    if not internet_client:
        return "推荐：仅本机。你不需要把开发服务发布到互联网。"
    if not long_term:
        return "推荐：Quick Tunnel。配置最少，适合先验证 ChatGPT / Gemini 能否正常使用。"
    if has_fixed_domain:
        return "推荐：Cloudflare 固定地址（已有 ngrok Reserved Domain 时也可选 ngrok）。长期使用时地址稳定，不用反复修改 ChatGPT。"
    return "推荐：先用 Quick Tunnel 完成验证；确定长期使用后再配置 Cloudflare 固定地址。"


__all__ = [
    "ManualTopic",
    "MANUAL_TOPICS",
    "search_topics",
    "recommend_connection",
    "HELP_CONNECTION_INFO",
    "HELP_GATEWAY_PORT",
    "HELP_CONNECTION_METHOD",
    "HELP_PUBLIC_HOSTNAME",
    "HELP_TUNNEL_TOKEN",
]
