# MCP DevBridge 0.9.1：剩余本机接入及发布执行

工程：`D:\Environment\mcp`。沿原维护分支、原未提交工作树及 durable `lr_mv7j94y4_0b4532683719`。先读 AGENTS.md、开发计划.md、进度验收.md，以当前文件为准。目标是完成以下接入并验证、发布、热更新，不重做已经完成的客户端中立 UI、工具注解或 Mac 平台代码。

## 1. OAuth 适配接入

文件 `src/local_dev_mcp_bridge/gateway.py`：

- 导入 `from .oauth_transport import client_auth_routes`。
- `_build_app()` 中现有 `routes = create_auth_routes(...)` 保持，以保留元数据、注册和授权路由。该调用之后、创建 protected-resource routes 之前，增加：

```python
        routes = [route for route in routes if route.path not in {"/token", "/revoke"}]
        routes.extend(client_auth_routes(self._provider))
```

不得增加重复 token/revoke 路由。不要修改资源绑定、既有客户端授权、GPT 访问码、Google scopes 或 No Auth 地址。

现有 `oauth_transport.py` 已补齐 Basic 身份、标准表单编码、混合认证和重复参数处理；`tests/test_v091_oauth_adapter.py` 为独立验证，不能代替真实 Gateway 的 `tests/test_v091_oauth_protocol.py`。后者原先仍有七项失败，接入后必须全部重跑。

## 2. 正式 smoke 使用已完成的等待器

文件 `third_party/codexpro/scripts/smoke.mjs`：

```javascript
import { waitForTask } from './task-wait.mjs';
```

随后删除同文件旧的局部 `async function waitForTask(...)` 定义；各调用点保留。新的 `task-wait.mjs` 会等待任务终态和持久回执，不改生产完成判定。

文件 `third_party/codexpro/package.json`：在现有 scripts.smoke 的原始整条命令前加 `node scripts/task-wait-smoke.mjs && `，保留后续全部 smoke，不删减测试。

## 3. 实际验证与发布

使用本工程 `.venv` 和项目内 TEMP/TMP，先验证：

- `tests/test_v091_oauth_adapter.py`、`tests/test_v091_oauth_protocol.py`、原 OAuth/双客户端/transport 回归。
- `node scripts/task-wait-smoke.mjs` 和完整 `npm run smoke`，不得只跑独立等待器。
- 全仓 pytest、Ruff、Windows/Linux/Darwin Pyright、版本锁检查。
- Windows、Linux 和 macOS arm64/Intel 同源 CI；Mac 原生 app 及冻结引擎探针必须有实际回执。

目标版本元数据统一为 0.9.1 后，按仓库规范冻结源提交、通过 CI、创建全新 annotated tag 与 Release，核对安装包、checksum 和 provenance。历史 tag/资产不可改写；CI候选分支不等于正式发布。

WJP先升级、GRW后升级；每台安装前重新读取当前运行根并排空所有活动任务，不恢复用户已移除的旧根。保留各机配置、原 GPT/Gemini 连接、访问码；GRW保留其原中转部署。仅使用已验证资产按原热更新流程重启，失败按各机回滚。

上线后执行 `scripts/audit_tool_annotations.py` 核验公网50工具的九项新增操作注解确已加载，再在Gemini验证只读查询及一次原始PowerShell读写删除任务。不能把源码注解齐全写成消费者Gemini弹窗已减少，也不能把写操作伪装成只读。

最后回报真实 commit、CI、Release、两机版本、运行根及双客户端实测证据。完成项写进进度验收，剩余项保留开发计划，不以测试数量替代用户的最终目标。
