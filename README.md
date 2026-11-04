# blue

基于 `infinity-agent` / `infinity-bus` / `infinity-data` 的**分布式 Agent 系统**。

- **控制面**：`blue` 包与 `blue` 命令，负责服务发现、进程编排、状态与日志；
- **工作负载**：`services/<name>/`，每个服务一个独立进程、一个 `pyproject.toml`、
  一个 `service.infd`；
- **声明式配置**：`blue.infd` 描述期望运行哪些服务，控制面像单机 Kubernetes
  一样持续把实际状态调谐到期望状态。

> **边界原则**：控制面只负责编排与总线契约；具体业务（Agent、工具、UI、RabbitMQ
> 管理）全部在工作负载进程内。控制面不导入任何服务实现。

## 架构

```text
blue CLI
  └─ Supervisor（根 blue 进程）
       ├─ DesiredStateLoader  ← blue.infd + services/*/service.infd
       │                         + contracts/*/contract.infd
       ├─ Reconciler          ← desired vs actual
       ├─ ProcessRunner       ← 启动 / 停止 / 重启 / 退避 / 日志
       └─ .blue/              ← state.json + logs + lock

       ├─ services/bus        → 管理 RabbitMQ + 可选 AMQP TCP 代理
       ├─ services/registry   → 工具租约 + 目录快照 + 目录广播
       ├─ services/session    → 会话持久化（aiosqlite + WAL）
       ├─ services/agent      → 模型推理 + 工具目录缓存
       ├─ services/webui      → FastAPI + 单页 Chat UI
       ├─ services/*          → 一个工具一个进程
       └─ services/chat|inspect → 手工运行的 CLI 客户端
```

一次对话的完整链路：

1. WebUI 通过 WebSocket / REST 发布 `session.prompt`；
2. agent 通过 `session.get` 向 session 服务取出/创建会话；
3. session 服务返回历史消息；agent 通过 `catalog.query` 向 registry 取工具目录，
   把目录交给模型；
4. 模型返回 `tool_calls`，agent 发布 `tool.call`；
5. 对应工具进程执行后回发 `tool.result`；
6. agent 把结果回灌模型，循环直到最终答复或 `max_steps`；
7. agent 通过 `session.append` 持久化新消息并发布 `session.reply`。

Agent 在模型流式输出期间同时发布 `session.stream`，携思考增量、文本增量、
工具调用与工具结果；WebUI 通过 WebSocket 即时转发给浏览器。

会话按写前日志落库：assistant 消息（含 `tool_calls`）先入会话库，工具结果再
逐条追加。进程若死在两者之间，历史里会留下没有结果的 `tool_call`，此后每次
请求都会被模型接口以 400「insufficient tool messages」拒收。因此 agent 在把
历史交给模型前先做一次内存自愈（`blue_service_agent.history.sanitize_history`）：
为缺失结果的调用补写占位工具结果、丢弃孤儿 `tool` 消息与空 assistant 消息，
持久化数据本身不改动。

模型若请求了工具目录里不存在的工具，agent 立即回灌错误文本（附可用工具清单），
不再空等到 `tool_timeout`——没有进程会应答这类调用。

生成中点「停止」走 `session.cancel`，agent 取消该会话正在进行的 turn。中断后
只落库真正产出的内容：已流出的文本保留并追加「（已停止）」，一个字都没流出就
整步丢弃；工具执行中被中断时只保留已经跑完的调用与结果，其余丢弃。于是一次
中断既不会留下界面上「看不见但存在」的空壳消息，也不会留下没有结果的
`tool_call`。step 内的 assistant 消息与其全部工具结果是一次性提交的，中途被杀
最多丢掉当前这一步。

工具进程启动时发布 `tool.register`，周期补发注册并发布 `tool.heartbeat`，
退出时发布 `tool.unregister`；agent 按 TTL 回收失联工具。

## 目录结构

```text
contracts/                    # 域契约包（不依赖控制面/服务实现）
├── tool/                     # blue-contracts-tool：tool.* / catalog.*
│   └── contract.infd         # 契约元数据
└── session/                  # blue-contracts-session：session.*
    └── contract.infd

src/blue/                     # 控制面 + 共享 SDK（随包安装）
├── transport.py              # RabbitMQ fanout / 进程内 fanout
├── lifecycle.py              # serve_forever
├── worker.py                 # ToolWorker / ToolRegistrar（工具服务使用）
├── runtime.py                # 服务侧运行时：读配置、连总线、run_tool
├── config.py                 # blue.infd 入口与文档导出
├── errors.py                 # BlueError / ConfigError / ...
├── control/
│   ├── model.py              # ServiceMeta / ContractMeta / DesiredState
│   ├── desired.py            # 编译 blue.infd + 加载 service/contract 元数据
│   ├── reconciler.py         # 调谐循环
│   ├── runner.py             # 进程、进程树、日志泵
│   ├── state.py              # state.json / 单实例锁 / 孤儿清理
│   ├── logs.py               # 日志轮转 / tail / follow
│   ├── health.py             # 进程 / TCP 就绪探针
│   ├── process.py            # 进程树信号
│   └── backoff.py            # 指数退避 + 重启策略
├── cli.py                    # up / apply / status / logs / down / ...
└── subscribers/
    └── tool_call.py          # 工具调用处理器

services/                     # 工作负载（不随 blue 包安装）
├── bus/                      # RabbitMQ 生命周期 + 配置 + 可选代理
├── registry/                 # 工具租约 + 目录快照 + 目录广播
├── session/                  # SQLite 会话存储（WAL）
├── agent/                    # 模型推理 + 工具目录缓存
├── webui/                    # FastAPI 后端 + 静态单页应用
├── clock/ echo/ text_stats/ weather/
├── read_file/ write_file/ edit_file/ shell/
├── mcp_bridge/               # MCP 客户端桥：外部 MCP server → 工具目录
├── chat/ inspect/            # 手工运行的 UI 客户端
└── */service.infd            # 服务模板 + 系统元数据
```

每个服务目录：

```text
services/echo/
├── pyproject.toml
├── service.infd
├── src/blue_service_echo/
└── tests/
```

每个契约目录：

```text
contracts/tool/
├── pyproject.toml
├── contract.infd
├── src/blue_contracts_tool/
└── tests/            # 可选
```

## 配置模型

### `blue.infd`：系统期望状态

顶层键就是服务名，也是 `services/<name>/` 目录名；`supervisor` 是唯一保留的
非服务顶层键。每个服务的值是 `services/<name>/service.infd` 中配置模板的一次
实例化。

```infd
# 密钥来自 secrets.toml（已 gitignore），见「密钥管理」一节
!file p"secrets.toml" as toml import .agent.api_key as agent_api_key, .bus.password as bus_password

!from p"services/bus/service.infd" import ServiceTemplate as BusService
!from p"services/session/service.infd" import ServiceTemplate as SessionService
!from p"services/agent/service.infd" import ServiceTemplate as AgentService
!from p"services/webui/service.infd" import ServiceTemplate as WebUIService
!from p"services/echo/service.infd" import ServiceTemplate as EchoService

supervisor {
  state_dir: str = ".blue"
  reconcile_interval: <float, positive> = 2.0
  shutdown_timeout: <float, positive> = 10.0
  restart_backoff: <float, positive> = 0.5
  restart_max_attempts: <int, range(1, 100)> = 10
  registry_ttl: <float, positive> = 15.0
}

bus = BusService(mode = "managed", vhost = "blue", password = $bus_password)
session = SessionService()
agent = AgentService(api_key = $agent_api_key, max_steps = 8)
webui = WebUIService()
echo = EchoService()
```

- 键存在即启用；删除键即停止并从状态中清理。
- `!from` 只导入模板，不导入数据；`service.infd` 里的 `service` 数据块不会污染
  核心配置。
- `!file` 从工作区文件导入数据，沙盒只授权 `secrets.toml` / `secrets.json`
  两个具体文件；导入其它文件会直接报错。
- 顶层值必须满足对应模板（编译期由 infinity-data 校验）。

### `service.infd`：服务模板 + 通用系统元数据

```infd
~ServiceTemplate {
  value: str = "default"
}

service {
  name = "echo"
  kind = "tool"                 # infra / agent / session / tool / ui
  package = "blue-service-echo" # uv workspace 包名
  module = "blue_service_echo"  # python -m 模块
  depends_on = ["bus"]
  depends_contracts = ["tool"]  # 依赖的事件契约名
  restart = "always"            # always / on-failure / never
  stop_timeout = 5.0
  startup_timeout = 30.0
  readiness = "process"         # process / tcp / none
  ready_tcp = null              # bus 可省略，从 url/proxy_listen 推导
  env = {}
}
```

- `~ServiceTemplate`：该服务的**配置 schema**，只放服务特有配置。
- `service`：**通用系统元数据**，由控制面的 `ServiceMeta` 校验；不是服务配置。
- 所有配置实例只存在于 `blue.infd`，`service.infd` 不保存配置实例。

### `contract.infd`：契约包元数据

契约包不由 `blue.infd` 显式实例化；控制面扫描 `contracts/*/contract.infd`
自动纳入期望状态：

```infd
contract {
  name = "tool"
  package = "blue-contracts-tool"
  module = "blue_contracts_tool"
  description = "工具与目录领域事件契约"
}
```

- 控制面为每个契约包计算 `source_hash`（`pyproject.toml` + `contract.infd` +
  `src/**/*.py`），并把契约纳入 generation。
- 服务用 `depends_contracts` 声明自己依赖的契约。
- `blue apply` / `reload()` 检测到契约变化时，会重启所有声明了该契约的服务；
  没有声明依赖的服务不受影响。
- 契约依赖必须指向存在的契约，否则配置编译直接失败。

### 密钥管理

所有密钥集中在项目根的 `secrets.toml`（或 `secrets.json`）。该文件已被
`.gitignore` 忽略，模板见 `secrets.example.toml`：

```toml
[agent]
api_key = "sk-..."

[bus]
password = ""
```

`blue.infd` 通过 `!file` 显式导入所需字段：

```infd
!file p"secrets.toml" as toml import .agent.api_key as agent_api_key, .bus.password as bus_password
```

约定：

- **文件或键缺失 → 取值为 `null`（noexist）**。编译只产生 warning、不失败；
  需要密钥的服务在启动时给出明确错误，而不是静默退化成空字符串。
- **越权导入直接报错**：沙盒 `allow_files` 只授权 `secrets.toml` /
  `secrets.json` 两个具体文件，从工作区其它文件 `!file` 导入会以
  `sandbox.access_denied` 失败（fail loud）。
- **不再读环境变量**：`agent` 的 `api_key`、`bus` 的 `password` 都只认配置值。
  `bus` 的 `user = "guest"` 且密码留空时回退到 RabbitMQ 默认密码 `guest`，
  其它用户留空即报错。
- 想改用 JSON：把导入语句换成
  `!file p"secrets.json" as json import ...` 即可，其余不变。
- 密钥只在 supervisor 编译期读取，随后写进 `service_runtime_config.json`
  （`0600`，已 gitignore），服务进程只读该文件。

### 运行时配置投影

supervisor 编译 `blue.infd` 后，把每个服务的配置实例写入：

```text
services/<name>/service_runtime_config.json
```

- 仅 supervisor 写入，服务进程只读；
- 原子替换、权限 `0600`、已加入 `.gitignore`；
- 内容变化才重写，并触发对应服务重启；
- 服务被移出期望状态时删除；
- 服务通过 `BLUE_CONFIG_FILE` 或 cwd 下的默认文件名读取。

`agent` 的配置示例：

```json
{
  "api_key": "sk-...",
  "base_url": null,
  "max_steps": 8,
  "model": "deepseek-v4-flash",
  "provider": "deepseek",
  "llm_timeout": 300.0,
  "llm_connect_timeout": 10.0,
  "llm_retries": 1,
  "llm_http_retries": 3,
  "queue_timeout": 60.0,
  "compact_history": true,
  "compact_keep_messages": 300,
  "compact_text_chars": 200,
  "compact_error_chars": 600,
  "compact_target_chars": 1200000,
  "session_timeout": 10.0,
  "system_prompt": "你是 blue，一个通过工具完成任务的智能体。",
  "tool_timeout": 30.0
}
```

`session` 的配置示例：

```json
{
  "database_path": "sessions.db",
  "max_sessions": 512,
  "idle_ttl": 7200.0,
  "prune_enabled": false,
  "prune_interval": 60.0,
  "prune_batch_size": 200,
  "active_grace": 60.0,
  "busy_timeout_ms": 5000
}
```

- `llm_timeout` / `llm_connect_timeout`：模型请求的读/连接超时。底层库默认读
  超时只有 **60s**，长回答或大上下文会撞上 `SocketTimeoutError`，所以 blue
  默认放到 300s，并把这两个值显式写进 `blue.infd`；
- `llm_retries`：流式生成在**还没收到任何内容**时允许的重放次数（此时向前端
  推过零个增量，重放安全）；已经出字后中断则不重放，改为把已生成内容落库；
- `llm_http_retries`：HTTP 层重试（429/5xx，由底层客户端退避），与
  `llm_retries` 是两套机制；
- `compact_history`（默认 `true`）：发送前压缩历史里的工具往返。blue 的工具
  结果几乎都是 JSON，所以按**字段**投影而不是猜文本：`path`/`exit_code`/
  `success` 原样保留，`stdout`/`content` 截成「开头 + `…[N chars omitted]`」，
  失败时额外保留结尾（错误通常在尾部），列表只留前几条，`write_file` 的正文
  直接省略（文件已落盘）。**只压较早的消息**，尾部 `compact_keep_messages`
  （默认 300 条）原样发送——模型正在用的工具结果不能被截断。
- `compact_target_chars`（默认 `1200000`，`0` = 不限）：提示体量的**预算**。
  只按条数保留是不够的：1700 条历史里每条哪怕只剩 200 字，加起来也有 1 MB。

  超预算的处理是**整步丢弃最旧的对话**，并在原位留一句
  `[早期历史已省略：N 步 / M 条消息…]`：
  - 为什么不是「把每条旧消息截成一行」：工具结果几乎都是 JSON，截前 120 字拿到的是
    `{` 和第一个键名，既没信息量、又和「按字段投影」那层语义重复；
  - 为什么必须整步丢：assistant 的 `tool_calls` 和它的 `tool` 结果一旦被拆开，
    下游会直接报「tool_call 没有对应结果」；
  - 永远保留：系统提示、最后一步（当前这轮的输入）。预算小到最后一步都放不下时
    不丢它，如实承认超预算。

  数值怎么定：`agent` 每步会打印实测的「字符/token」比例。真实会话里工具结果是
  JSON，实测 **1.6 字符/token**（不是纯文本的 2.6），所以 100 万 token 只相当于
  160 万字符。默认 120 万字符 ≈ 74 万 token，给 1M 窗口留出工具 schema 与回复的
  余量。换个模型/换个内容构成时，看日志里的比例重算一次即可。

  实测一条 1742 条消息的会话：原始 2967 KB，预算 200 万 → 1009 KB（不丢任何消息）；
  预算 60 万 → 584 KB / 丢 1136 条；预算 20 万 → 195 KB / 丢 1673 条。

  投影分界还按 50 条取整（`COMPACT_BOUNDARY_STEP`）：分界如果每来一条消息就挪一格，
  等于每次调用都把第 `len-keep` 条改写一遍，上游的 prompt 前缀缓存会从那里整段失效。
- `database_path` 为相对路径时解析到 `BLUE_STATE_DIR`，默认
  `.blue/sessions.db`；
- **`prune_enabled` 默认 `false`：不会自动删除任何会话或附件**，磁盘占满前
  不会丢历史；需要自动回收时显式打开；
- 打开后：`idle_ttl` 秒没活动的会话被回收，超过 `max_sessions` 时淘汰最久
  未活动的会话；`active_grace` 秒内活动过的会话在容量淘汰时受保护（避免误删
  正在跑长任务的对话）；`prune_batch_size` 限制单次回收数量，避免长事务锁库；
- SQLite 使用 WAL + `BEGIN IMMEDIATE`，一个 session 服务进程独占数据库；
- 消息以 `blue.messages` 编码后的 JSON 文本存储，保证 tool_calls 可 round-trip。

`shell` 的配置示例：

```json
{
  "workdir": null
}
```

- `workdir` 为 `null` 时使用**项目根**（`blue.infd` 所在目录，supervisor 通过
  `BLUE_PROJECT_ROOT` 注入），而不是服务进程所在的 `services/shell`；
- 相对路径按项目根解析，绝对路径直接使用；
- 单次调用仍可用工具参数 `cwd` 覆盖默认值。

`read_file` / `write_file` / `edit_file` 的配置示例与 `shell` 同形：

```json
{
  "workdir": null
}
```

- 三个文件工具共用 `blue.runtime.WorkspaceConfig`，相对路径同样锚定**项目根**，
  而不是工具进程所在的 `services/<name>`；`~` 与绝对路径不受影响；
- 这三类工具没有覆盖参数，相对路径的语义完全由 `workdir` 决定。

`webui` 的配置示例：

```json
{
  "host": "127.0.0.1",
  "port": 8787,
  "title": "Blue Agent Studio",
  "request_timeout": 600.0,
  "poll_interval": 0.9,
  "startup_timeout": 15.0
}
```

WebUI 通过 WebSocket 调用 Agent，并订阅 `session.stream` 实时展示模型思考、
文本增量、工具调用与工具结果；静态资源随 `blue-service-webui` 包一起安装。
`services/webui` 依赖 `websockets`，否则 uvicorn 无法完成 WebSocket 升级握手。

前端按消息粒度增量渲染：流式增量只更新当前气泡的 DOM，不重建整张消息列表，
因此滚动位置与复制按钮状态在流式输出期间保持稳定。工具调用与工具结果渲染为
可折叠卡片，默认折叠、点击展开，摘要行常驻显示工具名、参数预览或输出首行；
展开状态在后续增量更新中保留。

**中断**：生成期间发送按钮变为「停止」（瞄准图标），点击后 WebSocket 发送
`cancel` 帧，WebUI 转而向总线请求 `session.cancel`；Agent 协作式取消当前 turn
（在下一个 `await` 点生效），广播 `session.stream` 的 `cancelled` 事件并返回
`success=false` 的回复。WebUI 回推 `cancelled` 帧与最新快照，前端复位按钮。

消息正文按 **GFM** 渲染（表格、任务列表、删除线、自动链接等）；**LaTeX 公式**
支持 `$...$` 行内、`$$...$$` 块级，以及 `\(...\)` / `\[...\]` 两种写法，由 KaTeX
排版（含 `htmlAndMathml` 双输出，便于无障碍读取）。代码块内的 `$` 不会被当作
公式。

`marked` 与 `katex` 以离线静态资源 vendored 在
`services/webui/src/blue_service_webui/web/vendor/`，不走 CDN、不需要构建步骤；
版本与升级步骤见该目录的 `README.md`。库文件缺失时前端自动退回内置的简易
渲染器，页面不会白屏。模型输出中的原始 HTML 一律转义，`javascript:` 链接被
中和，外链统一带 `target="_blank" rel="noopener noreferrer"`。

> 运行时配置可能包含密钥，务必保持 `0600` 且不要提交。

### 历史回取工具（history_search / history_read）

压缩会丢掉旧对话，所以需要一条"回取原文"的通道。它由**两个普通工具服务**提供，
和别的工具一样受 registry 管理：

| 服务 | 工具 | 作用 |
|---|---|---|
| `history_search` | `history_search(query, limit=5, roles=...)` | 在本会话历史里按子串搜索，返回命中消息的 `id`、角色、链上序号与上下文片段 |
| `history_read` | `history_read(ids, max_chars=4000)` | 按 id 读回完整原文；超长截断并标注真实长度；缺失 id 单独列出 |

数据与搜索都在 **session 服务**里（`session.search` / `session.read`，SQL `LIKE`
先缩小范围再定位片段），两个工具只是薄包装：拿到工具调用上下文里的
`session_id`，转成一次 RPC。

**工具怎么知道自己在哪个会话**：工具函数的参数 schema 是发给模型看的，不能塞
内部字段，所以上下文走 `blue.tool_context`（`ContextVar`）：`ToolWorker` 在调用前
用 `ToolCallRequest.session_id` 绑定，调用后复位。工具里用 `current_context()`
取；需要反向调用别的服务时用 `call_service(...)`，并把那个契约包的事件传给
`run_tool(..., events=...)`。

> 为什么不做成 agent 内建工具：内建的看不见（不在 `/api/tools` 目录里、webui 与
> MCP 桥都拿不到）、改一次要重启 agent、也没有租约/审计。实测内建方案每轮固定
> 多付 28.5 ms（解码整条链），而服务方案只在真调用时付 ~12.5 ms。

### 反向：把 blue 工具暴露给外部 MCP 客户端

`mcp_server` 是反方向——把 blue 的工具目录变成一个 stdio MCP server，给
Claude Code / Cursor 这类客户端用。它手工运行、不纳入 supervisor 调谐：

```bash
# 先看目录里有哪些工具（注意：--expose 收的是工具名，不是服务名）
BLUE_BUS_URL=amqp://guest:guest@127.0.0.1:5672/blue \
  uv run --package blue-service-mcp-server blue-service-mcp-server --list-tools

# 作为 stdio MCP server 运行
BLUE_BUS_URL=amqp://guest:guest@127.0.0.1:5672/blue \
  uv run --package blue-service-mcp-server \
  blue-service-mcp-server --expose now,text_stats,get_weather
```

- `--expose` 必须显式列出，不支持「全部」；
- `run_shell` / `write_file` / `edit_file` 默认拒绝，需 `--allow-unsafe`；
- `tools/list` 走 `catalog.query`，`tools/call` 走 `tool.call`，调用前先核对目录，
  白名单有但服务没跑的工具立即报错而不是挂到超时；
- stdout 只承载协议帧，日志与 `--list-tools` 输出都走 stderr。

>
> **网络提示**：`weather` 会向 `wttr.in` 发起 HTTPS 请求；如需换源或镜像，
> 在 `blue.infd` 里传 `WeatherService(base_url = ..., timeout = ...)`。

## 新增服务

1. 创建 `services/<name>/`，加入 `pyproject.toml`（workspace 成员，依赖 `blue`）；
2. 写 `service.infd`：`~ServiceTemplate` 配置模板 + `service` 元数据；
3. 在 `src/blue_service_<name>/` 实现入口；工具服务直接调用
   `blue.runtime.run_tool`；
4. 在 `blue.infd` 中 `!from` 导入模板并实例化 `<name> = ...`；
5. 运行 `uv sync --all-packages`、`uv run blue config-check`；
6. 登记到 `pyproject.toml` 的 `[tool.pyright] extraPaths`，以及
   `tests/test_service_manifests.py` 的 `SERVICES` / `MANAGED` /
   `EXPECTED_CONTRACTS`（三处都不写，类型检查与清单测试会漏掉新服务）。

服务名必须是合法 `.infd` 标识符：`[a-z][a-z0-9_]*`，并与目录名、
`service.name` 完全一致。

## 测试

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
```

- `tests/`：控制面、配置、状态、进程管理、调谐、CLI；
- `services/*/tests/`：各服务单元测试；
- `services/webui/tests/`：FastAPI REST 与 WebSocket 协议测试；
- `tests/test_service_manifests.py`：服务目录、manifest、配置契约；
- RabbitMQ 集成测试在 broker 不可达时自动跳过，可用 `BLUE_RABBIT_URL` 覆盖；
  默认使用项目的 `blue` vhost（也可通过 `BLUE_BUS_URL` 指定）。

## 工程约束

- Python 3.14 / uv workspace / ruff / pyright strict / pytest
- `src/blue/` 禁止 `# type: ignore` 与 `# pyright: ignore`
- 控制面不导入服务实现；服务不读取 `blue.infd`
- 前端无构建链：`web/` 下的静态资源直接分发，第三方库 vendored 在 `web/vendor/`，
  升级时必须同步该目录的 `README.md` 与许可证文件
- 生成物（`.blue/`、`services/*/service_runtime_config.json`）已在 `.gitignore`

### 定位列（会话搜索 / 快速跳转）

长会话右侧有一条定位列（顶栏 `◎` 开关，记住开合状态）：

- 搜索框为空：按顺序列出**每一轮提问**当目录，分支起点还会带上分支名
- 有搜索词：在用户/助手正文里做大小写不敏感的子串搜索，命中片段高亮，最多 80 条
- 点任意一条：虚拟滚动按高度缓存算出位置，渲染后再用实测位置校正一次，
  目标消息落在视口顶部并闪一下

搜索只搜已经加载在内存里的消息（会话快照整棵在本地），不额外走后端。

### 分支命名

一个父节点下有多个孩子就是多条分支，界面上原本只能靠 `2 / 3` 这样的序号区分。
现在每条分支可以起名：分支条上的 `✎` 就地改名（24 字上限，留空恢复序号），
名字存在 `messages.label` 上，重开浏览器/换设备都在。

删分支时连带回收只被这条分支引用的附件（`attachments` 里的 blob），被别的消息
共用的图会保留 —— 否则删了分支，图还占着库。

## 多会话与发件箱

前端不再是「一个活会话槽」：每个会话有自己的一份界面状态（消息、树、忙碌、
草稿、队列、未读、渲染窗口），渲染统一读「当前会话」，WebSocket 帧按
`session_id` 路由到各自的会话。

- **多会话并行**：A 会话生成时切到 B 看历史、发消息、打断都不受影响；A 的流在
  后台继续累积，切回去内容已经在。订阅集合 = 当前会话 + 所有忙碌会话
- **会话标题在服务端**：首条用户消息自动生成（28 字），双击标题就地改名，
  留空回车恢复自动；不再依赖浏览器 localStorage
- **删除两步确认**：第一次点变成「确认」，3 秒后还原
- **草稿跟着会话走**：切走时存下输入框，切回来放回原处
- **长会话窗口化**：默认只渲染尾部 60 条，顶部「加载更早」按需追加

### 发件箱（待发送队列）

会话忙时发的消息不再被丢弃或卡在界面上，而是进一个**有状态**的队列：

| 状态 | 含义 | 前端显示 |
| --- | --- | --- |
| `pending` | 排队中，等 agent 空闲 | 「待发送」 |
| `sending` | 已被 agent 认领，正在跑 | 「发送中」（此时不可编辑、不可取消） |
| `failed` | 跑失败了，原因留在 `error` 里 | 「失败 · 第 N 次」+ 重试按钮 |

- **不丢消息**：认领是「改状态」而不是「删行」，跑完才删；`sending` 超过
  `outbox_claim_timeout`（默认 300 秒）会自动回到 `pending`——agent 崩在任意
  一步都能自愈
- **自动重试**：失败按 `outbox_max_attempts`（默认 3 次，含首次）重排并退避，
  用满就留在队列里等人工
- **谁来跑**：本轮对话结束、agent 启动时扫一遍遗留队列、以及 `session.outbox`
  广播（「立刻发送」和别的端排队）三条路都会拉起队列泵
- **界面**：面板与输入框同宽、同圆角同边框，就在输入框正上方；头部「立即发送」
  「全部取消」，行内点正文可改，悬停出现「立刻」「重试」「✕」；侧栏显示 `⬆ N` 角标

## 对话树

会话不是一条线性消息表，而是一棵树：每条消息记 `parent`（挂在谁下面）与
`current`（当前选中哪个孩子）。沿 `current` 从根走到底，就是界面显示、也是发给
模型的那条**当前链**；在同一个父节点下再挂一个孩子，就得到一条新分支，旧分支
原样保留。几何运算统一放在 `blue.tree`（`Tree` 只读视图：当前链、路径、子树），
session 服务与 agent 共用同一份实现。

```text
root(0)
└─ 1 system
   └─ 2 user「帮我查一下」
      ├─ 3 assistant「第一版回答」   ← 旧分支，保留
      └─ 4 assistant「第二版回答」   ← current 指向它，界面显示它
```

| 概念 | 含义 |
| --- | --- |
| `SessionNode.parent` | 父节点 id；`0` 表示会话根（第一条消息的父节点） |
| `SessionNode.current` | 该节点当前选中的子节点；`None` 表示叶子 |
| `SessionSnapshot.current` | 派生字段：当前链的节点 id（早 → 新），界面直接照它渲染 |
| `SessionSnapshot.nodes` | 全部节点（含未选中的分支），用来画分支切换条 |

协议增量：

- `session.append` 带 `parent`：`null` 挂到当前链末端（普通发言），显式给 id
  就是在该消息下开新分支；一次追加的若干条消息自动串成一条链，回复里带新节点 id。
  `assistant + 它的工具结果` 仍是一次原子提交，所以一步落库不会断开。
- `session.branch.activate`：只把父节点的 `current` 指过去，不动任何消息；
  `session.node.delete`：删掉该节点及其全部后代，父节点回落到剩下的最新分支。
- `session.prompt` 带 `parent` / `regenerate`：`regenerate` 表示**只生成回复**、
  不追加用户消息。于是三种界面动作落到同一套语义上：

| 界面动作 | 协议 | 结果 |
| --- | --- | --- |
| 发言 | `text` 不带 `parent` | 追加用户消息并生成回复，链继续变长 |
| 重试 | `regenerate=true` + `parent` | 同一父节点下多一条回复分支，旧的保留 |
| 编辑 | `text` + `parent=原消息父节点` | 新用户消息成为兄弟分支，随后生成新回复 |

每一轮送给模型的历史是**「根到挂载点的路径」**，不是整棵树：所以在旧分支上继续
追问，只会带上那条链的上下文，兄弟分支的内容不会串味。挂载点已过期（被删）时
立即失败，不会写半条消息。

存储侧：`messages` 多了 `parent_id` / `current_id`，`sessions` 多了
`root_current_id`。老库（schema 3）在启动时自动补列，并把线性历史折成一条单链
（前一条是父、后一条是选中分支），顺序与升级前完全一致。

前端：每条消息底部有脚注——父节点下有多个分支时显示 `‹ 2/3 ›`，另有「编辑」
（内联文本框，`Ctrl/⌘+Enter` 保存）、「重试」、「删除分支」。分支操作共用一处
事件委托，切换分支后由接口返回的快照整体重渲染。

## 多模态附件

WebUI 支持**粘贴 / 拖入 / 选择**图片（PNG、JPEG、GIF、WebP），随消息一起送给模型：

- 消息 JSON 里存的是内容寻址伪 URL `blue+attachment://<sha256>`，真实字节进会话库的
  `attachments` 表（主键即哈希）→ **同一张图只存一份**；`message_attachments` 记录
  消息与附件的映射，删会话时随消息级联消失，附件本身由 GC 按「无引用 + 超宽限期」回收；
- agent 发请求前把伪 URL 换成 `data:` URL（DeepSeek 只接受 data/http(s)），换不到就
  **降级成文本占位**，绝不把伪 URL 递给模型；
- WebUI 通过 `GET /api/attachments/<hash>` 取图，内容寻址因而可 `immutable` 长缓存；
- 预算：单张 ≤2 MiB（前端先降采样到长边 1568）、每轮 ≤4 张，超限报错而不是静默丢图；
- **工具也能返回图片**：工具把 `blue.runtime.tool_attachment(...)` 放进返回对象的
  `attachments` 字段即可，正文里不会出现 base64；MCP 桥的 `image` 块同样走这条路。
- `browser` 工具即是示例：`browser_screenshot(url, width, height)` 用无头 Firefox 抓图，
  经 Pillow 缩到长边 ≤1568、必要时转 JPEG（会话单附件上限 2 MiB），agent 直接「看见」页面。

细节（表结构、迁移、失败语义、界面链路）见 [`docs/multimodal.md`](docs/multimodal.md)。

## 事件协议

事件契约按域拥有，放在两个轻量契约包里：

- `contracts/tool` → `blue-contracts-tool`：`tool.*` / `catalog.*`
- `contracts/session` → `blue-contracts-session`：`session.*`

每个包导出 `EVENTS`（`tuple[type[EventDeclaration], ...]`），并在
`contract.infd` 中声明元数据；服务通过 `BusRuntime(events=...)` 只注册自己需要
的事件，不再维护全局 `ALL_EVENTS`。
契约包只依赖 `pydantic` / `infinity_bus`，不依赖 `blue` 或任何服务实现。

固定事件名：`tool.register` / `tool.heartbeat` / `tool.unregister` /
`catalog.changed` / `tool.call` / `tool.result` / `catalog.query` /
`catalog.reply` / `session.prompt` / `session.reply` / `session.stream` /
`session.cancel` / `session.cancel.reply` /
`session.get` / `session.get.reply` / `session.append` /
`session.append.reply` / `session.branch.activate` /
`session.branch.activate.reply` / `session.node.delete` /
`session.node.delete.reply` / `session.update` / `session.update.reply` /
`session.reset` / `session.reset.reply` / `session.state` /
`session.state.reply` / `session.attachment.get` / `session.attachment.reply` /
`session.rename` / `session.rename.reply` / `session.outbox`（广播）/
`session.outbox.enqueue` / `.list` / `.remove` / `.update` / `.move` /
`.send_now` / `.retry` / `.clear` / `.claim` / `.complete` / `.fail`（各自带
`.reply`）。

多目标/多实例靠载荷字段区分：`tool.call.tool`、`worker_id`、
`session_id + request_id`。多部署隔离用 RabbitMQ vhost，不要编码进事件名。

设计细节与迁移阶段见 [`docs/event-contracts.md`](docs/event-contracts.md)。
