"""期望状态渲染测试：``render_state`` 的返回值必须能直接 json.dumps。

模板引擎用 ``Decimal`` 表示 infd 里的浮点字面量，``render_state`` 一度把它原样
塞进返回值，于是 ``blue render`` 在序列化那一刻抛 ``TypeError``——写盘那条路径有
``_json_default`` 兜底，渲染这条没有。
"""

from __future__ import annotations

import json
from pathlib import Path

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


def test_decimal_config_is_rendered_as_float(desired: DesiredState) -> None:
    """Decimal 转成 float：保留数值语义，不是字符串 ``"1800.0"``。"""
    config = render_state(desired, redact=False)["services"]["agent"]["config"]

    assert isinstance(config["tool_timeout"], float)
    assert config["tool_timeout"] == 1800.0


def test_redacted_render_state_is_json_serialisable(desired: DesiredState) -> None:
    """脱敏路径同样可序列化，且疑似密钥的字段被替换。"""
    rendered = render_state(desired)

    json.dumps(rendered, ensure_ascii=False)
    assert rendered["services"]["agent"]["config"]["api_key"] == "***"
