"""自重启判定：收到 SIGUSR1 就照做，只拦「配置编译不过」这一种。

这里曾经自己再算一遍 SDK 指纹去"确认代码变没变"——那是循环论证：进程只能用
**自己的（旧的）** 代码算指纹。当指纹算法本身有 bug（只覆盖 pyproject.toml）时，
它永远得出"没变"，自重启从未发生。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from blue.cli import _maybe_self_restart  # pyright: ignore[reportPrivateUsage]
from blue.config import load_desired

ROOT = Path(__file__).resolve().parents[1]


class _FakeSupervisor:
    """只记录是否被要求重载。"""

    def __init__(self) -> None:
        self.reloads = 0

    def request_reload(self) -> None:
        self.reloads += 1


def test_restarts_on_signal() -> None:
    """配置能编译 → 直接同意 execv（不自己再判一遍代码有没有变）。"""
    desired = load_desired(ROOT / "blue.infd")
    supervisor = _FakeSupervisor()
    assert asyncio.run(_maybe_self_restart(desired, supervisor)) is True  # type: ignore[arg-type]
    assert supervisor.reloads == 0


def test_falls_back_to_reload_when_config_is_broken(tmp_path: Path) -> None:
    """配置编译不过就放弃 execv——否则新镜像会直接退出，而此时没有外部看护。"""
    desired = load_desired(ROOT / "blue.infd")
    broken = tmp_path / "blue.infd"
    broken.write_text("这不是合法配置 {\n", encoding="utf-8")
    desired.config_path = broken
    supervisor = _FakeSupervisor()
    assert asyncio.run(_maybe_self_restart(desired, supervisor)) is False  # type: ignore[arg-type]
    assert supervisor.reloads == 1
