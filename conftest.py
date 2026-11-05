"""pytest 全局配置：把测试环境与"运行中的 blue 系统"隔离。

服务进程是由调谐器启动的，环境里带着 ``BLUE_SERVICE_NAME`` / ``BLUE_CONFIG_FILE``
等变量。如果从这个环境的终端里跑 ``pytest``，这些变量会被继承，于是：

- ``blue.runtime.load_runtime_config()`` 认为自己在 supervisor 下，于是要求配置
  必须存在、必须带 ``infra`` 段 → 服务测试大面积失败（实测 35 个用例）；
- 那个路径还可能指向一个已被 ``blue gc`` 回收的世代，报「运行时配置不存在」；
- ``StateStore.cleanup_orphans`` 之类按 instance id 认进程的逻辑也可能误判。

所以这里在**导入时**（早于任何测试模块）就把它们摘掉，而不是靠 fixture——
免得某个模块在 import 阶段就读了环境。
"""

from __future__ import annotations

import os

_INHERITED_ENV = (
    "BLUE_SERVICE_NAME",
    "BLUE_INSTANCE_ID",
    "BLUE_CONFIG_FILE",
    "BLUE_ARTIFACT_HASH",
)

for _name in _INHERITED_ENV:
    os.environ.pop(_name, None)
