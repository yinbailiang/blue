"""期望状态渲染测试：``render_state`` 的返回值必须能直接 json.dumps。

模板引擎用 ``Decimal`` 表示 infd 里的浮点字面量，``render_state`` 一度把它原样
塞进返回值，于是 ``blue render`` 在序列化那一刻抛 ``TypeError``——写盘那条路径有
``_json_default`` 兜底，渲染这条没有。
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

from blue.config import load_desired
from blue.control.desired import load_supervisor, render_state, runtime_config_content
from blue.control.model import DesiredState, IdempotencySettings
from blue.errors import ConfigError
from blue.runtime import InfraConfig

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


def test_meta_env_is_redacted(desired: DesiredState) -> None:
    """``meta.env`` 的值默认打码：键名不可信（``FOO=secret`` 也合法）。

    服务清单会原样进 ``blue render`` / 世代快照，而 ``_redact`` 只认"密钥味儿"的
    字段名——``env`` 整块会漏过去，将来真把密钥写进 env 就会明文输出。
    """
    service = desired.services["terminal"]
    patched = service.model_copy(
        update={"meta": service.meta.model_copy(update={"env": {"FOO": "s3cret"}})}
    )
    state = desired.model_copy(update={"services": {"terminal": patched}})

    masked = render_state(state)["services"]["terminal"]["meta"]["env"]
    plain = render_state(state, redact=False)["services"]["terminal"]["meta"]["env"]

    assert masked == {"FOO": "***"}
    assert plain == {"FOO": "s3cret"}


def _infra_of(service: str, desired: DesiredState) -> dict[str, object]:
    """取某个服务运行配置里的 ``infra`` 段。"""
    payload = json.loads(runtime_config_content(desired.services[service], desired))
    return cast(dict[str, object], payload["infra"])


def test_every_infra_config_field_is_projected(desired: DesiredState) -> None:
    """守卫：``InfraConfig`` 的每个字段都必须由 supervisor 真的写进去。

    写死的键（曾经的 ``"idempotency": True``）会让配置项在服务侧永远改不动，而
    声明照样存在——排查时极易误判。新增字段忘了投影，这条就变红。
    """
    infra = _infra_of("agent", desired)

    missing = set(InfraConfig.model_fields) - set(infra)
    assert missing == set(), f"这些 infra 字段没有下发给服务：{sorted(missing)}"


def test_idempotency_settings_follow_supervisor_config(desired: DesiredState) -> None:
    """去重相关参数来自 ``supervisor`` 配置，不是写死的常量。"""
    patched = desired.model_copy(
        update={
            "supervisor": desired.supervisor.model_copy(
                update={
                    "idempotency": IdempotencySettings(
                        enabled=False, retention=1234.0, sweep_interval=56.0
                    ),
                }
            )
        }
    )

    infra = _infra_of("agent", patched)

    assert infra["idempotency"] is False
    assert infra["idempotency_retention"] == 1234.0
    assert infra["idempotency_sweep_interval"] == 56.0


def test_stop_timeout_comes_from_service_meta(desired: DesiredState) -> None:
    """停机预算按服务下发：服务自己据此把排空预算压在 supervisor 的 SIGKILL 之前。"""
    for name, service in desired.services.items():
        assert _infra_of(name, desired)["stop_timeout"] == service.meta.stop_timeout


def test_shutdown_timeout_covers_the_slowest_service(desired: DesiredState) -> None:
    """整体停机预算不得低于任一服务的 ``stop_timeout``。

    低于它等于「即使只停这一个服务也必然 SIGKILL」：bus 是 30 秒，所以这里曾经用
    10 秒的默认值实际上从来没够用过。
    """
    longest = max(service.meta.stop_timeout for service in desired.services.values())

    assert desired.supervisor.shutdown_timeout >= longest


def test_every_service_gets_a_30s_stop_budget(desired: DesiredState) -> None:
    """每个服务的优雅停止预算都是 30 秒（需求：单服务 30s、整场 60s）。

    低于它服务侧的排空预算就跟着缩水（`stop_timeout - min(2, 一半)`），在跑的处理器
    会在停机时被取消。这条同时钉住"以后别悄悄把某个服务调小"。
    """
    for name, service in desired.services.items():
        assert service.meta.stop_timeout >= 30.0, f"{name} 的 stop_timeout 被调小了"


def test_supervisor_nested_block_maps_to_grouped_model(tmp_path: Path) -> None:
    """supervisor 块的分组结构与 ``supervisor.inft`` 同构，pydantic 直接嵌套解析。

    中间没有映射层：配置里叫什么、模型上就叫什么。加一个配置项只需改两处
    （模板 + 模型），不存在"漏改映射表导致某项配置永远改不动"。
    """
    settings = load_supervisor(
        {
            "shutdown_timeout": "90.0",
            "log": {"max_bytes": 2048, "backups": 7},
            "registry": {"ttl": "30.0", "sweep_interval": "5.0"},
            "idempotency": {"enabled": False, "retention": "60.0"},
            "activation": {
                "timeout": "300.0",
                "await_control_change": False,
                "smoke": {"enabled": False, "timeout": "5.0"},
            },
            "journal": {
                "enabled": True,
                "max_bytes": 67108864,
                "max_rows": None,
                "payload": {"mode": "full", "allowlist": ["tool.call"]},
            },
            "generation": {
                "keep": 9,
                "artifact": {"python": "3.13", "link_mode": "copy"},
            },
        },
        tmp_path,
    )

    assert settings.shutdown_timeout == 90.0
    assert (settings.log.max_bytes, settings.log.backups) == (2048, 7)
    assert (settings.registry.ttl, settings.registry.sweep_interval) == (30.0, 5.0)
    assert settings.idempotency.enabled is False
    assert settings.idempotency.retention == 60.0
    assert settings.activation.timeout == 300.0
    assert settings.activation.await_control_change is False
    assert settings.activation.smoke.enabled is False
    assert settings.activation.smoke.timeout == 5.0
    assert settings.journal.enabled is True
    assert settings.journal.max_bytes == 67108864
    assert settings.journal.max_rows is None
    assert settings.journal.payload.mode == "full"
    assert settings.journal.payload.allowlist == ("tool.call",)
    assert settings.generation.keep == 9
    assert settings.generation.artifact.python == "3.13"
    assert settings.generation.artifact.link_mode == "copy"


@pytest.mark.parametrize(
    "legacy_key",
    ["activation_timeout", "log_backups", "journal_max_bytes", "stable_after"],
)
def test_supervisor_legacy_flat_keys_are_rejected(
    tmp_path: Path, legacy_key: str
) -> None:
    """旧的平铺键必须**报错**，绝不静默失效。

    静默忽略的后果就是这项配置永远改不动——那正是分组化要消灭的那类 bug。
    """
    with pytest.raises(ConfigError, match=legacy_key):
        load_supervisor({legacy_key: 1}, tmp_path)


@pytest.mark.parametrize("name", ["journal", "idempotency"])
def test_supervisor_boolean_switch_is_rejected_with_hint(
    tmp_path: Path, name: str
) -> None:
    """``journal`` / ``idempotency`` 从布尔变成了分组对象，旧写法要给明确提示。"""
    with pytest.raises(ConfigError, match="分组对象"):
        load_supervisor({name: True}, tmp_path)
