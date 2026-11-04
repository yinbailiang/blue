"""服务测试共享夹具：模拟 supervisor 下发运行时配置。

环境变量不再承载服务配置：项目根、状态目录、总线地址等由服务目录下的
``service_runtime_config.json`` 提供——文件里 ``config`` 段是服务自身配置，
``infra`` 段是共享的基础设施参数。测试通过 ``install_infra`` 夹具在临时目录
里造出这份文件，并把 ``BLUE_CONFIG_FILE`` 指向它。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from blue.runtime import (
    CONFIG_SECTION,
    DEFAULT_BUS_URL,
    DEFAULT_RUNTIME_CONFIG,
    INFRA_SECTION,
    reset_infra_config,
)

InstallInfra = Callable[..., Path]


@pytest.fixture(autouse=True)
def _isolate_infra() -> Iterator[None]:
    """基础设施配置有进程级缓存，每个用例前后都清空。"""
    reset_infra_config()
    yield
    reset_infra_config()


@pytest.fixture
def install_infra(monkeypatch: pytest.MonkeyPatch) -> InstallInfra:
    """返回一个函数：把项目根指向指定目录，模拟 supervisor 下发。"""

    def _install(
        project_root: Path,
        *,
        state_dir: Path | None = None,
        config: dict[str, object] | None = None,
    ) -> Path:
        base = project_root / ".infra"
        base.mkdir(parents=True, exist_ok=True)
        config_path = base / DEFAULT_RUNTIME_CONFIG
        payload = {
            CONFIG_SECTION: config or {},
            INFRA_SECTION: {
                "project_root": str(project_root),
                "bus_url": DEFAULT_BUS_URL,
                "state_dir": str(state_dir or project_root / ".blue"),
            },
        }
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setenv("BLUE_CONFIG_FILE", str(config_path))
        return config_path

    return _install
