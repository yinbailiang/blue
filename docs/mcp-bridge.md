# MCP 桥接（Phase 1：客户端方向）

> 把一个或多个外部 **MCP server**（stdio 传输）的工具接进 blue 工具目录，
> 让 agent 用与本地工具完全一致的方式调用它们。
> 服务实现见 `services/mcp_bridge/`；本文只记录**设计决策与已知边界**。

## 1. 决策摘要

| 维度 | 取态 | 理由 |
| --- | --- | --- |
| 方向 | 客户端（外部 MCP → blue 目录） | 复用度最高，且不把 `shell` / `write_file` 这类无沙箱工具暴露到进程外；「blue → 外部 IDE」是独立课题 |
| 形态 | 单服务 `services/mcp_bridge/`，`kind = "tool"`，一进程托管全部 server | 配置集中、目录版本统一；server 级故障用进程内退避重连隔离，不必为每个 server 起一个受控进程 |
| 多工具承载 | 从 `blue.worker.ToolWorker` 抽出 `ToolRegistrar`（租约 / 心跳 / 注销） | `ToolWorker` 有「一进程一工具」的硬断言，但注册中心本身支持多工具；只把「一条租约」放宽成「一组租约」 |
| 工具名 | `<server>__<tool>` | 注册中心对同名工具是 last-writer-wins：两个 server 提供同名工具会互相驱逐，`catalog.changed` 版本抖动，agent 可能静默路由到错误后端 |
| 租约标识 | `mcp.<server>.<tool>`，**确定性无随机段** | 进程重启后租约不变，注册中心不会留下「新进程接管旧记录」的噪声 |
| 工具集 | **声明式白名单** `tools = [...]` | 见 §2 |
| 运行期目录 | 不跟随 `tools/list_changed` | 见 §2 |
| 事件协议 | 不新增事件，`depends_contracts = ["tool"]` | 桥产生的仍是 `tool.register` / `tool.heartbeat` / `tool.unregister` / `tool.result` |
| 依赖 | 零新增运行时依赖 | 见 §3 |

## 2. 为什么工具集必须是声明式的

blue 的一切都是「`blue.infd` 声明期望状态 → supervisor 调谐」。若让外部 MCP server
在运行期单方面改写可用工具集，会同时输掉三件事：

1. **可复现性**：同一会话在 server 升级前后可用工具不同，问题极难归因；
2. **可审计性**：`blue config-check` 再也回答不了「这次部署能干什么」；
3. **目录稳定性**：`catalog.changed` 频繁抖动会打乱 agent 的目录缓存。

因此：

| 场景 | 行为 |
| --- | --- |
| 白名单里的工具，server 未提供 | **启动失败**（fail loud，`blue status` 可见），不静默少一个 |
| server 提供了但未声明 | 只 `logger.warning`，不注册 |
| 运行期收到 `tools/list_changed` | 仅记日志；不向 server 声明 `listChanged` 能力，也不依赖该通知 |
| 声明了 server 但 `tools` 为空 | 启动失败（配置自相矛盾） |
| 全部 server `enabled = false` | 以空目录运行，不影响其它工具 |

对齐动作只发生在 `blue apply`（与「源码指纹变化才重启」同构）。

## 3. 为什么不用官方 `mcp` SDK

官方 SDK（`mcp==2.3.0`）会引入 opentelemetry、uvicorn、starlette、pyjwt、
sse-starlette 等一串传递依赖，而桥只用到客户端侧 stdio 的三个方法。
本仓库当前环境亦无法离线解析该依赖链。

`services/mcp_bridge/client.py` 因此自带最小实现：

- 传输：stdio + newline-delimited JSON-RPC 2.0；
- 方法：`initialize`（+ `notifications/initialized`）、`tools/list`（自动翻页）、`tools/call`；
- 服务端主动发来的**通知**记日志、**请求**一律回 `-32601`（避免对方无限等待）；
- stderr 收进环形缓冲，失败时把最后几行拼进错误文本。

会话以 `McpSession` 协议暴露，`McpConnector` 负责「配置 → 会话」。要换成官方 SDK
只需实现一个 `McpConnector`（或一个 `McpSession` 实现类），桥的其余部分不动。

## 4. 数据流

```text
blue.infd  tools = ["fs__read_file"]        servers = [{name = "fs", command = "npx", ...}]
   │
   ▼ supervisor  →  services/mcp_bridge/service_runtime_config.json (0600)
McpBridge.start()
   ├─ build_runtime("mcp_bridge", TOOL_EVENTS) → 连总线
   ├─ ToolCallHandler(self)                 → 订阅 tool.call（按名字过滤）
   ├─ 每个 server: spawn 子进程 → initialize → tools/list（翻页）
   ├─ 白名单求交 → schema 归一化 → ToolDefinition
   └─ ToolRegistrar.announce()              → tool.register × N + 心跳
                                      │
agent ── tool.call ───────────────────┴──> McpBridge.handle_call
                                                └─ tools/call → content 降维
                                      ┌────────── tool.result（必达，成功或失败）
agent <───────────────────────────────┘
```

## 5. schema 归一化：复用 + 薄适配

转换本体复用 `infinity_agent.tools.introspectionist.convert`：

- `resolve_refs`：内联 `#/$defs/`，深度上限 32，循环引用截断为
  `object` + `additionalProperties=True` + 说明；
- `prop_from_schema`：`anyOf`/`oneOf`、`nullable`、`enum`、标量 `default`、
  `minimum/maximum/exclusive*/pattern/minLength/maxLength/examples`、
  嵌套 `properties`/`required`、`items`、`additionalProperties`。

`services/mcp_bridge/schema.py` 只补它不做、且**实测**会出问题的那部分：

| 输入形态 | 不处理的后果 | 桥的处理 |
| --- | --- | --- |
| `definitions` + `#/definitions/X` | 静默退化成 `type="string"` | 前置并入 `$defs`、改写 `$ref` |
| `{"type": "array"}`（合法） | `ValidationError` 崩溃 | 补 `items={}` |
| `prefixItems` / 元组式 `items` | `ValidationError` 崩溃 | 降级为任意元素 |
| `{"type": "null"}` | `ValidationError` 崩溃 | 降级为 `string`（`anyOf` 内的 null 分支保留给库转 `nullable`） |
| `allOf` | 静默变 `string` | 单分支内联；多对象分支合并；否则丢弃并留痕 |
| `const` | 静默变 `string` | 转单值 `enum` |
| `type: [T1, T2]` | 静默变 `string` | 转 `anyOf` |
| `format` / `minItems` / `not` / `if-then-else` 等 | 丢弃 | 丢弃但记录；`format` 搬进 `description` |
| 单个属性转换抛错 | 整个工具注册失败 | 该属性降级为 `string`，其余参数不受影响 |
| 顶层不是 object | 参数语义丢失 | 包装成唯一参数 `input` |

**降级可见**：所有说明汇总进 `SchemaConversion.notes`，并追加到工具
`description` 末尾（`[blue] 参数 schema 已降级：…`），模型与人都能看到；同时
逐条 `logger.warning`。

## 6. 失败语义

设计目标是「永远不要留下没有应答的 `tool.call`」——否则 agent 会一直等到
`tool_timeout`（默认 1800s），并在会话里留下悬空 `tool_calls`（见
`.agents/MEMORY/memorys/known_pitfalls.md`）。

| 场景 | 行为 |
| --- | --- |
| server 掉线 / 握手未完成 | **立即** `tool.result(success=false)`，不等超时 |
| `tools/call` 超时（`call_timeout`，默认 60s） | 立即失败，**不**丢弃会话（慢 ≠ 死） |
| `tools/call` 返回 JSON-RPC error | 立即失败，带错误码 |
| `tools/call` 返回 `isError: true` | `success=false`，`error_msg` = 文本 |
| 传输中断（进程退出 / 管道破裂） | 立即失败并丢弃会话，下次调用或巡检触发重连 |
| 桥进程崩溃 | supervisor 重启；期间注册中心按 TTL（默认 15s）摘除工具 |
| server 反复失败 | 进程内指数退避（`reconnect_backoff` → `_max`），不拖垮其它 server |

## 7. 已知缺口（明确不做，不是遗漏）

- **取消**：`session.cancel` 无法传播成 MCP 的 `notifications/cancelled`。
  `ToolCallHandler` 没有取消通道，`tool.call` 也只有请求/结果两种事件。
- **进度**：MCP `progress` 通知无处投射（`tool.call` 不携带中间态）。
- **资源与提示词**：`resources/*`、`prompts/*` 未接入，只接 `tools/*`。
- **HTTP 传输**：只实现 stdio；streamable HTTP 需要鉴权与长连接管理，留待后续。
- **`tools/list_changed`**：按 §2 只记日志。

## 8. 验证

```bash
uv run pytest services/mcp_bridge/tests -q   # 72 条
```

- `test_bridge_schema.py`：27 条表驱动，覆盖上表每一行（崩溃类与失真类分开断言）；
- `test_bridge_client.py`：8 条，用真实 stdio 子进程（`tests/fake_mcp_server.py`）
  验证翻页、content 降维、`isError`、JSON-RPC error、超时、关闭幂等、stderr 采集；
- `test_bridge.py`：注册 / 调用 / 超时 / 掉线 / 未知工具忽略 / fail-loud /
  重连 / 巡检 / 停机注销 / 降级可见，外加一条**端到端**（真实子进程 + 进程内总线 + 注册中心）；
- `test_bridge_config.py`：命名规则、配置一致性、模板默认值与运行时模型不漂移；
- `tests/test_worker.py`：`ToolRegistrar` 的多租约登记/续约/注销，以及
  `ToolWorker` 行为不变。

## 9. 反向：blue 工具 → 外部 MCP 客户端

`services/mcp_server/` 是反方向，服务名 `mcp_server`：把 blue 的**工具目录**
暴露成一个 stdio MCP server，给 Claude Code / Cursor / Claude Desktop 这类客户端用。

```text
tools/list  ←  catalog.query（按 expose 白名单过滤）→ ToolParameters 转 JSON Schema
tools/call  →  tool.call（总线 RPC）→ tool.result → content 块 + isError
```

与客户端桥的对称关系：一个把 MCP 工具搬进 blue，一个把 blue 工具搬出去；
两者都不新增事件契约，都只依赖 `catalog.*` / `tool.*`。

### 关键决策

| 维度 | 取态 | 理由 |
| --- | --- | --- |
| 运行方式 | 手工运行的 `kind = "ui"` 服务（同 `chat` / `inspect`），不纳入 supervisor | stdio 模式必须独占 stdout，让日志泵托管它只会互相添乱 |
| 配置来源 | 命令行参数（`--expose` / `--timeout` / `--allow-unsafe`），**不读** `service_runtime_config.json` | 它不是 supervisor 调谐的对象，没有投影；模板留空、不定义 `CONFIG_MODEL` |
| 工具集 | 必须显式 `--expose a,b,c`，不支持「全部」 | 与客户端桥一致：能干什么要写下来 |
| 危险工具 | `run_shell` / `shell` / `write_file` / `edit_file` 默认拒绝，需 `--allow-unsafe` | 这些工具以工具进程权限写文件或执行命令，交出去等于交出宿主机 |
| 调用前校验 | 先查目录，白名单有但目录无的工具**立即失败** | 否则会变成「没有应答的 `tool.call`」→ 客户端挂到超时 |
| 并发 | 每个请求一个任务，串行写出 | 慢工具不堵 `ping` / `tools/list`；stdio 不能被并发写坏行 |
| stdout | 只允许协议帧 | 任何多余输出都会破坏 MCP 帧格式（日志、诊断一律 stderr） |

### 用法

```bash
# 先看目录里有哪些工具（写 stderr，stdout 留给协议）
BLUE_BUS_URL=amqp://guest:guest@127.0.0.1:5672/blue \
  uv run --package blue-service-mcp-server blue-service-mcp-server --list-tools

# 作为 stdio MCP server 运行
BLUE_BUS_URL=amqp://guest:guest@127.0.0.1:5672/blue \
  uv run --package blue-service-mcp-server \
  blue-service-mcp-server --expose now,text_stats,get_weather
```

客户端配置（Claude Code / Cursor 的 MCP 段）就是上面那条命令 + 环境变量。

### 已知边界

- `notifications/cancelled` 只记日志：`tool.call` 没有取消通道，取消无法传播；
- 未实现 `resources/*`、`prompts/*`、`prompt` 采样与 progress；
- 只支持 stdio 传输（HTTP 变体需要鉴权与长连接管理）；
- `tools/list` 不做 `listChanged` 广播（目录变化由客户端重新拉取）。

### 踩过的坑：服务名 ≠ 工具名

`--expose` 收的是**工具名**，而服务名常常不同：

| 服务名 | 工具名 |
| --- | --- |
| `clock` | `now` |
| `weather` | `get_weather` |
| `shell` | `run_shell` |
| `text_stats` / `read_file` / `write_file` / `edit_file` | 同名 |

第一次联调就是拿服务名当工具名，结果 `tools/list` 空、`tools/call` 一直等到超时。
现在 `--list-tools` 会列出真实工具名并给危险项打 `[danger]`，`tools/call` 也会在
调用前核对目录并立即报错。

### 验证

```bash
uv run pytest services/mcp_server/tests -q   # 34 条
```

- `test_server.py`：握手 / 白名单过滤 / schema 转换 / 成功与 `isError` /
  未暴露工具拒绝 / 白名单有但目录无 → 立即失败 / 未知方法 / `ping` / 解析错误 /
  「租约在但无人应答」超时 / 慢调用不堵 `ping` / EOF 后总线关闭；
- `test_server_schema.py`：反向 schema 映射，并用客户端桥的正向转换做往返；
- `test_server_config.py`：白名单必填、重复与非法名、危险工具门禁、超时与未知字段。

真实进程冒烟（本机 broker + 只读工具）：

```text
initialize → {"protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": false}}, ...}
tools/list → ['now', 'text_stats']
tools/call now → {"content": [{"type": "text", "text": "{\n  \"iso\": \"2026-11-03T22:30:58…\"}"}], "isError": false}
tools/call run_shell → {"code": -32602, "message": "工具 'run_shell' 未在 expose 白名单中"}
exit code → 0（stdout 只有协议帧，日志全在 stderr）
```


