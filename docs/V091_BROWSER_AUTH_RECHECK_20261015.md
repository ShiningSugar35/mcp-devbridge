# 0.9.1 浏览器登录态复核与联调记录（2026-10-15）

## 当前结论

用户提供的本机 Chrome 登录态已经实际核验：Google Gemini Connectors 和 Cloudflare shiningsugar.shop 均可打开已登录页面。此前 TinyFish 默认云浏览器打开登录页的结果，只代表另一套浏览器，不能再作为本机缺少登录态的依据。

本轮完成浏览器诊断辅助脚本修复，并推进至 Google 第一层 MCP 连接风险确认页；尚未完成该轮 Google 账号关联、MCP 自身 consent 或 token 交换。没有把页面可打开当作 OAuth 已成功。

## 实际修复

项目内 `.ai-bridge/gemini-0.9.0/browser_probe.py` 在成功 initialize/list_tools 之后，读取旧 Python 字段 `inputSchema` 导致 AttributeError，随后关闭阶段又遇到第三方 BrowserMCP 的递归 close。已改为优先 `input_schema`、兼容旧字段，并兼容工具结果的 `is_error` 字段；加入本地9009端口占用检查、项目内临时目录、30分钟会话上限、按字符串去敏后序列化JSON，避免长页面结果截断成无效JSON。仅项目诊断脚本，不是修改生产网关或安装新浏览器扩展。

辅助回归 `.ai-bridge/gemini-0.9.0/test_browser_probe.py`：6 passed，task `db6bef6b-9bf1-4805-ac08-ef664a066667` completed/exit0，回执 `.ai-bridge/v091-validation/browser-helper-0332/receipt.json`。随后增加的截图读取路径已经通过实际页面截图检查；没有把它说成已额外执行单元测试。

新增有界单请求控制与读取脚本 `request_browser.py`、`read_browser_result.py`。相同请求ID不重放，前一操作没有返回结果则不提交下一操作；展示阶段只读取去敏结果。图片只写一份固定的项目内诊断文件，不导出 Cookie、浏览器Profile或登录凭据。

## 已取得的 Cloudflare 页面事实

通过本机登录会话正常导航到该域名的 Security Analytics Events、Security rules 和 Settings 页面，未更改配置。

- 域名状态 Active，Free plan。
- 自定义规则显示0/5，Rate limiting rules显示0/1；用户创建的Managed rules列表为空。这不等于Cloudflare内置托管防护关闭，Settings里另显示始终启用的Cloudflare managed ruleset。
- 过去24小时的Sampled logs实际可见：10月15日10:48:19 GMT+8的Browser integrity check Block；07:19:42的两条Managed rules Block；10月14日18:26:20、18:23:00、18:22:36及15:40:45的其他拦截。
- 当前可见行没有与原10月15日08:35 GMT+8授权窗口相符的记录。尚未展开行取得各自Host、Path、Ray ID和规则详情，也没有完成精确窗口过滤，因此这些拦截不能被归因为Gemini令牌交换。
- 官方明确Events采用采样、仅展示被安全产品处理或标记的事件；空结果或未匹配行不能证明所有来源均未被处理。依据：https://developers.cloudflare.com/waf/analytics/security-events/ 。

未关闭Bot模式、浏览器完整性检查或全站WAF，未创建放行规则、Token或额外账号。

## 本机浏览器的实际交互行为

页面navigation和snapshot可用，click/type多次返回30秒WebSocket超时。截图核对证明部分输入已实际发生，并且个别snapshot会暂时落后于可见页面。因此超时不等于操作未执行，不能盲目重复提交。键盘Enter正常推进了Gemini第一层确认。

真实页面进度：
1. 已登录Gemini的Customize/Connectors。
2. 打开Connect to an MCP server。
3. 输入 `https://mcp.shiningsugar.shop/mcp`，未展开或填写Client ID/Client secret；截图与后续snapshot均确认标准地址。
4. 通过Enter推进到Connect this MCP server，标准地址显示正确。
5. 最后截图为风险确认复选框未选中、Connect不可用。随后只发送了一次Tab，没有发送Connect或第二层Agree and continue，也没有本轮新的MCP consent同意。

最后页面仍停留在该确认流程，Google/Cloudflare登录未退出。

## 本轮中断位置与运行态

约03:58 UTC，下一次 `run_program` 调用返回：`Windows full-system administrator capability is not authorized; complete the one-time UAC broker registration first.` 该次页面截图请求未进入辅助脚本。现有短命令入口需恢复原有系统管理员授权，不能把这一运行错误当作Google OAuth的故障原因；本轮未修改系统授权机制。

随后只读复核：server_config仍可返回原mcp根配置；devbridge_list_workspaces显示mcp、C、D、AStockMultiAgent、meme、BI六根运行中。这个登记状态不代表已完成全部数据面或管理员操作验证。

两次本轮浏览器辅助任务均已通过各自停止指令结束，终态completed/exit0：
- `62dc7d0e-79be-4536-8aeb-9ee0ce478447`，03:45:34 UTC。
- `5c72588b-48e9-4226-90df-bae102693b25`，03:59:42 UTC。
第三方BrowserMCP关闭时仍有递归close的stderr；父辅助进程正常退出，不伪称上游关闭错误已修好。未停止Chrome或任何业务根。

## 未完成项及接续位置

恢复现有MCP管理员服务授权后，从当前浏览器页面核验起继续；不要以旧snapshot推断复选框状态。先完成真实Google关联并关联同一UTC窗口的网关日志，再核Cloudflare事件的Host/Path/规则，不无证据地改全站配置。用户无需重新提供密码、访问码或浏览器Profile。

本轮未改生产OAuth源码、GPT地址/访问码、域名、项目权限；此前7项OAuth协议测试失败及Mac原生构建/发布验收没有在本轮被宣称通过。0.9.1未commit/push、未创建正式tag/Release、未安装或热重启。原durable仍为 `lr_mv7j94y4_0b4532683719`；诊断文件和单份最后截图保留为下一次接续证据。
