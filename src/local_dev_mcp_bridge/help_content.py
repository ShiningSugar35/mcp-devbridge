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
        ("多设备", "hub", "朋友", "电脑", "配对", "心跳"),
        """<h2>Multi-Device Hub</h2>
        <p>目标是：<b>ChatGPT 永远只连接主 Hub 的一个固定 MCP 地址</b>，但同一个聊天可以切换到你或朋友的不同电脑。</p>
        <h3>主 Hub 电脑</h3>
        <ol><li>建议用 <b>Cloudflare 固定地址</b>或 ngrok 固定地址启动公网服务，让 ChatGPT 永远连接同一个 Hub URL。</li><li>到“设备”页生成 6 位配对码。</li><li>把主 Hub 的 MCP 地址和配对码发给朋友。</li></ol>
        <p>主 Hub 也可以临时用 Quick Tunnel 测试，但 Hub 自己一旦重启换了地址，ChatGPT 和已经加入它的远端电脑都需要更新 Hub 地址，所以不适合长期多设备使用。</p>
        <h3>朋友电脑</h3>
        <ol><li>安装 MCP DevBridge，添加项目，并先启动一个公网服务（Quick Tunnel 也可以）。</li>
        <li>到“设备”页填写主 Hub MCP 地址和配对码，点击“加入 Hub”。</li></ol>
        <p>之后朋友电脑每隔约 15 秒向 Hub 报告在线状态和当前公网地址。即使朋友使用 Quick Tunnel，
        重启后随机地址改变，Hub 也会自动更新。只有一台电脑在线时，ChatGPT 自动使用它；多台在线时可以切换设备。</p>""",
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
        <p><b>朋友电脑离线会怎样？</b><br>Hub 会把它显示为离线，不会把请求发过去。只有一台设备在线时会自动选择在线设备。</p>""",
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
