# blue

[![Test](https://github.com/yinbailiang/blue/actions/workflows/test.yml/badge.svg)](https://github.com/yinbailiang/blue/actions/workflows/test.yml)

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

## 开发环境

需要 **Python 3.14** 与 **RabbitMQ**，两条路任选：

```bash
# 方式一：Nix（flake 已固定 nixpkgs 与工具版本；Linux / macOS）
nix develop
uv sync --all-packages

# 方式二：本机已有 Python 3.14 + RabbitMQ
uv sync --all-packages
```

首次运行前复制密钥模板并填入自己的值（`secrets.toml` 已被 `.gitignore`）：

```bash
cp secrets.example.toml secrets.toml
```

devShell 里已带 `uv`、`python3.14`、`rabbitmq-server`，进入后会打印启动与管理台命令。
服务默认连 `amqp://guest:guest@127.0.0.1:5672/blue`，可用 `BLUE_BUS_URL` 覆盖。
`flake.lock` 已提交；升级工具链时改 `flake.nix` 里的 nixpkgs URL 再 `nix flake lock`，
并确认 Python 3.14 仍在。

## 持续集成

质量门与本地完全一致（`.agents/SKILLS/setup-cicd.md`）：

```bash
uv sync --all-packages
uv run pre-commit install          # 一次性
uv run pre-commit run --all-files  # 与 CI 同一个门
```

`.github/workflows/test.yml`：

| 作业 | 内容 |
| --- | --- |
| `pre-commit` | ruff lint/format、pyright strict、interrogate、pytest + 覆盖率 |
| `test` | `pytest`，Linux 与 macOS 双平台（无 Windows：`fcntl.flock`） |
| `integration` | 起一个 RabbitMQ，真跑跨进程 fanout 往返 |

`.github/workflows/publish.yml`：推送 `v*` 标签 → 同一套质量门 → `uv build --all-packages`
→ GitHub Release。PyPI 发布默认关闭，需先在仓库变量里设 `PYPI_ENABLED=true`
（并完成 Trusted Publisher 注册与 workspace 包发布）。

覆盖率门槛当前是 **83%**，家族标准是 90%——先按“只升不降”的棘轮落下，
补上 `health` / `logs` / `lifecycle` / `journal` 的用例后再抬高。

需要 broker 的用例（`services/agent/tests/test_integration_rabbit.py`）在不可达时
自动跳过，所以本地不启 RabbitMQ 也能跑全量测试。

## 目录结构

```text
contracts/                    # 域契约包（不依赖控制面/服务实现）
├── tool/                     # blue-contracts-tool：tool.* / catalog.*
│   └── contract.infd         # 契约元数据
└── session/                  # blue-contracts-session：session.*
    └── contract.infd

src/blue/                     # 控制面 + 共享 SDK（不依赖任何契约包）
├── transport.py              # RabbitMQ fanout / 进程内 fanout
├── lifecycle.py              # serve_forever
├── runtime.py                # 服务侧运行时：读配置、按 depends_contracts 扫事件、连总线
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

sdk/tool/                     # blue-tool-sdk：工具域运行时（只被工具服务依赖）
├── sdk.infd                  # SDK 元数据（name / package / module）
├── worker.py                 # ToolWorker / ToolRegistrar
├── tool_call.py              # tool.call 处理器
├── tool_context.py           # ContextVar 调用上下文 / call_service
└── runtime.py                # run_tool

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
  shutdown_timeout: <float, positive> = 60.0   # 整场停机墙钟，见「优雅停机」
  restart_backoff: <float, positive> = 0.5
  restart_max_attempts: <int, range(1, 100)> = 10
  registry_ttl: <float, positive> = 15.0
  idempotency: bool = true                      # 事件去重总开关，见「事件去重」
  idempotency_retention: <float, positive> = 86400.0
  idempotency_sweep_interval: <float, positive> = 600.0
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
  depends_contracts = ["tool"]  # 依赖的事件契约名（自动注册其 EVENTS）
  depends_sdk = ["tool"]        # 依赖的 SDK 运行时包名（sdk/<name>/sdk.infd）
  restart = "always"            # always / on-failure / never
  stop_timeout = 30.0           # 本服务的优雅停止预算（supervisor 到点 SIGKILL）
  startup_timeout = 30.0
  readiness = "process"         # process / tcp / none
  ready_settle = 0.3            # process 探针的观察期（秒）：先等这么久再看进程还活着没
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
  `src/**` 全部文件），并把契约纳入 generation。
- 服务用 `depends_contracts` 声明自己依赖的契约；supervisor 把契约名展开成
  `{"tool": "blue_contracts_tool"}` 写进运行时配置的 `infra.contracts`。
- 服务进程调 `build_runtime()` 时自动导入这些模块的 `EVENTS`，服务代码里不再
  手写事件列表；`run_tool()` 同理。
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
  "compact_boundary_step": 50,
  "compact_attachment_chars": 2400,
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
  （默认 300 条）原样发送——模型正在用的工具结果不能被截断。预算极低时这条
  保护会让位于「不超窗口」，但省略说明里会写明「其中最近 N 条也一并省略」，
  不让模型误以为近几轮还在。
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

  数值怎么定：`agent` 每步会打印实测的「字符/token」比例，同时打印**工具 schema
  字符数**与**附件个数**。真实会话里工具结果是 JSON，实测 **1.6 字符/token**
  （不是纯文本的 2.6），所以 100 万 token 只相当于 160 万字符。默认 120 万字符
  ≈ 74 万 token，给 1M 窗口留出工具 schema 与回复的余量。换个模型/换个内容构成时，
  看日志里的比例重算一次即可。

  > **工具 schema 不在预算内**（当前 38 个工具、实测 **20,911 字符** ≈ 8 千 token，
  > 每步都随请求发送）：提示很小时它会主导「字符/token」比值（实测出现过 **0.4**），
  > 别拿那个比例反推预算。统计口径必须按客户端的序列化方式（`exclude_none`）——
  > 全量 dump 会把每个参数的可选字段写成 `"anyOf": null, "enum": null, …`，
  > 虚高 **2.4 倍**（50,600 vs 20,911）。附件则相反——按
  > `compact_attachment_chars` 计入，所以图片多的会话预算才是准的。

  实测一条 1742 条消息的会话：原始 2967 KB，预算 200 万 → 1009 KB（不丢任何消息）；
  预算 60 万 → 584 KB / 丢 1136 条；预算 20 万 → 195 KB / 丢 1673 条。

  投影分界由 `compact_boundary_step`（默认 50 条，`0` = 不量化）控制：分界如果
  每来一条消息就挪一格，等于每次调用都把第 `len-keep` 条改写一遍，上游的 prompt
  前缀缓存会从那里整段失效。按步长取整后，整个步长区间内分界不动，代价是最少
  多保护 `步长-1` 条。量化只作用于已经越过步长的区间——短会话（`len-keep < 步长`）
  不受影响，否则分界会被整成 0、等于一条都不压。
- `compact_attachment_chars`（默认 `2400`，`0` = 不计入）：每个附件内容块折算的
  **字符当量**。消息里的图片只是 `blue+attachment://<hash>` 伪 URL，真实字节要到
  发请求时才 materialize 成 `data:` URL——按文本估算等于把图片当零成本，预算对它
  完全失去约束力。2400 ≈ 1500 视觉 token × 1.6 字符/token 的实测比例。
- `system_prompt` 只在**建会话时**写入根节点：已存在的会话沿用建会话时的提示，
  改配置不会回写（历史消息都是在那条提示下产生的）。配置与库内不一致时 session
  服务会告警一次（每个会话一次），需要新提示请新建会话。
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
内部字段，所以上下文走 `blue_tool_sdk`（`ContextVar`）：`ToolWorker` 在调用前
用 `ToolCallRequest.session_id` 绑定，调用后复位。工具里用 `current_context()`
取；需要反向调用别的服务时用 `call_service(...)`，并在 `service.infd` 的
`depends_contracts` 里声明对应契约——事件由运行时自动注册，不需要手传。

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

## 思考内容（reasoning）会进上下文——注意前提

DeepSeek 官方文档「思考模式」写的是：`reasoning_content` 是否拼进上下文，**取决于
请求是否携带 `tools`**：

| 请求 | 历史 reasoning_content |
|---|---|
| **不带 `tools`** | 不拼接；传了也被忽略 |
| **带 `tools`** | **应回传，且会被拼接进上下文** |

blue 是 agent，每个请求都带工具目录（当前 36 个），所以走的是第二行：历史轮的
思考内容会一直留在上下文里。实测这条长会话：**prompt 709,570 token 里 235,451
token（33%）是历史 reasoning**（用官方分词器算 703,471，与账单差 0.9%）。

官方给的调节旋钮是 `reasoning_effort`（默认 `high`，思考最长）：

```infd
reasoning_effort = "low"   # low / medium / high；null = 用服务端默认（high）
```

调低会让每轮的思考更短，从而**从源头**压住上下文增长（历史 reasoning 也随之变小）。
不要用"自己截断历史 reasoning"来省 token——文档明确说带 tools 时应回传，砍掉属于
偏离官方行为，可能影响工具调用的连贯性。

## 调谐器是独立进程，能重启自己

`blue up` 默认**后台拉起独立的调谐器进程**（`start_new_session`，日志落
`.blue/logs/supervisor.log`），命令立刻返回：

```
$ blue up
supervisor 已启动（pid=12345）
  日志：.blue/logs
  改完配置用 blue apply 生效；停止用 blue down
```

`--foreground` 保留旧行为（容器/调试用）。

**为什么必须独立**：`apply` 要用上新的 SDK 代码，而一个已经在跑的 Python 进程
换不掉自己的实现。现在 `apply` 会按情况选信号：

| 变化 | 信号 | 调谐器做什么 |
|---|---|---|
| 服务配置 / 服务源码 / 契约 | `SIGHUP` | 重载配置，**只重启受影响的服务** |
| **blue SDK / 蓝本版本 / 配置格式** | `SIGUSR1` | 优雅停掉全部服务 → `execv` 自己（PID 不变、锁不丢）→ 按新代码全部拉起 |

**不会无限重启**：自重启时锁文件里写的是**新** SDK 指纹，下一次 `apply` 比对相同
→ 走 SIGHUP，不再重启。也就是"一次 apply 只重启一次"。

> 迁移提示：从旧版本升上来时，**运行中的调谐器还是旧代码**（没有 SIGUSR1 处理），
> 所以升级后需要 `blue down && blue up` 一次；此后 `apply` 即可自理，不用再手动
> 停起。

### 自重启的已知限制与兜底

`execv` 是"进程用自己替换自己"：PID 不变、锁不丢、服务随后重新拉起。代价是
**execv 之后没有外部看护**——如果新镜像启动就崩（语法错误、坏配置、缺依赖），
整棵树会停在原地，只能人工 `blue down && blue up` 救回。

现有的三道缓冲：

1. `apply` 发出信号前会先 `load_desired()` 编译校验，坏配置直接报错、不会触发 execv；
2. 调谐器收到信号后**再校验一次**配置能否编译，不行就退化成"重载"（保留当前进程）；
3. 新镜像启动失败会立刻退出，原因写在 `.blue/logs/supervisor.log`。

生产环境建议再包一层进程管理器（execv 失败也能被拉起）：

```ini
# /etc/systemd/system/blue.service
[Unit]
Description=blue supervisor
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/blue
# 用 --foreground：交给 systemd 管生命周期，而不是自己 detach
ExecStart=/path/to/blue/.venv/bin/python -m blue up --foreground
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
```

判断一次 apply 走的是哪条路，看 `blue status` 的 supervisor PID：

| PID | 含义 |
|---|---|
| **不变** | 走了 `execv` 自重启（只换代码，不换进程） |
| **变了** | 走了人工 `down`/`up`，或进程管理器重新拉起 |

## 变更追踪：契约、服务包、核心、运行时包四件都算

`blue plan` / 调谐器判断「要不要重启」时比对四样东西：

| 维度 | 覆盖范围 | 变化时的后果 |
|---|---|---|
| **契约**（`contracts/*`） | 事件、载荷、代理 | 只重启 `depends_contracts` 里声明了它的服务 |
| **服务包**（`services/<name>/`） | pyproject + `src/**`（全部文件，含 `web/**` 静态资源；排除 `__pycache__`/`*.pyc` 等缓存） | 只重启该服务 |
| **blue 核心**（`src/blue/**` + 根 pyproject） | runtime、transport、控制面 | **重启全部服务** |
| **SDK 运行时包**（`sdk/*`） | 如 `tool` → `blue-tool-sdk` 的 worker / tool_context | 只重启 `depends_sdk` 里声明了它的服务 |

后两行是变更追踪的关键：`ToolWorker`/`BusRuntime` 这些运行时宿主不在服务目录里，
而服务包的 `source_hash` 只覆盖自己的目录——不单独跟踪的话，运行时改了调谐器
**完全看不见**，进程会一直跑旧代码。现在核心与 `sdk/*` 的指纹分别进期望状态、
写进 `.blue/state.json`，`plan` 会显示「SDK 变化」或「依赖运行时包变化」。

判据（为什么 worker 不搬进契约包）：能放进"别的语言也能实现"的包 → 契约；
只有"我们的运行时"才懂 → `sdk/*` 运行时包。搬进契约会把 `BusRuntime`/
`ContextVar`/生命周期塞进协议包，还会让"改一行实现"变成全机重启。
现在 `blue` 核心**不依赖任何契约包**，工具域实现集中在 `blue-tool-sdk`，
工具服务用 `depends_sdk = ["tool"]` 声明它；改工具运行时只重启工具类服务。

## 世代（generation）：原子激活与回滚

`blue apply` 不再是"发信号 + 原地覆盖配置"，而是 NixOS 式的三阶段：

```text
build  → 计算每个服务的产物闭包指纹、构建缺失的产物（不碰运行中的系统）
flip   → 冻结 .blue/generations/<N>/（含配置）+ os.replace 原子翻转 current
verify → 等全部服务 Ready；超时/失败 → 自动翻回上一个 stable 世代
```

三层严格分离：**代码**（`.blue/artifacts`，内容寻址，可回滚）、**配置**
（`.blue/generations/<N>/services/`，随世代冻结）、**数据**（`sessions.db`、
`rabbitmq/`、用户文件，**永不回滚**）。

| 命令 | 作用 |
|---|---|
| `blue apply` | 三阶段全流程；内容没变则直接复用当前世代 |
| `blue apply --staged` | 只冻结世代、不切换（等价 `nixos-rebuild boot`） |
| `blue apply --expect <指纹>` | 断言 `current` 仍是 `plan` 里显示的那个（可只写前缀）；变了就拒绝，不承诺执行完落在哪个世代 |
| `blue apply --expect-desired <指纹>` | 断言要构建的内容仍是 `plan` 里显示的"期望"值；plan 之后改过源码就拒绝（两个一起用 = 起点和内容都没动） |
| `blue activate <N>` | 激活一个已存在的世代 |
| `blue rollback` | 回到上一个 stable 世代（整机） |
| `blue rollback --service agent` | 只把某个服务的产物+配置换回旧世代（生成新世代，历史保持线性） |
| `blue generations` | 列出全部世代、当前指针与状态（`*` 为当前） |
| `blue gc --keep N` | 回收老世代、无引用产物与 wheel 目录；`--dry-run` 先看要删什么，`--vacuum-dedupe` 顺手压去重库（与 `apply` 互斥，撞上就跳过） |

六条容易踩的规则：

1. **切换世代 ≠ 全量重启**。只有真正变了的服务会重启，判据是服务清单 / 配置 /
   源码 / 版本 / 依赖契约 / SDK 核心 / 依赖的 `sdk/*` 包，外加**产物指纹兜底**
   （`uv.lock`、解释器版本变了而上面七项都没变时，也必须重启，否则进程继续跑旧产物）。
   实测：只改 `weather` 的 `timeout`，18 个产物全部复用、只有 `weather` 重启，
   其余 17 个 PID 一个没动，5.2 s 完成。
2. **闸门之后还有冒烟**：每个服务在自己的 `service.infd` 里声明怎么检查自己
   （`smoke_tools` / `smoke_min_tools` / `smoke_command`），失败就回滚。控制面只执行、
   不含业务知识——session 的写读检查写在 session 包里，weather 只需一行
   `smoke_tools = ["get_weather"]`。实测：正常 13/13 通过；故意写错一个工具名 →
   自动回滚 + 退出码 1。
3. **控制面变化会自动等结果**：`src/blue/**` 变了会 execv，这种 apply 即使没带
   `--wait` 也会等验证（`supervisor.await_control_change`），免得"命令返回 0 但其实没起来"。
4. **控制面设置随世代冻结**。`activation_timeout`、`generation_auto_rollback`
   这些属于"这一代怎么跑"，存进 `generation.json`；所以 `apply --config other.infd`
   改这些值会生效（早期版本没存，实测就是这里翻的车：设 12s 却在跑 120s）。
5. **gc 至少保留两个 stable**。自动回滚的目标是"上一个 stable"，只留一个等于
   把机制刚需的东西回收掉。
6. **execv 自重启 ≠ 崩溃**。两者都表现为"`current` 指向的世代还没 stable"，
   区别在 PID：execv 保留 PID，崩溃后重新拉起的一定是新 PID。用状态快照里的
   `supervisor_pid` 判定，自重启要继续验证，不能误回滚。

详细设计（目录布局、产物闭包、状态机、崩溃兜底、已知边界）见
[`docs/generations.md`](docs/generations.md)。

## 注册中心是单实例（工具登记走请求/应答）

`tool.register` / `tool.heartbeat` / `tool.unregister` 是**请求/应答**，不是单向广播：

| 事件 | 应答 | 说明 |
|---|---|---|
| `tool.register` | `ToolRegisterResponse{changed, version}` | 目录变没变、当前版本号 |
| `tool.heartbeat` | `ToolHeartbeatResponse{registered}` | `false` = 租约已失效，调用方要重新登记 |
| `tool.unregister` | `ToolUnregisterResponse{removed, changed}` | 是否真的移除了 |

代价是**前提**：注册中心必须**单实例**——多实例时多个副本会各回一条，`request()`
只等第一条，语义就含糊了。以后要支持多实例，请设计专用协议（例如选主 + 确认数）
再迁移，不要直接把这条请求复制成 N 份。

配套的 worker 侧语义（`ToolRegistrar`）：

- **登记不阻塞启动**：`announce()` 把登记丢到后台任务里发，注册中心没起来也不影响
  服务 ready，失败由心跳循环重试；
- **心跳先续约、失效才补登记**：`registered=False` 时才重发 `tool.register`，
  少一半广播（注册中心重启后能自愈）；
- **下线用短超时**：退出路径不等太久（`WITHDRAW_TIMEOUT`），租约本来也会过期。

## 队列守卫（bus 自带兜底）

fanout 成员队列是 **durable + 非 auto-delete**（`restart` 策略要能补投积压），
所以进程被 kill 时没人解绑，队列会留在 broker 上**继续接收广播**——孤儿队列会
一直涨。bus 服务启动时下发一条策略，同时兜住两种最坏情况：

```
blue-fanout-guard:  ^infinity_bus\.fanout\.   (apply-to queues)
  {"expires": 1800000, "max-length-bytes": 1073741824, "overflow": "drop-head"}
```

| 键 | 兜住什么 | 配置项 |
|---|---|---|
| `expires` | **孤儿**：最后一个消费者断开后 N 分钟没有新消费者 → 队列自动删除 | `queue_expires_ms`（默认 1800000 = 30 分钟） |
| `max-length-bytes` | **卡死**：有消费者但不读 → 单队列封顶，超限丢最旧的 | `queue_max_bytes`（默认 1073741824 = 1 GiB） |
| `overflow: drop-head` | 显式固定为「丢最旧」，**不阻塞发布方**（`reject-publish` 会让一个堵住的消费者拖死整个总线） | 固定 |

两个配置填 `0` 都可以关掉；都为 0 时不下发策略（自建 broker 自管策略的场景）。

> 实测：给一条队列设 2 MB 上限后灌 5 MB 进去，队列只留在 2.06 MB —— 上限生效，
> 发布方没有被阻塞。

排查磁盘暴涨：

```bash
rabbitmqctl -n blue@<host> -p blue list_queues name consumers messages message_bytes
```

**0 消费者 + 大量消息**的 durable 队列就是血栓，`delete_queue` 即可回收。实测一次
清掉 172 个孤儿队列、释放 51.8 GB（增长从 55 MB/20s 降到 1 MB/20s）。

## 优雅停机：什么会丢、什么会重投

停机策略分两种，由**谁发起**决定，不是配置项：

| 场景 | 策略 | 队列后果 |
|---|---|---|
| 普通重启（`apply` / 崩溃拉起 / 自重启） | `restart` | 只停消费，绑定与积压保留 → 启动后**补投** |
| 真正下线（`blue down` / Ctrl-C） | `offline` | 解绑 + 排空 → 离线期不再入队，**不留积压** |

- **意图用文件传，不用信号**：supervisor 在停之前按服务写
  `state_dir/shutdown-intent/<service>.json`，写完才发 `SIGTERM`。一个服务一份
  ——共享一份时第一个停机的服务读完即删，其余全按 `restart` 留着积压。
- **残留的意图会被清掉**：supervisor 启动时清 `shutdown-intent/`，服务侧也只认
  `SHUTDOWN_INTENT_TTL`（300 秒）内的文件。上一次 down 被强杀留下的文件如果被当真，
  下一次普通重启就会解绑队列、丢掉本该补投的积压。
- **预算怎么分**：服务的 ``stop_timeout`` 是 supervisor 给它的优雅停止预算，服务据此
  把自己的排空预算（`stop_timeout - min(2, 一半)`）交给总线的 `ShutdownConfig`：

  | 总线超时 | 等的是什么 |
  |---|---|
  | `queue_timeout_*` | `queue.join()`：**事件全部派发完成**（= 每个事件的处理器任务都拿到执行权），只占零头 |
  | `tasks_timeout` | `_wait_all_tasks_done()`：**正在跑的处理器**收尾（占预算大头） |

  ack 的时机决定了这个方向：`task_done()`（= `basic_ack`）在「该事件所有处理器任务都
  获得执行权」之后就发（`_DispatchFence`），所以 `join()` 与处理器跑多久无关——只等
  「启动」；真正等收尾的是 `tasks_timeout`。**这条方向跟着库的 ack 语义走**（历史上
  翻过两次），改库语义时必须同步改 `shutdown_config_for()`，
  `tests/test_shutdown_budget.py` 里的方向断言就是哨兵。
- **派发放弃时不 ack**：停机时还有处理器从未获得执行权的事件，总线走
  `queue.task_abandoned()`——不 ack，持久队列在通道关闭时重投（真正的 at-least-once）。
  中间件同时收到 `on_dispatch_abandoned` 清掉派发跟踪；认领只写在处理器启动时，没起来
  的处理器本来就没有记录可撤回（见「事件去重」）。
- **失败不再触发重投**：ack 在「启动」时就发了，处理器之后再失败也不会重投——
  失败由 `TaskErrorEvent` 上报。代价是没有 nack/DLQ：需要重试就在业务侧做
  （`release` + 重发重试事件）。
- **按依赖分层倒序停，层内并发**：`dependency_levels()` 把服务按 `depends_on`
  算成层级，停机从高层往低层走，同一层之间没有依赖关系，一起停（`asyncio.gather`）。
  依赖别人的先停、被别人依赖的后停——bus 永远在最后一层，所以前面那些服务收尾时
  总还在，能正常 ack。

  | 配置 | 含义 | 本仓库的值 |
  |---|---|---|
  | `service.stop_timeout` | **单个服务**的优雅停止预算（到点 SIGKILL） | 30 秒 |
  | `supervisor.shutdown_timeout` | **整场停机**的墙钟预算 | 60 秒 |

  最坏时长因此是「**层数 × 单服务 30s**」而不是「服务数 × 30s」：本仓库 4 层
  （webui → agent 那层 → 12 个工具/会话 → bus），上界 120 秒；实际停机通常 2~3 秒。
- **告警只在真的被压缩时打**：层数 × 30s 超过 60s 是常态（服务退出远快于预算），
  所以开局只记 info（含波次与上界）；某一波真的只剩不足该层最慢服务的 `stop_timeout`
  时才 warning——那才是"预算花完了、这一波要打折停"。

## 事件去重：保证「执行一次」，不插手执行结果

at-least-once 下事件可能重复投递（崩溃重连 / `restart` 补投）。中间件按
`(消费方, 处理器, event.id)` 记账：**在保留期内**，一条事件最多被同一个处理器执行一次
（超过 `idempotency_retention` 的极端重投会重新执行——去重表已经不记得它了）。

- **认领写在处理器启动时**（`on_task_start`），`before_dispatch` 只查不写。于是
  「派发之后、启动之前」崩溃（supervisor 到点 SIGKILL、掉电）不会留下认领，broker
  重投后照常派发，事件不丢。反过来，认领若写在派发时，这个窗口里的事件会被自己的
  认领挡住：既不执行、也不再 ack，变成持久队列上的毒消息。
- **重复投递要 ack**：`before_dispatch` 把空列表交给核心层，由它确认消息。不确认的话
  持久队列上会留下永不 ack 的消息——每次重连重投一次，还占着 prefetch 配额。唯一例外
  是「原任务还在本进程排队等执行权」：这条重复保持未确认，等原任务启动（有认领）或
  放弃（无认领）之后，下一次投递自然会被判成重复。
- **去重库故障时放行**：查询失败当作「没执行过」，认领写失败只记日志。`before_dispatch`
  里的异常会被总线当成派发链失败直接 ack——去重是优化，不能把库的问题升级成丢事件。
- **失败不自动重试**：认领在启动时就写死了，重投回来会被挡掉。失败由 `TaskErrorEvent`
  上报；要重试得另配机制（`release` + 显式重发重试事件），目前**没有做**。
- **粒度是 per-handler**：`consumer = "{member_id}:{稳定键}"`，稳定键默认
  `模块.类名`，可用处理器的 `dedupe_key` 属性覆盖（同类多实例、或类名会重命名时）。
  不能用 `EventHandlerRegistry` 的 handler id —— 那是每次注册新生成的 uuid，
  重启后去重表全对不上。拼键用 `blue.idempotency.consumer_key()`。
- **停机放弃派发不需要撤回**：总线触发 `on_dispatch_abandoned`，中间件只清内存跟踪。
  没起来的处理器没有认领，已启动的处理器保留认领；重投后只补跑没起来的那些，已经跑过
  的不会再跑一遍（这是上游事件级 `claim` 拿不到、blue 本地补上的精度）。
- 存储：库的 `SqliteIdempotencyRecorder`（`state_dir/dedupe/<service>.sqlite`），
  跨重启有效——进程内集合做不到，重复恰恰发生在重启之后；
- 保留期：`supervisor.idempotency_retention`（默认 1 天）+ 每
  `idempotency_sweep_interval`（默认 600 秒）顺带清理一次。不清就是只增不减
  （实测单个服务一天 5 万行，webui/registry 一天几十万行）；
- **删行不缩文件**：SQLite 按行 DELETE 后空间留在 freelist 里，文件大小不回落。
  调小保留期只是停止增长，要真回收得 `blue gc --vacuum-dedupe`（`VACUUM` 需独占，
  服务开着库时会跳过，停机窗口跑更彻底）；
- 关掉：`supervisor.idempotency = false`（排查"某条事件没被处理"时的第一步）；
- 写放大：库开 WAL + `synchronous=NORMAL`，实测 13ms/事件 → 0.02ms。
  代价是掉电可能丢最后几个事务——最坏等于「该事件被重复执行一次」，也就是没有去重
  时的行为。

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
uv run pytest -q          # 默认并行 + 跳过 slow：约 15 秒
```

- `tests/`：控制面、配置、状态、进程管理、调谐、CLI；
- `services/*/tests/`：各服务单元测试；
- `services/webui/tests/`：FastAPI REST 与 WebSocket 协议测试；
- `tests/test_service_manifests.py`：服务目录、manifest、配置契约；
- RabbitMQ 集成测试在 broker 不可达时自动跳过，可用 `BLUE_RABBIT_URL` 覆盖；
  默认使用项目的 `blue` vhost（也可通过 `BLUE_BUS_URL` 指定）。

### 跑法：默认并行，重活按需

`pyproject.toml` 里 `addopts = "-n 8 -m 'not slow'"`，所以不带参数的 `uv run pytest`
就是**日常该用的那条命令**：896 个用例并行跑完约 15 秒（此前串行 116 秒）。

```bash
uv run pytest                      # 日常：并行 + 跳过 slow
uv run pytest -m ""                # 全量：把 slow 也算上（真起 firefox / geckodriver）
uv run pytest -m slow              # 只跑依赖真实外部环境的集成用例
uv run pytest services/agent       # 只跑改动涉及的模块，约 2 秒
uv run pytest -n 0 -k 某个用例     # 调试：-n 0 关掉并行，断点/print 才看得见
uv run pytest -n 2                 # 低并发的确定性模式（CI 与排查 flaky 用）
```

几条经验值：

| 并行度 | 耗时（899 个） | 说明 |
|---|---|---|
| `-n 0` | ~70s | 串行，仅用于调试 |
| `-n 2` | ~36s | **CI 用这个**：GitHub runner 只有 2–4 核，再多会过载 |
| `-n 8` | **~15.5s** | 本地默认。22 核机器上的甜点，连跑 6 次稳定 |
| `-n auto` | 12–69s 抖动 | **不要用**：22 worker 会互相抢，偶发超时 |

`slow` 标记只留给**依赖真实外部环境**的用例：`browser` / `browser_ops` 里真起
firefox、geckodriver 的那 6 条（并行时互相抢 GPU/进程资源，单个从 2.5s 涨到 11s，
是唯一拖住关键路径的瓶颈）。纯时序敏感的用例不再需要它，见下。

> **曾经 flaky 的两条回滚用例已根治**（`test_activation_timeout_rolls_back` 等）。
> 症状：`-n 8` 下约一半概率等不到 `rolled_back`，日志里连 ERROR 都没有。
> 根因是就绪探针与时序的配合缺陷，不是用例写错，也不在 `_reload_locked`：
> `readiness="process"` 的探针原先只观察固定 100ms，而一个「起来就崩」的进程在
> 高负载下能活过这 100ms（实测 20 个忙进程时 exit1 存活 104ms），于是被判 READY；
> 激活闸门若恰好在此刻看到「全绿」就 `mark_stable`，**坏世代被标成稳定、永不回滚**
> ——「激活失败自动回滚」这个核心保证静默失效。
>
> 修了两层：
> 1. 探针观察期改为可配（`service.infd` 的 `ready_settle`，默认 0.3s）；
> 2. 闸门要求全绿**连续保持一段宽限**（`activation_timeout` 的 0.4 倍、封顶 1s）
>    才承认激活通过，中间任何一次崩溃都会打断计时。这一层不依赖 `ready_settle`
>    配得对不对，是兜底。
>
> 回归用例 `test_activation_rejects_service_that_dies_right_after_ready` 是确定性的
> （故意把 `ready_settle` 配成 0.05 秒，让 fixture 的 `exit1-late` 骗过探针），
> 把 grace 置 0 它必挂。

### 测试里不该有的固定等待

- 工具下线注销租约走 `blue_tool_sdk.worker.WITHDRAW_TIMEOUT`（默认 1.5 秒）。测试里
  注册中心从不应答，每个用到 `ToolWorker` / `ToolRegistrar` 的用例都会白等满——
  根 `conftest.py` 的 `_fast_tool_withdraw` 已把它压到 0.05 秒（28 个用例、约 28 秒）。

- **新写用例**别再用 `await asyncio.sleep(1.0)` 这种固定等待：慢机器上不够、快机器上
  白等。要等某件事发生就用条件轮询（`tests/support.py` 有 `wait_until(pred)`）。

  > `services/terminal/tests/` 里还留着十来处固定 sleep，暂未改动：那些用例里有好几处
  > 断言的字符串**本身就出现在命令回显里**（如 `echo hello-terminal-test`），改成
  > `wait_until(lambda: needle in text)` 会在「命令回显」那一刻就返回，等于悄悄把断言
  > 从「输出已产生」弱化成「命令已回显」。要改得逐个区分回显与输出（例如数出现次数），
  > 收益只有约 2 秒，不值当在没测试覆盖的情况下动——留作后续单独处理。

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
