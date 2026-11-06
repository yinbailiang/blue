"""期望状态渲染测试：``render_state`` 的返回值必须能直接 json.dumps。

模板引擎用 ``Decimal`` 表示 infd 里的浮点字面量，``render_state`` 一度把它原样
塞进返回值，于是 ``blue render`` 在序列化那一刻抛 ``TypeError``——写盘那条路径有
``_json_default`` 兜底，渲染这条没有。
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import pytest

from blue.config import load_desired
from blue.control.desired import render_state
from blue.control.model import DesiredState

ROOT = Path(__file__).resolve().parents[1]
"""项目根（含 blue.infd）。"""


@pytest.fixture(scope="module")
def desired() -> DesiredState:
    """编译仓库自带的配置；模板里的数值字段是 Decimal。"""
    return load_desired(ROOT / "blue.infd")


def test_render_state_is_json_serialisable(desired: DesiredState) -> None:
    """含 Decimal 的模板配置也能直接序列化（不加任何 default 兜底）。"""
    rendered = render_state(desired, redact=False)

    json.dumps(rendered, ensure_ascii=False)


def test_decimal_config_is_rendered_as_number(desired: DesiredState) -> None:
    """Decimal 投影成 JSON 数字：保留数值语义，不是字符串 ``"1800.0"``。

    整值按 emit 层语义落成 ``int``（避免 ``1.0`` 这种噪声），所以这里只要求
    "是数字"——绑死 float 是在测实现细节，不是测语义。
    """
    config = render_state(desired, redact=False)["services"]["agent"]["config"]

    assert isinstance(config["tool_timeout"], int | float)
    assert not isinstance(config["tool_timeout"], str)
    assert config["tool_timeout"] == 1800


def test_path_config_is_projected_to_posix_string(desired: DesiredState) -> None:
    """``p"..."`` 的路径要投影成 POSIX 字符串，不能原样留在返回值里。

    盯的是「``PurePosixPath`` 不是 ``Path`` 子类」这个坑：infd 的 ``p"..."``
    产出的正是 ``PurePosixPath``，手写 ``isinstance(v, Path)`` 会直接漏掉它，
    于是 ``blue render`` / 指纹计算在序列化那一刻炸掉。投影统一交给 emit 层
    （``EmitConfig`` 默认 ``full_path=False`` → POSIX 字符串）。
    """
    service = desired.services["terminal"]
    patched = service.model_copy(
        update={"config": {"workdir": PurePosixPath("/tmp/x")}}
    )
    state = desired.model_copy(update={"services": {"terminal": patched}})

    rendered = render_state(state, redact=False)["services"]["terminal"]["config"]

    assert rendered["workdir"] == "/tmp/x"
    assert isinstance(rendered["workdir"], str)


def test_redacted_render_state_is_json_serialisable(desired: DesiredState) -> None:
    """脱敏路径同样可序列化，且疑似密钥的字段被替换。"""
    rendered = render_state(desired)

    json.dumps(rendered, ensure_ascii=False)
    assert rendered["services"]["agent"]["config"]["api_key"] == "***"
