"""blue —— 分布式 Agent 系统的控制面与共享 SDK。

``blue`` 核心不依赖任何契约包：工具调用等域运行时在 ``blue-tool-sdk``，
契约在 ``contracts/*``。事件注册由服务配置驱动，见
:func:`blue.runtime.build_runtime`。
"""

from blue.config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    DesiredState,
    SupervisorSettings,
    load_desired,
)
from blue.control import Supervisor, render_state, write_runtime_configs
from blue.errors import BlueError, RuntimeConfigError, SupervisorError
from blue.runtime import (
    RUNTIME_CONFIG_VERSION,
    build_runtime,
    load_runtime_config,
    registry_ttl,
    run_service,
)
from blue.transport import (
    BusRuntime,
    MemoryFanout,
    MemoryRuntime,
    RabbitRuntime,
)
from blue.tree import ROOT_ID, Tree, TreeError, TreeNode

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "ROOT_ID",
    "RUNTIME_CONFIG_VERSION",
    "BlueError",
    "BusRuntime",
    "ConfigError",
    "DesiredState",
    "MemoryFanout",
    "MemoryRuntime",
    "RabbitRuntime",
    "RuntimeConfigError",
    "Supervisor",
    "SupervisorError",
    "SupervisorSettings",
    "Tree",
    "TreeError",
    "TreeNode",
    "build_runtime",
    "load_desired",
    "load_runtime_config",
    "registry_ttl",
    "render_state",
    "run_service",
    "write_runtime_configs",
]
