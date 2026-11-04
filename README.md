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
  "prune_interval": 60.0,
  "busy_timeout_ms": 5000
}
```

- `database_path` 为相对路径时解析到 `BLUE_STATE_DIR`，默认
  `.blue/sessions.db`；
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

## 命令行

```bash
uv run blue up                  # 前台运行 supervisor
uv run blue status [--json]     # 查看实际状态
uv run blue logs echo -f        # 跟踪日志
uv run blue apply --wait        # SIGHUP 重载配置并等待生效
uv run blue down                # 优雅停止 supervisor 与全部服务
uv run blue config-check        # 编译校验 + 打印期望状态摘要
uv run blue config-check --write# 只生成运行时配置，不启动
uv run blue render [--full]     # 打印期望状态（默认脱敏）
uv run blue build               # uv sync --all-packages
uv run blue doctor              # 检查 uv / RabbitMQ / 状态目录
```

全局参数：`--config <path>`、`--verbose`。

## 快速开始

```bash
uv sync --all-packages               # 安装根包与全部服务
cp secrets.example.toml secrets.toml # 填入密钥
chmod 600 secrets.toml
uv run blue config-check             # 编译校验

# 终端 1：控制面（前台；生产交给 systemd / Docker）
uv run blue up

# 终端 2：观察与操作
uv run blue status
uv run blue logs agent -f

# 浏览器打开 WebUI
#   http://127.0.0.1:8787

BLUE_BUS_URL=amqp://guest:guest@127.0.0.1:5672/blue \
  uv run --package blue-service-inspect blue-service-inspect tools

# 交互式 CLI（手工客户端，不纳入 supervisor）
BLUE_BUS_URL=amqp://guest:guest@127.0.0.1:5672/blue \
  uv run --package blue-service-chat blue-service-chat --interactive
```

`bus` 默认 `managed` 模式：supervisor 会启动本机 `rabbitmq-server`，创建
`blue` vhost、配置用户与权限；退出时一并停止。若使用外部 RabbitMQ：

```infd
bus = BusService(
  mode = "external",
  url = "amqp://user:pass@broker:5672/blue",
  configure = false,
)
```

## 服务状态与生命周期

```text
Pending → Starting → Running → Ready
                    ↘ BackOff → Starting
Ready / Running → Stopping → Stopped
任意状态 → Failed（restart=never 或策略不再重启）
连续失败 → CrashLoopBackOff（仍按最大退避重试）
```

- **依赖**：`depends_on` 形成 DAG；依赖 Ready 后才启动下游，停止时逆序。
- **契约变更**：`blue apply` 会比较契约包指纹；变化时重启所有
  `depends_contracts` 命中该契约的服务。
- **重启**：`always` / `on-failure` / `never`；指数退避 + 抖动；稳定运行
  `stable_after` 秒后重置失败计数。
- **就绪**：`process`（存活）、`tcp`（连接 host:port）、`none`；`bus` 的 TCP
  目标自动从 `url` 或 `proxy_listen` 推导。
- **停机**：SIGTERM 进程树 → 等待 `stop_timeout` → SIGKILL；整体受
  `supervisor.shutdown_timeout` 预算约束。
- **孤儿清理**：重启 supervisor 时，按 `BLUE_INSTANCE_ID` 核对
  `/proc/<pid>/environ` 后清理旧进程；RabbitMQ 子进程带 `PR_SET_PDEATHSIG`。
- **单实例**：`.blue/supervisor.lock` 防止重复启动 supervisor。
- **版本与源码扫描**：每个服务的 `package_version` 取自
  `services/<name>/pyproject.toml` 的 `[project].version`；`source_hash` 是
  「版本声明 + `src/**/*.py` 内容」的 SHA-256 指纹。两者都写进 `state.json`，
  版本在 `blue status` / `config-check` 中可见。源码/版本扫描只在显式
  `blue apply`（或 `SIGHUP`/`reload()`）时执行：重新编译 `blue.infd`、计算
  指纹并重启发生变化的服务。supervisor 不会持续 watch 源码，也不会因为保存
  文件自动重启服务。
  （`uv run` 在源码变化时会自动重建并重装该 workspace 包，所以 apply 重启即生效。）

## 内置服务

| 服务 | 说明 |
| --- | --- |
| `bus` | 管理 RabbitMQ、vhost/用户/权限配置，可选 AMQP TCP 代理 |
| `registry` | 工具运行时注册中心：租约、心跳、目录版本与 `catalog.changed` 广播 |
| `session` | SQLite（WAL）会话持久化 + 内容寻址附件表（去重、级联、GC），独立进程、支持重启后恢复 |
| `agent` | 模型推理 + 工具目录缓存，本身不保存会话正文；加载历史时自愈悬空 `tool_calls`，未知工具立即失败 |
| `webui` | FastAPI + 静态单页 Chat UI，WebSocket 实时进度，托管在 supervisor 中 |
| `clock` / `echo` / `text_stats` | 基础工具 |
| `read_file` / `write_file` / `edit_file` | 文件读写与精确替换；相对路径默认锚定项目根 |
| `shell` | 异步 shell 命令，超时进程组强杀、输出截断；默认工作目录为项目根 |
| `weather` | 天气查询（wttr.in，免密钥）：实时实况 + 最多 3 天预报，天气码本地译中文 |
| `browser` | 无头 Firefox 截图：把网页截成图片**作为附件**回到 agent（Pillow 归一化到会话体积上限内） |
| `browser_ops` | 可交互浏览器（WebDriver/geckodriver）：等渲染、点击、输入、滚动、执行 JS，再截图回传附件 |
| `view_image` | 查看本地图片：把磁盘上的图（截图/图表/下载图）**作为附件**交给模型；相对路径锚定项目根 |
| `mcp_bridge` | MCP 客户端桥：把外部 MCP server（stdio）的工具接入工具目录；工具集声明式、schema 自动降级并留痕 |
| `mcp_server` | MCP 服务端桥：把 blue 工具目录暴露给外部 MCP 客户端（stdio，手工运行，白名单 + 危险工具门禁） |
| `chat` / `inspect` | 手工 UI 客户端，不纳入 supervisor 调谐 |

> **安全提示**：`read_file` / `write_file` / `edit_file` / `shell` 以工具进程
> 权限运行，没有额外沙箱。只在可信工作目录中启用。
>
> **MCP 提示**：`mcp_bridge` 会以工具进程权限启动外部命令（`npx` / `uvx` /
> 任意 `command`）。MCP server 就是一段任意代码，只接入可信来源。

## MCP 桥接

`mcp_bridge` 把外部 **MCP server**（stdio 传输）的工具接进工具目录，agent 用它
们和用本地工具没有区别。

```infd
!from p"services/mcp_bridge/service.infd" import ServiceTemplate as McpBridgeService

mcp_bridge = McpBridgeService(
  tools = ["fs__read_file", "git__status"],
  servers = [
    {name = "fs", command = "npx", args = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
    {name = "git", command = "uvx", args = ["mcp-server-git"]},
  ],
)
```

约定：

- **工具集是声明式的**。只有 `tools` 里列出的 `<server>__<tool>` 会注册；运行期
  不跟随 MCP 的 `tools/list_changed`，只有 `blue apply` 才会对齐。
- **fail loud**：声明了但 server 未提供的工具会让桥启动失败（`blue status` 可见），
  而不是静默少一个；server 提供了但没声明的工具只 warning。
- **调用必应答**：server 掉线、调用超时（`call_timeout`，默认 60s）、JSON-RPC error
  都会立刻回 `tool.result(success=false)`，不会挂到 agent 的 `tool_timeout`。
- **参数 schema 自动降级**：MCP 的完整 JSON Schema 超出工具参数能表达的部分
  （`$ref` 的 `definitions` 写法、`allOf`、`const`、`format`、无 `items` 的数组…）
  会被就地降级，并在工具描述末尾追加 `[blue] 参数 schema 已降级：…`，同时逐条告警。
- **零新增依赖**：桥自带 stdio JSON-RPC 客户端（只实现 `initialize` /
  `tools/list` / `tools/call`），不引入官方 `mcp` SDK 的传递依赖链。

未接入的能力：MCP 的 `resources/*`、`prompts/*`、`notifications/cancelled`、
`progress` 与 streamable HTTP 传输。设计与取舍见
[`docs/mcp-bridge.md`](docs/mcp-bridge.md)。

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
`session.append.reply` / `session.update` / `session.update.reply` /
`session.reset` / `session.reset.reply` / `session.state` /
`session.state.reply` / `session.attachment.get` / `session.attachment.reply`。

多目标/多实例靠载荷字段区分：`tool.call.tool`、`worker_id`、
`session_id + request_id`。多部署隔离用 RabbitMQ vhost，不要编码进事件名。

设计细节与迁移阶段见 [`docs/event-contracts.md`](docs/event-contracts.md)。
