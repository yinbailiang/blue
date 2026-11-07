"""pytest 全局配置：把测试环境与"运行中的 blue 系统"隔离。

服务进程是由调谐器启动的，环境里带着 ``BLUE_SERVICE_NAME`` / ``BLUE_CONFIG_FILE``
等变量。如果从这个环境的终端里跑 ``pytest``，这些变量会被继承，于是：

- ``blue.runtime.load_runtime_config()`` 认为自己在 supervisor 下，于是要求配置
  必须存在、必须带 ``infra`` 段 → 服务测试大面积失败（实测 35 个用例）；
- 那个路径还可能指向一个已被 ``blue gc`` 回收的世代，报「运行时配置不存在」；
- ``StateStore.cleanup_orphans`` 之类按 instance id 认进程的逻辑也可能误判。

所以这里在**导入时**（早于任何测试模块）就把它们摘掉，而不是靠 fixture——
免得某个模块在 import 阶段就读了环境。

## 下线注销超时

``blue_tool_sdk.worker.WITHDRAW_TIMEOUT`` 默认 1.5 秒：工具进程退出时用它去
注册中心注销租约，注册中心不在就放弃（租约本来也会过期）。产品语义没问题，
但在测试里**注册中心从来不会应答**，于是每个用到 ``ToolWorker`` / ``ToolRegistrar``
的用例都在收尾时死等满 1.5 秒——实测单个用例 1.50s 里 1.45s 是纯等待
（``proxy.py`` 的 ``wait_for`` 打满），全量约 28 个用例白白烧掉 28 秒。

这里把它压到 0.05 秒：租约注销在本进程内是「发出去就没下文」的，测试不验证
这条路径的等待时长（没有任何用例断言 1.5s 这个值），压小不改变任何断言语义。
"""

from __future__ import annotations

import os

import pytest

from blue_tool_sdk import worker as _tool_worker

_INHERITED_ENV = (
    "BLUE_SERVICE_NAME",
    "BLUE_INSTANCE_ID",
    "BLUE_CONFIG_FILE",
    "BLUE_ARTIFACT_HASH",
)

for _name in _INHERITED_ENV:
    os.environ.pop(_name, None)

FAST_WITHDRAW_TIMEOUT = 0.05
"""测试里注销租约的超时（秒）：注册中心不会应答，等 1.5 秒纯属浪费。"""


@pytest.fixture(autouse=True)
def _fast_tool_withdraw(monkeypatch: pytest.MonkeyPatch) -> None:
    """把工具下线时的租约注销超时压到 ``FAST_WITHDRAW_TIMEOUT``。

    用 ``monkeypatch`` 而不是直接改常量，用例结束自动还原——避免某个测试
    依赖「已被改小」的全局值而悄悄通过。
    """
    monkeypatch.setattr(_tool_worker, "WITHDRAW_TIMEOUT", FAST_WITHDRAW_TIMEOUT)
