# MCP Live 与 0.8.9 工具链

MCP Live 提供独立的公网 MCP 入口。Windows 便携端通过出站 HTTPS 接入，运行原 DevBridge 工作区引擎。启动前为每个文件夹设置权限，默认只读。工作区、任务和 Windows 能力由原引擎校验。

## 服务端部署

准备安装 Docker 的 Linux 主机、可用的 HTTPS 域名和回源方案。中转服务只监听服务器回环地址，默认端口 `8796`。将域名的 HTTPS 回源指向 `http://127.0.0.1:8796`，并为 MCP、OAuth、健康检查和下载路径使用同一入口。

部署脚本使用三个独立的私有凭据。服务所有者凭据只能生成临时配对码或撤销活动设备，MCP 访问凭据用于受管 API 验收，OAuth 批准凭据用于账号连接。固定设备密钥与这些角色无关，服务器只保存带盐的 scrypt 验证值。配置文件应写在 Docker 构建上下文之外，挂载到容器的只读凭据目录。使用密码管理器生成自己的长随机密钥，避免写入 Git、命令参数、日志或压缩包。

启动后检查 `/health` 返回成功、匿名访问 `/mcp` 返回 401，以及 HTTPS OAuth 元数据可发现。然后在 ChatGPT 插件页添加远程 MCP，输入 `https://你的域名/mcp`，选 OAuth 并完成一次账号授权。已配置的连接需复测其可发现工具。发行包的旧 Live 辅助工具与原 50 项工具共存。


### 从发行标签构建和启动

在服务器克隆仓库并检出发行页面对应的标签，再执行下列命令。替换域名和 ZIP 的实际文件名。

~~~sh
docker build -f packaging/live-server/Dockerfile -t mcp-live:release .
sudo install -d -o root -g 65532 -m 0750 /opt/mcp-live/private
sudo install -d -o 65532 -g 65532 -m 0700 /opt/mcp-live/state
sudo install -d -o root -g 65532 -m 0750 /opt/mcp-live/download
sudo sh -c 'umask 027; cd /opt/mcp-live/private; for file in owner-token access-token oauth-approval-token; do test -s "$file" || openssl rand -hex 32 > "$file"; chgrp 65532 "$file"; chmod 0640 "$file"; done'
sudo python3 packaging/live-server/configure_device_key.py /opt/mcp-live/private/device-key-verifier
sudo chgrp 65532 /opt/mcp-live/private/device-key-verifier
sudo chmod 0640 /opt/mcp-live/private/device-key-verifier
sudo install -o root -g 65532 -m 0640 ./MCP-Live-Windows-x64.zip /opt/mcp-live/download/MCP-Live-Windows-x64.zip

docker run -d --name mcp-live --restart unless-stopped \
  --network host --read-only --cap-drop ALL --security-opt no-new-privileges \
  --mount type=bind,source=/opt/mcp-live/private,target=/run/mcp-live,readonly \
  --mount type=bind,source=/opt/mcp-live/state,target=/state \
  --mount type=bind,source=/opt/mcp-live/download,target=/downloads,readonly \
  -e MCP_LIVE_PUBLIC_HOSTNAME=live.example.com \
  -e MCP_LIVE_DOWNLOAD_FILE=/downloads/MCP-Live-Windows-x64.zip \
  mcp-live:release
~~~

在 Linux Docker 上使用 host 网络，容器默认仍只绑定回环。若无需下载服务，省略下载目录挂载和 `MCP_LIVE_DOWNLOAD_FILE`。配置后 `/download` 显示大小及 SHA-256，`/download/windows` 跳转到当前 ZIP。版本化 ZIP 地址含 SHA-256，可由 CDN 缓存并支持断点续传。升级请使用新的 ZIP 文件名并重启服务。不要把凭据或源码树挂到下载位置。

~~~sh
curl -fsS http://127.0.0.1:8796/health
curl -fsS https://live.example.com/.well-known/oauth-protected-resource/mcp
curl -I https://live.example.com/download/windows
~~~

## Windows 客户端

解压整个便携包到较短的目录（如 `D:\MCP-Live`），避免 Windows 深层路径限制。双击 `MCP-Live.exe`，并保留同目录的 `_internal`。添加要共享的目录后启动。首次运行默认勾选 Windows 控制，新加目录默认选择完全访问。已经保存的目录权限和 Windows 控制选项仍按原设置读取。首次启用 Windows 控制而本机没有 uvx 时，客户端会从官方发布源获取并校验运行组件。组件准备失败时，文件与项目工具仍可启动，可从测试与日志中查看原因。高级设置可切换关闭窗口时是否收进系统托盘。托盘菜单可显示主窗口或退出。连接测试与日志可检查本机运行时、公网中转及启动进度，并复制诊断记录。设备密钥在当前窗口内存中短暂使用，不写入设置。只读模式下服务会拒绝写入和运行命令。Windows 控制需要启用原 Windows 模块及对应权限，完全访问会在每次启动共享时请求 Windows 管理员授权，断开时关闭本次启动的管理员进程。

## 升级和回滚

升级前确认当前设备已断开。保留现有 OCI 镜像、设备密钥验证值和 OAuth 存储卷，先用临时凭据及独立端口验证新镜像，再切换公网回源。失败时启动原容器并检查 OAuth 发现、原授权和返回的工具契约。