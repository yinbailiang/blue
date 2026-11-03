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
