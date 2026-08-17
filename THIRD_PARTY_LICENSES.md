# THIRD-PARTY LICENSES

MCP DevBridge bundles third-party components. This file lists each bundled
component and its license. The full license texts are included in the
distributed package next to each component (e.g. `LICENSE` files inside
`third_party/codexpro/node_modules/...`) or in the component's repo.

## Engine: CodexPro (forked)

| Component | Version | License | Source |
|---|---|---|---|
| codexpro (engine, fork of `rebel0789/codexpro`) | 0.29.0 | MIT | `third_party/codexpro/FORK_NOTES.md` |

## Runtime engine: Windows-MCP

| Component | Version | License | Source |
|---|---|---|---|
| windows-mcp (桥接引擎，`uvx --from windows-mcp==0.8.2` 拉取，不随包分发) | 0.8.2 | MIT | https://github.com/CursorTouch/Windows-MCP |

本软件运行时通过 `uvx --from windows-mcp==0.8.2 windows-mcp serve` 启动
Windows-MCP 子进程（仅回环 127.0.0.1）。版本锁定在 `engines.py` 的
`WINDOWS_MCP_PINNED_VERSION`；其许可证（MIT）见上游仓库。

## Bundled binary: cloudflared

| Component | Version | License | Source |
|---|---|---|---|
| cloudflared.exe (`.tools/`, 随安装包分发) | 2026.7.3 | 以官方仓库 LICENSE 为准 | https://github.com/cloudflare/cloudflared |

## Bundled private runtimes

| Component | Version | License | Source |
|---|---|---|---|
| Node.js Windows x64 runtime (`node.exe`) | 22.19.0 | Node.js license / bundled third-party notices | https://nodejs.org/ |
| uv / uvx Windows x64 runtime | 0.11.25 | MIT OR Apache-2.0 | https://github.com/astral-sh/uv |

These executables are bundled privately for MCP DevBridge and are not installed system-wide or added to the user's PATH.

## Desktop runtime (打包进 PyInstaller 产物)

| Component | Version | License |
|---|---|---|
| PySide6 | 6.11.1 | LGPL-3.0-only (plus linking exception) |
| Python | 3.12 | PSF-2.0 |
| mcp (MCP Python SDK) | 2.0.0 | MIT |
| uvicorn | 0.52.1 | BSD-3-Clause |
| starlette | 1.4.1 | BSD-3-Clause |
| pydantic | 2.13.4 | MIT |
| httpx | 0.28.x | BSD-3-Clause |
| platformdirs | 4.3.x | MIT |
| psutil | 6.1.x | BSD-3-Clause |
| pywin32 | 312 | PSF-2.0 |
| PyInstaller (构建工具，产物为独立 onedir) | 6.11.x | GPL-2.0-or-later (bootloader 例外，产物不传染) |

完整许可证文本随各组件分发（site-packages 内 `LICENSE`/`LICENSE.txt`）。

## Engine npm dependencies (bundled with `third_party/codexpro/node_modules`)

| Package | Version | License |
|---|---|---|
| @esbuild/win32-x64 | 0.28.1 | MIT |
| @hono/node-server | 2.0.12 | MIT |
| @modelcontextprotocol/sdk | 1.30.0 | MIT |
| @types/body-parser | 1.19.6 | MIT |
| @types/connect | 3.4.38 | MIT |
| @types/cors | 2.8.19 | MIT |
| @types/express | 5.0.6 | MIT |
| @types/express-serve-static-core | 5.1.1 | MIT |
| @types/http-errors | 2.0.5 | MIT |
| @types/node | 24.13.2 | MIT |
| @types/qs | 6.15.1 | MIT |
| @types/range-parser | 1.2.7 | MIT |
| @types/send | 1.2.1 | MIT |
| @types/serve-static | 2.2.0 | MIT |
| accepts | 2.0.0 | MIT |
| ajv | 8.20.0 | MIT |
| ajv-formats | 3.0.1 | MIT |
| balanced-match | 4.0.4 | MIT |
| body-parser | 2.3.0 | MIT |
| brace-expansion | 5.0.8 | MIT |
| bytes | 3.1.2 | MIT |
| call-bind-apply-helpers | 1.0.2 | MIT |
| call-bound | 1.0.4 | MIT |
| content-disposition | 1.1.0 | MIT |
| content-type | 1.0.5 | MIT |
| cookie | 0.7.2 | MIT |
| cookie-signature | 1.2.2 | MIT |
| cors | 2.8.6 | MIT |
| cross-spawn | 7.0.6 | MIT |
| debug | 4.4.3 | MIT |
| depd | 2.0.0 | MIT |
| dunder-proto | 1.0.1 | MIT |
| ee-first | 1.1.1 | MIT |
| encodeurl | 2.0.0 | MIT |
| esbuild | 0.28.1 | MIT |
| escape-html | 1.0.3 | MIT |
| es-define-property | 1.0.1 | MIT |
| es-errors | 1.3.0 | MIT |
| es-object-atoms | 1.1.2 | MIT |
| etag | 1.8.1 | MIT |
| eventsource | 3.0.7 | MIT |
| eventsource-parser | 3.1.0 | MIT |
| express | 5.2.1 | MIT |
| express-rate-limit | 8.5.2 | MIT |
| fast-deep-equal | 3.1.3 | MIT |
| fast-uri | 3.1.4 | BSD-3-Clause |
| finalhandler | 2.1.1 | MIT |
| forwarded | 0.2.0 | MIT |
| fresh | 2.0.0 | MIT |
| function-bind | 1.1.2 | MIT |
| get-intrinsic | 1.3.0 | MIT |
| get-proto | 1.0.1 | MIT |
| gopd | 1.2.0 | MIT |
| has-symbols | 1.1.0 | MIT |
| hasown | 2.0.4 | MIT |
| hono | 4.12.32 | MIT |
| iconv-lite | 0.7.2 | MIT |
| inherits | 2.0.4 | ISC |
| ip-address | 10.2.0 | MIT |
| ipaddr.js | 1.9.1 | MIT |
| is-promise | 4.0.0 | MIT |
| isexe | 2.0.0 | ISC |
| jose | 6.2.3 | MIT |
| json-schema-traverse | 1.0.0 | MIT |
| json-schema-typed | 8.0.2 | BSD-2-Clause |
| math-intrinsics | 1.1.0 | MIT |
| media-typer | 1.1.0 | MIT |
| merge-descriptors | 2.0.0 | MIT |
| mime-db | 1.54.0 | MIT |
| mime-types | 3.0.2 | MIT |
| minimatch | 10.2.5 | BlueOak-1.0.0 |
| ms | 2.1.3 | MIT |
| negotiator | 1.0.0 | MIT |
| object-assign | 4.1.1 | MIT |
| object-inspect | 1.13.4 | MIT |
| once | 1.4.0 | ISC |
| on-finished | 2.4.1 | MIT |
| parseurl | 1.3.3 | MIT |
| path-key | 3.1.1 | MIT |
| path-to-regexp | 8.4.2 | MIT |
| pkce-challenge | 5.0.1 | MIT |
| proxy-addr | 2.0.7 | MIT |
| qs | 6.15.2 | BSD-3-Clause |
| range-parser | 1.2.1 | MIT |
| raw-body | 3.0.2 | MIT |
| require-from-string | 2.0.2 | MIT |
| router | 2.2.0 | MIT |
| safer-buffer | 2.1.2 | MIT |
| send | 1.2.1 | MIT |
| serve-static | 2.2.1 | MIT |
| setprototypeof | 1.2.0 | ISC |
| shebang-command | 2.0.0 | MIT |
| shebang-regex | 3.0.0 | MIT |
| side-channel | 1.1.1 | MIT |
| side-channel-list | 1.0.1 | MIT |
| side-channel-map | 1.0.1 | MIT |
| side-channel-weakmap | 1.0.2 | MIT |
| statuses | 2.0.2 | MIT |
| toidentifier | 1.0.1 | MIT |
| tsx | 4.22.4 | MIT |
| type-is | 2.1.0 | MIT |
| typescript | 5.9.3 | Apache-2.0 |
| undici-types | 7.18.2 | MIT |
| unpipe | 1.0.0 | MIT |
| vary | 1.1.2 | MIT |
| which | 2.0.2 | ISC |
| wrappy | 1.0.2 | ISC |
| zod | 3.25.76 | MIT |
| zod-to-json-schema | 3.25.2 | ISC |