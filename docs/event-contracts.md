# 事件契约设计（Phase 1）

## 核心决策

> 本文描述的是**静态事件契约**。运行时“谁活着、工具租约、目录快照”由独立的
> `services/registry` 服务负责，两者不是同一个“注册中心”。

1. **契约按域拥有，不按服务拥有。**
   - 请求/应答天然跨服务；按服务放契约会形成包依赖环。
   - 域契约包只描述“领域里有哪些事件”，不依赖任何服务实现。

2. **两个初始契约包**
   - `contracts/tool` → `blue-contracts-tool`：`tool.*`、`catalog.*`
   - `contracts/session` → `blue-contracts-session`：`session.*`

3. **契约包保持纯净**
   - 只依赖 `pydantic`、`infinity_bus`。
   - `blue-contracts-tool` 额外依赖 `infinity_agent` 以复用 `ToolDefinition`。
   - 不依赖 `blue`，不依赖任何 `blue_service_*`。

4. **每个契约包导出 `EVENTS`**
   - `blue_contracts_tool.EVENTS`
   - `blue_contracts_session.EVENTS`
   - 不再维护全局 `ALL_EVENTS`。

5. **运行时显式注册**
   - `BusRuntime(member_id=..., events=[...])`
   - 每个进程只注册自己生产/消费的事件。
   - 控制面不导入 session/tool 契约。

6. **事件命名与归属**
   - 名称格式：`<domain>.<entity>.<action>`。
   - `tool.*` / `catalog.*` 归 `blue-contracts-tool`。
   - `session.*` 归 `blue-contracts-session`。
   - 事件名保持稳定；版本演进后续通过包版本 + catalog 基线处理，不把版本号塞进事件名。

7. **测试期 catalog**
   - 扫描所有 `blue_contracts_*` 包的 `EVENTS`。
   - 校验：事件名唯一、前缀匹配所属包、`payload_type` 非空。
   - 生成发布/订阅图与事件表（文档产物）。
   - catalog 是只读投影，不拥有契约。

8. **契约依赖进入控制面**
   - 每个契约包带一个 `contract.infd` 声明元数据（name/package/module/description）。
   - 控制面扫描 `contracts/*`，计算契约包 `source_hash`，并纳入 `generation`。
   - 服务在 `service.infd` 中用 `depends_contracts = [...]` 声明依赖。
   - `blue apply` / `Supervisor.reload()` 检测到契约变化时，重启所有声明了该
     契约的服务；未声明依赖的服务不受影响。
   - 服务导入某个契约包时，同时要在 `pyproject.toml` 中声明包依赖。

## 目录结构

```text
contracts/
├── tool/
│   ├── contract.infd
│   ├── pyproject.toml
│   └── src/blue_contracts_tool/
│       ├── __init__.py
│       ├── events.py
│       └── py.typed
└── session/
    ├── contract.infd
    ├── pyproject.toml
    └── src/blue_contracts_session/
        ├── __init__.py
        ├── events.py
        └── py.typed
```

## 运行时接线

```python
from blue_contracts_tool import EVENTS as TOOL_EVENTS
from blue_contracts_session import EVENTS as SESSION_EVENTS
from blue.runtime import build_runtime

runtime = build_runtime("agent", (*TOOL_EVENTS, *SESSION_EVENTS))
```

工具服务通过 `run_tool()` 自带 `TOOL_EVENTS`，无需手动注册。

## 控制面集成

```text
contracts/<name>/contract.infd
        │
        ▼
DesiredContract（meta + package_version + source_hash）
        │
        ├── 纳入 DesiredState.generation
        └── reload 时对比 source_hash
                │
                ▼
        重启 depends_contracts 命中该契约的服务
```

`contract.infd` 示例：

```infd
contract {
  name = "tool"
  package = "blue-contracts-tool"
  module = "blue_contracts_tool"
  description = "工具与目录领域事件契约"
}
```

契约指纹覆盖 `pyproject.toml`、`contract.infd` 与 `src/**/*.py`；因此只改契约
源码、不改服务源码，也会触发依赖服务重启。

## 会话树的协议增量（2026-11-05）

会话从线性消息表升级为树之后，`session.*` 增加了两个事件与三个字段；事件名
本身不变，语义向后兼容（旧记录在升级时折成单链，等同于「只有一条分支」）。

| 事件 | 方向 | 载荷 | 说明 |
| --- | --- | --- | --- |
| `session.branch.activate` | 请求 | `node` | 把该节点的父节点 `current` 指针指向它 |
| `session.branch.activate.reply` | 应答 | `success` / `error_msg` | 切换结果 |
| `session.node.delete` | 请求 | `node` | 删除该节点及其全部后代 |
| `session.node.delete.reply` | 应答 | `removed` | 删除条数（含后代） |

字段增量：

- `SessionSnapshot`：`nodes`（整棵树）+ `current`（当前链的节点 id）+
  `root_current`（根选中的顶层分支）取代原来的线性 `messages`；
  每个 `SessionNode` 只带 `id` / `parent` / `current` / `message` / `created_at`，
  消息正文仍是 `blue.messages` 编码的 JSON 文本，避免 `Message` 的序列化特例。
- `SessionAppendRequest`：`parent`（`null` = 当前链末端，显式 id = 开新分支）；
  `SessionAppendResponse`：`nodes`（新节点 id，末尾即新链尾）。
- `SessionPromptRequest`：`parent` / `regenerate`（只生成回复、不追加用户消息）。

指针语义是「父节点记住当前选中哪个孩子」，因此切换分支不产生写放大，删除分支
只需要修一个指针；几何运算统一在 `blue.tree`，session 服务与 agent 不各算一套。

## 发件箱状态机（2026-11-05）

待发送队列从「取走即销毁」升级成有状态队列，事件名下不变、语义收紧：

| 事件 | 用途 |
| --- | --- |
| `session.outbox.enqueue` / `.list` / `.remove` | 入队、查看、单条取消 |
| `session.outbox.update` / `.move` | 改正文附件、重排（top/up/down） |
| `session.outbox.send_now` | 置顶并广播，agent 立刻拉起队列泵 |
| `session.outbox.retry` / `.clear` | 失败项重排、清空（发送中的保留） |
| `session.outbox.claim` / `.complete` / `.fail` | agent 专用：认领、完成、失败 |

- `QueuedMessage` 增加 `state`（`pending`/`sending`/`failed`）、`attempts`、`error`
- 认领是 `UPDATE ... RETURNING`（pending → sending），**不删行**；跑完才删，
  失败留原因；超过 `outbox_claim_timeout` 仍停在 sending 的自动回收成 pending
- 每次变更都会广播 `session.outbox`（带完整队列），前端据此刷新面板与侧栏角标
- 失败重试上限由 agent 配置 `outbox_max_attempts`（默认 3）决定

## 会话标题（2026-11-05）

`SessionInfo` 增加 `title` / `preview` / `queued`，`SessionSnapshot` 增加 `title`；
标题由 session 服务按首条用户消息生成（28 字），`session.rename` 可手改，
传空标题表示恢复自动（按「当前链」首条用户消息重新推导）。

## 帧级会话标识（2026-11-05）

WebSocket 的 `status` / `stream` / `reply` / `queued_ack` / `cancelled` 帧顶层
都带 `session_id`，前端才能把事件路由到对应会话（多会话并行的前提）。

## 迁移阶段

- **Phase 1（本次）**
  - 新建两个契约包。
  - `BusRuntime` 改为显式 `events`。
  - 核心 `blue` 移除 `ALL_EVENTS` / `register_events` / `EVENT_NAMES`。
  - 服务与测试迁移到契约包导入。
  - 增加事件目录测试。
- **Phase 2**
  - 增加依赖审计测试，清理未声明导入。
  - 生成 `docs/events.md` 与发布/订阅图。
  - 契约变更只重启 `depends_contracts` 直接命中的服务；后续可扩展到契约间依赖。
- **Phase 3（按需）**
  - `.inft` schema / codegen / catalog lock / 运行时 registry。
  - 跨语言客户端、外部插件、版本协商。

## 非目标（Phase 1）

- 跨语言契约生成。
- 运行时事件注册中心。
- 动态版本协商。
- 把 `blue` 拆成 `blue-bus` / `blue-control` / `blue-sdk` 多个发行包。

## 验收标准

- `uv run pytest -q` 全绿。
- `uv run pyright` 通过 strict。
- `uv run ruff check .` 通过。
- 代码中不再出现 `blue.protocol`、`ALL_EVENTS`、`register_events`。
- 每个服务只注册自己需要的事件。
- 契约包变化时，依赖它的服务会被重启。
