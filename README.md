# blue

基于 `infinity-agent` / `infinity-bus` / `infinity-data` 的**分布式 Agent 核心**。

- **核心（本体聚合）** —— `src/blue/`：会话中心 + 工具目录，一个进程、一个总线成员；
- **外置程序** —— `handlers/`：工具处理器与用户界面，各自独立进程；
- **模型与工具调用** → `infinity-agent`；**配置** → `infinity-data`；
  **装配与跨进程通信** → `infinity-bus` + RabbitMQ fanout。

> **边界原则**：核心只管自己。它不启动、不监督、不代理任何外围程序；外围程序
> 也独立启动、独立退出，双方只通过总线上的事件契约耦合。

## 架构

```text
                     RabbitMQ fanout（同一个逻辑总线）
      ┌───────────────────────────────┬───────────────────────────────┐
      │                               │                               │
┌───────────────────────┐   ┌───────────────────────┐   ┌───────────────────────┐
│   blue serve          │   │ handlers.tools.echo   │   │ handlers.ui.chat      │
│   本体聚合（核心）      │   │ 外置工具处理器（进程）   │   │ 外置用户界面（进程）     │
│  · 会话中心            │   │ 一个进程一个工具         │   │ · 发消息 / 看目录        │
│  · 工具目录 / 租约      │   │ 自我注册 + 心跳 + 执行   │   │ · 只做总线 RPC          │
└───────────────────────┘   └───────────────────────┘   └───────────────────────┘
```

一次对话的完整链路：

1. 界面发布 `session.prompt`；
2. 核心取出/创建会话，把工具目录（`catalog.changed` 缓存或 `catalog.query` 拉取）
   作为 `tools` 参数交给模型；
3. 模型返回 `tool_calls`，核心为每个调用发布 `tool.call`，按
   `session_id + request_id` 等待 `tool.result`；
4. 对应工具进程执行后回发 `tool.result`；
5. 核心把结果作为 `tool` 消息回灌模型，循环直到最终答复（或达到 `max_steps`），
   再发布 `session.reply`。

工具进程启动时发布 `tool.register` 自我注册，周期发布 `tool.heartbeat` 续约，
退出时发布 `tool.unregister`；核心按 TTL 回收失联工具。

## 目录结构

```text
src/blue/                 # 核心：本体聚合（随包安装）
├── aggregate.py      # AgentAggregate：会话中心 + 工具目录，一个运行实体
├── agent.py          # 会话中心：模型循环 + 总线工具调用
├── catalog.py        # 工具目录状态 + 注册中心服务 + Agent 侧缓存
├── session.py        # 中心化会话仓库（每会话独立锁 + 空闲回收）
├── protocol.py       # 事件主题与负载模型（按 namespace 生成事件声明）
├── transport.py      # BusRuntime：RabbitMQ fanout / 进程内 fanout
├── worker.py         # 工具进程宿主（供外置工具使用）
├── config.py         # .infd 编译 + 强类型校验 + 凭证解析
├── lifecycle.py      # serve_forever：核心与外置程序共用的生命周期
├── cli.py            # 核心命令行：serve / config-check
├── errors.py         # BlueError
└── subscribers/      # 核心内部事件订阅者，一个模块一个
    ├── registry.py   #   注册 / 续约 / 下线 / 目录查询
    ├── catalog.py    #   目录变更 → 缓存
    ├── session.py    #   消息 / 重置 / 会话状态
    └── tool_call.py  #   工具调用入参

handlers/                 # 外置程序（不随包安装，独立进程）
├── support.py        # 公共设施：读配置、建总线、跑生命周期
├── tools/            # 工具处理器：一个工具一个模块一个进程
│   ├── echo.py
│   ├── clock.py
│   └── text.py
└── ui/               # 用户界面
    ├── chat.py
    └── inspect.py
```

### 扩展约定

- **新增工具**：在 `handlers/tools/` 加一个模块，暴露 `TOOL` 与 `__main__`
  （参考 `echo.py`）；核心无需改动、无需重启。
- **新增界面**：在 `handlers/ui/` 加一个程序，只依赖 `blue` 包暴露的协议与传输。
- **核心不感知具体工具与界面**：核心只有主题契约与目录，不含任何具体实现。

## 配置

`blue.infd` 由 `infinity-data` 编译，字段约束（类型、范围、枚举）即校验规则：

```text
!env import DEEPSEEK_API_KEY as ds_key

agent {
  provider: in("deepseek", "openai") = "deepseek"
  model: str = "deepseek-v4-flash"
  api_key: str = $ds_key
  max_steps: <int, range(1, 32)> = 8
  tool_timeout: <float, positive> = 30.0
}
```

- `!env import` 显式声明要读取的环境变量；加载器只授权**配置里声明过**的变量，
  其余保持零信任。
- 密钥来源：配置内 `api_key`（如 `$ds_key`），或 `api_key_env` 指向的环境变量。
- 本地密钥放 `.env`（已 gitignore），不要写进 `blue.infd`：

```bash
set -a; . ./.env; set +a
```

校验配置：`uv run blue config-check`。

## 快速开始

```bash
uv sync --group dev          # 安装依赖（含核心 blue 包）
rabbitmq-server -detached    # 或在 flake devShell 中直接 rabbitmq-server
set -a; . ./.env; set +a

# 核心（本体聚合）：会话中心 + 工具目录
uv run blue serve

# 外置程序：各自独立终端启动
uv run python -m handlers.tools.echo
uv run python -m handlers.tools.clock
uv run python -m handlers.ui.chat --session demo --interactive
uv run python -m handlers.ui.chat --text "统计这段文字：hello world"
uv run python -m handlers.ui.inspect tools
uv run python -m handlers.ui.inspect sessions
```

核心命令行只有两个命令：

| 命令 | 说明 |
| --- | --- |
| `blue serve` | 以本体聚合身份运行（会话中心 + 工具目录） |
| `blue config-check` | 编译并校验 `blue.infd` |

工具与界面**不由核心启动**，核心也不会感知它们的进程状态——只感知事件与租约。

## 事件协议

| 主题 | 方向 | 说明 |
| --- | --- | --- |
| `tool.register` / `tool.heartbeat` / `tool.unregister` | 工具 → 所有人 | 自我注册、续约、下线 |
| `catalog.changed` | 核心 → 所有人 | 工具目录快照（带版本号） |
| `catalog.query` / `catalog.reply` | 界面 ↔ 核心 | 目录查询（RPC） |
| `tool.call` / `tool.result` | 核心 ↔ 工具 | 工具调用 RPC |
| `session.prompt` / `session.reply` | 界面 ↔ 核心 | 对话 RPC |
| `session.reset` / `session.state` | 界面 ↔ 核心 | 会话管理（RPC） |

所有 RPC 复用 `infinity_bus.templates.request`：自动注入 `session_id` / `request_id`，
按二者精确匹配响应。

## 传输

- `bus.transport = "rabbit"`：`RabbitFanoutQueue`，每个进程一条持久队列，可多机部署；
- `bus.transport = "memory"`：进程内 fanout，仅用于测试（`MemoryFanout`）。

## 测试

```bash
uv run pytest -q                                     # 单元 + 进程内总线集成
uv run pytest tests/test_integration_rabbit.py -q    # 需可达的 RabbitMQ
```

`tests/test_integration_rabbit.py` 在 broker 不可达时自动跳过；可用
`BLUE_RABBIT_URL` 覆盖连接串。

## 工程约束

- Python 3.14 / uv / ruff / pyright strict / pytest
- `src/blue/` 禁止 `# type: ignore` 与 `# pyright: ignore`
- 门禁：`uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q`
