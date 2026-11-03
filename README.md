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
       ├─ Reconciler          ← desired vs actual
       ├─ ProcessRunner       ← 启动 / 停止 / 重启 / 退避 / 日志
       └─ .blue/              ← state.json + logs + lock

       ├─ services/bus        → 管理 RabbitMQ + 可选 AMQP TCP 代理
       ├─ services/session    → 会话持久化（aiosqlite + WAL）
       ├─ services/agent      → 模型推理 + 工具目录
       ├─ services/webui      → FastAPI + 单页 Chat UI
       ├─ services/*          → 一个工具一个进程
       └─ services/chat|inspect → 手工运行的 CLI 客户端
```

一次对话的完整链路：

1. WebUI 通过 WebSocket / REST 发布 `session.prompt`；
2. agent 通过 `session.get` 向 session 服务取出/创建会话；
3. session 服务返回历史消息，agent 把工具目录交给模型；
4. 模型返回 `tool_calls`，agent 发布 `tool.call`；
5. 对应工具进程执行后回发 `tool.result`；
6. agent 把结果回灌模型，循环直到最终答复或 `max_steps`；
7. agent 通过 `session.append` 持久化新消息并发布 `session.reply`。

Agent 在模型流式输出期间同时发布 `session.stream`，携思考增量、文本增量、
工具调用与工具结果；WebUI 通过 WebSocket 即时转发给浏览器。

工具进程启动时发布 `tool.register`，周期补发注册并发布 `tool.heartbeat`，
退出时发布 `tool.unregister`；agent 按 TTL 回收失联工具。

## 目录结构

```text
src/blue/                     # 控制面 + 共享 SDK（随包安装）
├── protocol.py               # 固定事件名与负载模型
├── transport.py              # RabbitMQ fanout / 进程内 fanout
├── lifecycle.py              # serve_forever
├── worker.py                 # ToolWorker（工具服务使用）
├── runtime.py                # 服务侧运行时：读配置、连总线、run_tool
├── config.py                 # blue.infd 入口与文档导出
├── errors.py                 # BlueError / ConfigError / ...
├── control/
│   ├── model.py              # ServiceMeta / DesiredState / ActualState
│   ├── desired.py            # 编译 blue.infd + 加载 service.infd
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
├── session/                  # SQLite 会话存储（WAL）
├── agent/                    # 模型推理 + 工具目录
├── webui/                    # FastAPI 后端 + 静态单页应用
├── clock/ echo/ text_stats/
├── read_file/ write_file/ edit_file/ shell/
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

## 配置模型

### `blue.infd`：系统期望状态

顶层键就是服务名，也是 `services/<name>/` 目录名；`supervisor` 是唯一保留的
非服务顶层键。每个服务的值是 `services/<name>/service.infd` 中配置模板的一次
实例化。

```infd
#env: DEEPSEEK_API_KEY "sk-xxx"
!env import DEEPSEEK_API_KEY as ds_key

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

bus = BusService(mode = "managed", vhost = "blue")
session = SessionService()
agent = AgentService(api_key = $ds_key, max_steps = 8)
webui = WebUIService()
echo = EchoService()
```

- 键存在即启用；删除键即停止并从状态中清理。
- `!from` 只导入模板，不导入数据；`service.infd` 里的 `service` 数据块不会污染
  核心配置。
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
  "api_key_env": "DEEPSEEK_API_KEY",
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
uv sync --all-packages          # 安装根包与全部服务
set -a; . ./.env; set +a        # DEEPSEEK_API_KEY 等
uv run blue config-check        # 编译校验

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
- **重启**：`always` / `on-failure` / `never`；指数退避 + 抖动；稳定运行
  `stable_after` 秒后重置失败计数。
- **就绪**：`process`（存活）、`tcp`（连接 host:port）、`none`；`bus` 的 TCP
  目标自动从 `url` 或 `proxy_listen` 推导。
- **停机**：SIGTERM 进程树 → 等待 `stop_timeout` → SIGKILL；整体受
  `supervisor.shutdown_timeout` 预算约束。
- **孤儿清理**：重启 supervisor 时，按 `BLUE_INSTANCE_ID` 核对
  `/proc/<pid>/environ` 后清理旧进程；RabbitMQ 子进程带 `PR_SET_PDEATHSIG`。
- **单实例**：`.blue/supervisor.lock` 防止重复启动 supervisor。
- **版本与源码重载**：每个服务的 `package_version` 取自
  `services/<name>/pyproject.toml` 的 `[project].version`；`source_hash` 是
  「版本声明 + `src/**/*.py` 内容」的 SHA-256 指纹。两者都写进 `state.json`，
  版本在 `blue status` / `config-check` 中可见。调谐循环每轮用 `stat` 签名
  （路径 + 大小 + mtime）做低成本比对，只有疑似变化时才读文件内容算指纹；
  指纹与期望状态不一致就重新编译 `blue.infd` 并重启对应服务——**即使配置和
  版本号都没变，只改了源码也会重新加载**。指纹参与 `generation` 计算，
  因此 `blue apply --wait` 也能等到刷新完成。
  （`uv run` 在源码变化时会自动重建并重装该 workspace 包，所以重启即生效。）

## 内置服务

| 服务 | 说明 |
| --- | --- |
| `bus` | 管理 RabbitMQ、vhost/用户/权限配置，可选 AMQP TCP 代理 |
| `session` | SQLite（WAL）会话持久化，独立进程、支持重启后恢复 |
| `agent` | 模型推理 + 工具注册中心，本身不保存会话正文 |
| `webui` | FastAPI + 静态单页 Chat UI，WebSocket 实时进度，托管在 supervisor 中 |
| `clock` / `echo` / `text_stats` | 基础工具 |
| `read_file` / `write_file` / `edit_file` | 文件读写与精确替换 |
| `shell` | 异步 shell 命令，超时进程组强杀、输出截断；默认工作目录为项目根 |
| `chat` / `inspect` | 手工 UI 客户端，不纳入 supervisor 调谐 |

> **安全提示**：`read_file` / `write_file` / `edit_file` / `shell` 以工具进程
> 权限运行，没有额外沙箱。只在可信工作目录中启用。

## 新增服务

1. 创建 `services/<name>/`，加入 `pyproject.toml`（workspace 成员，依赖 `blue`）；
2. 写 `service.infd`：`~ServiceTemplate` 配置模板 + `service` 元数据；
3. 在 `src/blue_service_<name>/` 实现入口；工具服务直接调用
   `blue.runtime.run_tool`；
4. 在 `blue.infd` 中 `!from` 导入模板并实例化 `<name> = ...`；
5. 运行 `uv sync --all-packages`、`uv run blue config-check`。

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

## 事件协议

事件名是固定常量：`tool.register` / `tool.heartbeat` / `tool.unregister` /
`catalog.changed` / `tool.call` / `tool.result` / `catalog.query` /
`catalog.reply` / `session.prompt` / `session.reply` / `session.stream` /
`session.get` / `session.get.reply` / `session.append` /
`session.append.reply` / `session.update` / `session.update.reply` /
`session.reset` / `session.reset.reply` / `session.state` /
`session.state.reply`。

多目标/多实例靠载荷字段区分：`tool.call.tool`、`worker_id`、
`session_id + request_id`。多部署隔离用 RabbitMQ vhost，不要编码进事件名。
