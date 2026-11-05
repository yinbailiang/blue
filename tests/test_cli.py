"""CLI 测试：config-check、render、status 与无 supervisor 时的 apply。"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from blue.cli import main
from blue.config import DesiredState
from blue.control.artifacts import ArtifactStore
from blue.control.desired import load_desired
from blue.control.generations import Generation, GenerationStore
from blue.control.model import (
    ActivationState,
    ActualContract,
    ActualSdk,
    ActualService,
    ActualState,
    DesiredSdkPackage,
    SdkMeta,
    ServiceStatus,
)
from blue.control.smoke import Check, SmokeReport
from blue.control.state import StateStore
from blue.runtime import RUNTIME_CONFIG_VERSION

ROOT = Path(__file__).resolve().parents[1]


def _stub_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    """跳过产物构建。

    产物构建需要真实的开发用 ``.venv``（测试工作区里没有）；这些用例只关心
    "apply 发了什么信号"，所以把构建这一步打桩，其余链路（冻结世代、翻转
    current、通知调谐器）仍然真实执行。
    """

    def no_build(desired: object, artifacts: object) -> None:
        return None

    monkeypatch.setattr("blue.cli._build_artifacts", no_build)


def _config(root: Path) -> Path:
    contract_dir = root / "contracts" / "demo"
    (contract_dir / "src" / "demo_contracts").mkdir(parents=True, exist_ok=True)
    (contract_dir / "contract.infd").write_text(
        """
contract {
  name = "demo"
  package = "demo-contracts"
  module = "demo_contracts"
  description = "demo contract"
}
""",
        encoding="utf-8",
    )
    (contract_dir / "pyproject.toml").write_text(
        """
[project]
name = "demo-contracts"
version = "0.1.0"
requires-python = ">=3.14"

[build-system]
requires = ["uv_build>=0.9.0,<0.14"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "demo_contracts"
module-root = "src"
""",
        encoding="utf-8",
    )
    (contract_dir / "src" / "demo_contracts" / "__init__.py").write_text(
        "", encoding="utf-8"
    )

    sdk_dir = root / "sdk" / "demo"
    (sdk_dir / "src" / "demo_sdk").mkdir(parents=True, exist_ok=True)
    (sdk_dir / "sdk.infd").write_text(
        """
sdk {
  name = "demo"
  package = "demo-sdk"
  module = "demo_sdk"
  description = "demo sdk"
}
""",
        encoding="utf-8",
    )
    (sdk_dir / "pyproject.toml").write_text(
        """
[project]
name = "demo-sdk"
version = "0.2.0"
requires-python = ">=3.14"

[build-system]
requires = ["uv_build>=0.9.0,<0.14"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "demo_sdk"
module-root = "src"
""",
        encoding="utf-8",
    )
    (sdk_dir / "src" / "demo_sdk" / "__init__.py").write_text("", encoding="utf-8")

    service_dir = root / "services" / "demo"
    service_dir.mkdir(parents=True, exist_ok=True)
    (service_dir / "service.infd").write_text(
        """
~ServiceTemplate {
  value: str = "x"
}

service {
  name = "demo"
  kind = "tool"
  package = "demo"
  module = "demo"
  depends_on = []
  depends_contracts = ["demo"]
  depends_sdk = ["demo"]
}
""",
        encoding="utf-8",
    )
    path = root / "blue.infd"
    path.write_text(
        """
!from p"services/demo/service.infd" import ServiceTemplate as DemoService

supervisor {
  state_dir: str = ".blue"
  reconcile_interval: <float, positive> = 0.2
}

demo = DemoService(value = "hello")
""",
        encoding="utf-8",
    )
    return path


def test_config_check_prints_services(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """config-check 输出服务与契约摘要。"""
    code = main(["--config", str(_config(tmp_path)), "config-check"])
    output = capsys.readouterr().out
    assert code == 0
    assert "services   : 1" in output
    assert "contracts  : 1" in output
    assert "sdk        : 1" in output
    assert "demo" in output


def test_render_outputs_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """render 输出可解析的期望状态。"""
    code = main(["--config", str(_config(tmp_path)), "render"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["services"]["demo"]["config"]["value"] == "hello"


def test_status_without_supervisor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """没有 supervisor 时 status 回退到期望状态，并展示契约版本。"""
    code = main(["--config", str(_config(tmp_path)), "status"])
    output = capsys.readouterr().out
    assert code == 0
    assert "stopped" in output
    assert "services   : 1" in output
    assert "contracts  : 1" in output
    assert "sdk        : 1" in output
    assert "demo-contracts" in output
    assert "demo-sdk" in output
    assert "0.1.0" in output
    assert "0.2.0" in output


def test_status_json_includes_contracts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """status --json 暴露契约版本与使用方。"""
    code = main(["--config", str(_config(tmp_path)), "status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["state"] is None
    assert payload["contracts"]["demo"]["version"] == "0.1.0"
    assert payload["contracts"]["demo"]["used_by"] == ["demo"]
    assert payload["sdk"]["demo"]["package"] == "demo-sdk"
    assert payload["sdk"]["demo"]["version"] == "0.2.0"
    assert payload["sdk"]["demo"]["used_by"] == ["demo"]


def test_status_reads_actual_contracts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """有实际快照时优先展示实际版本。"""
    config = _config(tmp_path)
    state = StateStore(tmp_path / ".blue")
    state.write_state(
        ActualState(
            generation="gen-1",
            supervisor_pid=1,
            started_at=0.0,
            updated_at=0.0,
            services={
                "demo": ActualService(
                    name="demo",
                    kind="tool",
                    status=ServiceStatus.READY,
                    pid=123,
                    package_version="9.9.9",
                )
            },
            contracts={
                "demo": ActualContract(
                    name="demo",
                    package="demo-contracts",
                    module="demo_contracts",
                    package_version="1.2.3",
                    source_hash="abc123",
                )
            },
        )
    )

    code = main(["--config", str(config), "status"])
    output = capsys.readouterr().out
    assert code == 0
    assert "9.9.9" in output
    assert "1.2.3" in output
    assert "demo-contracts" in output
    assert "Ready" in output
    assert "desired    :" in output
    assert "blue apply" in output


def _state_with_old_sdk(tmp_path: Path) -> None:
    """写一份实际快照：SDK 包指纹与当前期望不同。"""
    state = StateStore(tmp_path / ".blue")
    state.write_state(
        ActualState(
            generation="gen-1",
            supervisor_pid=1,
            started_at=0.0,
            updated_at=0.0,
            services={
                "demo": ActualService(
                    name="demo",
                    kind="tool",
                    status=ServiceStatus.READY,
                )
            },
            sdk=ActualSdk(
                package_version="0.1.0",
                source_hash="core",
                packages={
                    "demo": DesiredSdkPackage(
                        meta=SdkMeta(
                            name="demo", package="demo-sdk", module="demo_sdk"
                        ),
                        package_version="0.2.0",
                        source_hash="old-hash",
                    )
                },
            ),
        )
    )


def test_plan_shows_sdk_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``blue plan`` 在文本输出里展示 SDK 包变更与受影响服务。"""
    config = _config(tmp_path)
    _state_with_old_sdk(tmp_path)

    code = main(["--config", str(config), "plan"])

    output = capsys.readouterr().out
    assert code == 1
    assert "SDK 变更" in output
    assert "影响：demo" in output


def test_plan_json_includes_sdk(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``blue plan --json`` 暴露 SDK 包变更。"""
    config = _config(tmp_path)
    _state_with_old_sdk(tmp_path)

    code = main(["--config", str(config), "plan", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["sdk"] == [
        {
            "name": "demo",
            "action": "change",
            "before": "old-hash",
            "after": payload["sdk"][0]["after"],
            "affected": ["demo"],
        }
    ]
    assert payload["sdk"][0]["after"]


def test_apply_without_supervisor_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无 supervisor 时 apply 返回配置错误码。"""
    _stub_artifacts(monkeypatch)
    assert main(["--config", str(_config(tmp_path)), "apply"]) == 2


def test_apply_asks_self_restart_when_config_format_is_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """配置格式过旧 → 让调谐器重启自己（不再要求人工 down/up）。"""
    config = _config(tmp_path)
    _stub_artifacts(monkeypatch)
    store = StateStore(tmp_path / ".blue")
    store.acquire(
        generation="gen-1",
        blue_version="0.1.0",
        config_version=RUNTIME_CONFIG_VERSION - 1,
    )
    signals: list[int] = []
    # 只关心发哪种信号：验证过程打桩（否则会真等闸门 150s）
    monkeypatch.setattr("blue.cli._verify_activation", _no_verify)

    def fake_kill(pid: int, sig: int) -> None:
        if sig != 0:
            signals.append(sig)

    monkeypatch.setattr("blue.cli.os.kill", fake_kill)
    try:
        code = main(["--config", str(config), "apply"])
    finally:
        store.release()

    assert code == 0
    assert signals == [signal.SIGUSR1]
    assert "重启自己" in capsys.readouterr().out


def test_apply_asks_self_restart_on_blue_version_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """blue 版本不同 → 自重启（SIGUSR1）。"""
    config = _config(tmp_path)
    _stub_artifacts(monkeypatch)
    monkeypatch.setattr("blue.cli._blue_version", lambda: "9.9.9")
    store = StateStore(tmp_path / ".blue")
    store.acquire(
        generation="gen-1", blue_version="0.0.0", config_version=RUNTIME_CONFIG_VERSION
    )
    signals: list[int] = []
    # 只关心发哪种信号：验证过程打桩（否则会真等闸门 150s）
    monkeypatch.setattr("blue.cli._verify_activation", _no_verify)

    def fake_kill(pid: int, sig: int) -> None:
        if sig != 0:
            signals.append(sig)

    monkeypatch.setattr("blue.cli.os.kill", fake_kill)
    try:
        code = main(["--config", str(config), "apply"])
    finally:
        store.release()

    assert code == 0
    assert signals == [signal.SIGUSR1]
    assert "重启自己" in capsys.readouterr().out


def test_apply_signals_compatible_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """版本一致时正常发送 SIGHUP。"""
    config = _config(tmp_path)
    _stub_artifacts(monkeypatch)
    monkeypatch.setattr("blue.cli._blue_version", lambda: "9.9.9")
    store = StateStore(tmp_path / ".blue")
    store.acquire(
        generation="gen-1", blue_version="9.9.9", config_version=RUNTIME_CONFIG_VERSION
    )
    signals: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        if sig != 0:
            signals.append((pid, sig))

    monkeypatch.setattr("blue.cli.os.kill", fake_kill)
    try:
        code = main(["--config", str(config), "apply"])
    finally:
        store.release()

    assert code == 0
    assert signals == [(os.getpid(), signal.SIGHUP)]
    assert "已通知 supervisor" in capsys.readouterr().out


def test_logs_missing_returns_error(tmp_path: Path) -> None:
    """日志不存在时 logs 返回 1。"""
    assert main(["--config", str(_config(tmp_path)), "logs", "demo"]) == 1


def test_real_config_check(capsys: pytest.CaptureFixture[str]) -> None:
    """仓库自带的 blue.infd 可编译。"""
    code = main(["--config", str(ROOT / "blue.infd"), "config-check"])
    output = capsys.readouterr().out
    assert code == 0
    assert "agent" in output
    assert "bus" in output


def test_real_render_outputs_json(capsys: pytest.CaptureFixture[str]) -> None:
    """仓库自带的 blue.infd 渲染成 JSON 不崩（模板里有 Decimal 数值字段）。"""
    code = main(["--config", str(ROOT / "blue.infd"), "render"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["services"]["agent"]["config"]["tool_timeout"] == 1800.0
    assert payload["services"]["agent"]["config"]["api_key"] == "***"


def test_apply_asks_self_restart_only_when_sdk_hash_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK 指纹变了才自重启；没变就只重载——这是「不会无限重启」的关键。

    apply 一次：指纹不同 → SIGUSR1 → 新镜像把新指纹写进锁；
    再 apply 一次：指纹相同 → SIGHUP → 不再重启。一次 apply 只重启一次。
    """
    from blue.cli import _needs_self_restart  # pyright: ignore[reportPrivateUsage]
    from blue.config import load_desired
    from blue.control.state import LockInfo

    # 用仓库真实配置：`src/blue` 只在项目根下才存在，SDK 指纹才有值
    desired = load_desired(ROOT / "blue.infd")
    old = LockInfo(
        pid=1,
        started_at=0.0,
        generation="g",
        blue_version="",
        config_version=RUNTIME_CONFIG_VERSION,
        sdk_hash="stale-hash",
    )
    same = LockInfo(
        pid=1,
        started_at=0.0,
        generation="g",
        blue_version="",
        config_version=RUNTIME_CONFIG_VERSION,
        sdk_hash=desired.sdk.source_hash,
    )
    assert _needs_self_restart(old, desired=desired) is True
    assert _needs_self_restart(same, desired=desired) is False


def test_exec_self_uses_the_supervise_subcommand(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """自重启走 ``blue _supervise <config>``：PID 不变、锁不丢。"""
    from blue.cli import _exec_self  # pyright: ignore[reportPrivateUsage]
    from blue.config import load_desired

    config = _config(tmp_path)
    desired = load_desired(config)
    captured: list[tuple[str, list[str]]] = []

    def fake_execv(path: str, argv: list[str]) -> None:
        captured.append((path, list(argv)))
        raise SystemExit(0)

    monkeypatch.setattr("blue.cli.os.execv", fake_execv)
    with pytest.raises(SystemExit):
        _exec_self(desired)
    assert captured and captured[0][1][-2:] == ["_supervise", str(desired.config_path)]


def test_supervise_subcommand_accepts_config_path(tmp_path: Path) -> None:
    """``_supervise`` 必须能解析 up / execv 传入的配置路径。"""
    from blue.cli import _parser  # pyright: ignore[reportPrivateUsage]

    config = tmp_path / "blue.infd"
    args = _parser().parse_args(["_supervise", str(config)])
    assert args.command == "_supervise"
    assert args.config_path == str(config)


def test_module_entry_point_works() -> None:
    """``python -m blue`` 可用：后台调谐器与自重启都依赖这个入口。"""
    result = subprocess.run(
        [sys.executable, "-m", "blue", "--help"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert result.returncode == 0, result.stderr
    assert "up" in result.stdout


# ---------------------------------------------------------------------------
# 世代相关命令：apply --staged / activate / rollback / generations / gc
# ---------------------------------------------------------------------------


def _lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StateStore:
    """伪造"supervisor 正在运行"：写锁文件并拦截信号。"""
    store = StateStore(tmp_path / ".blue")
    store.ensure_dirs()
    store.acquire(
        generation="gen-1", blue_version="9.9.9", config_version=RUNTIME_CONFIG_VERSION
    )
    monkeypatch.setattr("blue.cli._blue_version", lambda: "9.9.9")

    def fake_kill(pid: int, sig: int) -> None:
        return None

    monkeypatch.setattr("blue.cli.os.kill", fake_kill)
    return store


def _generations(tmp_path: Path) -> GenerationStore:
    return GenerationStore(tmp_path / ".blue")


def _rewrite_value(config: Path, value: str) -> None:
    """改 blue.infd 的一个配置值，制造"配置变化"。"""
    text = config.read_text(encoding="utf-8")
    text = re.sub(
        r'DemoService\(value = "[^"]*"\)',
        f'DemoService(value = "{value}")',
        text,
    )
    config.write_text(text, encoding="utf-8")


def test_apply_staged_does_not_switch_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--staged 只冻结世代，不动 current（等价 nixos-rebuild boot）。"""
    config = _config(tmp_path)
    _stub_artifacts(monkeypatch)
    store = _lock(tmp_path, monkeypatch)
    try:
        assert main(["--config", str(config), "apply", "--staged"]) == 0
    finally:
        store.release()

    generations = _generations(tmp_path)
    assert generations.current() is None
    assert [generation.id for generation in generations.list()] == [1]
    assert "未激活" in capsys.readouterr().out


def test_activate_switches_current_and_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """activate 切换 current 并通知调谐器。"""
    config = _config(tmp_path)
    _stub_artifacts(monkeypatch)
    lock = _lock(tmp_path, monkeypatch)
    try:
        assert main(["--config", str(config), "apply", "--staged"]) == 0
        assert main(["--config", str(config), "activate", "1"]) == 0
    finally:
        lock.release()

    current = _generations(tmp_path).current()
    assert current is not None
    assert current.id == 1


def test_apply_unchanged_config_reuses_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """配置没变时 apply 不产生新世代。"""
    config = _config(tmp_path)
    _stub_artifacts(monkeypatch)
    lock = _lock(tmp_path, monkeypatch)
    try:
        assert main(["--config", str(config), "apply"]) == 0
        assert main(["--config", str(config), "apply"]) == 0
    finally:
        lock.release()

    generations = _generations(tmp_path)
    assert len(generations.list()) == 1
    assert "配置未变化" in capsys.readouterr().out


def test_apply_changed_config_creates_second_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置变化 → 新世代，父世代指向前一个。"""
    config = _config(tmp_path)
    _stub_artifacts(monkeypatch)
    lock = _lock(tmp_path, monkeypatch)
    try:
        assert main(["--config", str(config), "apply"]) == 0
        _rewrite_value(config, "world")
        assert main(["--config", str(config), "apply"]) == 0
    finally:
        lock.release()

    generations = _generations(tmp_path)
    listed = generations.list()
    assert [generation.id for generation in listed] == [1, 2]
    assert listed[1].parent_id == 1
    assert generations.current() is not None
    assert generations.current().id == 2  # type: ignore[union-attr]


def test_generations_lists_current_and_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """generations 输出当前标记，--json 可直接解析。"""
    config = _config(tmp_path)
    _stub_artifacts(monkeypatch)
    lock = _lock(tmp_path, monkeypatch)
    try:
        assert main(["--config", str(config), "apply"]) == 0
        assert main(["--config", str(config), "generations"]) == 0
        text = capsys.readouterr().out
        assert "*0001" in text or "*0" in text
        assert "0001" in text

        assert main(["--config", str(config), "generations", "--json"]) == 0
    finally:
        lock.release()

    payload = json.loads(capsys.readouterr().out)
    assert payload["current"] == 1
    assert payload["generations"][0]["id"] == 1
    assert payload["generations"][0]["current"] is True


def test_rollback_returns_to_previous_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """整机回滚：current 指回上一个 stable 世代。"""
    config = _config(tmp_path)
    _stub_artifacts(monkeypatch)
    lock = _lock(tmp_path, monkeypatch)
    generations = _generations(tmp_path)
    try:
        assert main(["--config", str(config), "apply"]) == 0
        _rewrite_value(config, "world")
        assert main(["--config", str(config), "apply"]) == 0
        # 手工标记两个世代都 stable（真实系统里由健康闸门落标记）
        for generation in generations.list():
            generations.mark_stable(generation)
        assert main(["--config", str(config), "rollback"]) == 0
    finally:
        lock.release()

    current = generations.current()
    assert current is not None
    assert current.id == 1
    assert "回滚" in capsys.readouterr().out


def test_rollback_service_rebinds_single_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """单服务回滚：产出新世代，只有目标服务回到旧配置。"""
    config = _config(tmp_path)
    _stub_artifacts(monkeypatch)
    lock = _lock(tmp_path, monkeypatch)
    generations = _generations(tmp_path)
    try:
        assert main(["--config", str(config), "apply"]) == 0
        _rewrite_value(config, "world")
        assert main(["--config", str(config), "apply"]) == 0
        for generation in generations.list():
            generations.mark_stable(generation)
        assert main(["--config", str(config), "rollback", "--service", "demo"]) == 0
    finally:
        lock.release()

    current = generations.current()
    assert current is not None
    assert current.id == 3
    payload = json.loads(current.config_path("demo").read_text(encoding="utf-8"))
    assert payload["config"]["value"] == "hello"
    out = capsys.readouterr().out
    assert "已构造世代 0003" in out


def test_rollback_without_previous_stable_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有更早的 stable 世代时回滚报错退出码 2。"""
    config = _config(tmp_path)
    _stub_artifacts(monkeypatch)
    lock = _lock(tmp_path, monkeypatch)
    try:
        assert main(["--config", str(config), "apply"]) == 0
        assert main(["--config", str(config), "rollback"]) == 2
    finally:
        lock.release()


def test_gc_keeps_current_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """gc 至少保留当前世代，--dry-run 不删任何东西。"""
    config = _config(tmp_path)
    _stub_artifacts(monkeypatch)
    lock = _lock(tmp_path, monkeypatch)
    generations = _generations(tmp_path)
    try:
        for index in range(3):
            _rewrite_value(config, f"v{index}")
            assert main(["--config", str(config), "apply"]) == 0
        assert main(["--config", str(config), "gc", "--keep", "1", "--dry-run"]) == 0
        assert len(generations.list()) == 3
        assert main(["--config", str(config), "gc", "--keep", "1"]) == 0
    finally:
        lock.release()

    remaining = [generation.id for generation in generations.list()]
    assert remaining == [3]
    assert "将删除世代 2 个" in capsys.readouterr().out


def test_wait_for_activation_reports_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """apply --wait 遇到 rolled_back 时返回非 0 并说明回滚去向。"""
    from blue.cli import _wait_for_activation  # pyright: ignore[reportPrivateUsage]
    from blue.config import load_desired

    config = _config(tmp_path)
    desired = load_desired(config)
    _stub_artifacts(monkeypatch)
    store = _generations(tmp_path)
    artifacts = ArtifactStore(
        tmp_path / ".blue", project_root=tmp_path, warn_cross_device=False
    )
    generation = store.create(desired, artifacts, reason="test")
    state = StateStore(desired.supervisor.state_dir)
    state.ensure_dirs()
    state.write_state(
        ActualState(
            generation=desired.generation,
            supervisor_pid=os.getpid(),
            started_at=1.0,
            updated_at=2.0,
            generation_id=generation.id,
            activation=ActivationState(
                phase="rolled_back",
                generation_id=generation.id,
                previous_id=1,
                message="未在 1s 内就绪：demo",
                failures=["demo"],
            ),
        )
    )

    code = _wait_for_activation(desired, generation, timeout=5.0)

    assert code == 1
    captured = capsys.readouterr()
    assert "激活失败" in captured.err
    assert "已自动回滚到世代 1" in captured.err


def test_wait_for_activation_returns_zero_when_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """apply --wait 在 stable 时返回 0。"""
    from blue.cli import _wait_for_activation  # pyright: ignore[reportPrivateUsage]
    from blue.config import load_desired

    config = _config(tmp_path)
    desired = load_desired(config)
    _stub_artifacts(monkeypatch)
    store = _generations(tmp_path)
    artifacts = ArtifactStore(
        tmp_path / ".blue", project_root=tmp_path, warn_cross_device=False
    )
    generation = store.create(desired, artifacts, reason="test")
    state = StateStore(desired.supervisor.state_dir)
    state.ensure_dirs()
    state.write_state(
        ActualState(
            generation=desired.generation,
            supervisor_pid=os.getpid(),
            started_at=1.0,
            updated_at=2.0,
            generation_id=generation.id,
            activation=ActivationState(phase="stable", generation_id=generation.id),
        )
    )

    assert _wait_for_activation(desired, generation, timeout=5.0) == 0
    assert "已稳定" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 激活验证：控制面变化自动等待 + 冒烟失败自动回滚
# ---------------------------------------------------------------------------


def _no_notify(desired: DesiredState, state: object) -> bool:
    """不打信号（测试里没有真的 supervisor）。"""
    return False


def _restart_notify(desired: DesiredState, state: object) -> bool:
    """模拟"控制面代码变了，要 execv"。"""
    return True


def _no_verify(
    desired: DesiredState, generation: Generation, *, timeout: float | None, smoke: bool
) -> int:
    """跳过等待与冒烟（对应 _verify_activation 的关键字调用）。"""
    return 0


def _wait_ok(
    desired: DesiredState, generation: Generation, timeout: float | None
) -> int:
    """假装闸门立刻通过（对应 _wait_for_activation 的位置参数调用）。"""
    return 0


def test_apply_waits_automatically_after_control_plane_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """控制面代码变化会 execv，apply 没带 --wait 也要等验证结果。

    否则"命令返回 0 但其实没起来"——自改进最怕的正是这个。
    """
    config = _config(tmp_path)
    _stub_artifacts(monkeypatch)
    lock = _lock(tmp_path, monkeypatch)
    verified: list[int] = []

    def fake_verify(
        desired: DesiredState,
        generation: Generation,
        *,
        timeout: float | None,
        smoke: bool,
    ) -> int:
        verified.append(generation.id)
        return 0

    monkeypatch.setattr("blue.cli._notify_supervisor", _restart_notify)
    monkeypatch.setattr("blue.cli._verify_activation", fake_verify)
    try:
        assert main(["--config", str(config), "apply"]) == 0
    finally:
        lock.release()

    assert verified, "控制面变化时必须自动走验证"
    assert "自动等待验证结果" in capsys.readouterr().out


def test_apply_does_not_wait_when_only_service_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只有服务改动时不强制等待（默认行为保持不变）。"""
    config = _config(tmp_path)
    _stub_artifacts(monkeypatch)
    lock = _lock(tmp_path, monkeypatch)
    verified: list[int] = []

    def fake_verify(
        desired: DesiredState,
        generation: Generation,
        *,
        timeout: float | None,
        smoke: bool,
    ) -> int:
        verified.append(generation.id)
        return 0

    monkeypatch.setattr("blue.cli._notify_supervisor", _no_notify)
    monkeypatch.setattr("blue.cli._verify_activation", fake_verify)
    try:
        assert main(["--config", str(config), "apply"]) == 0
    finally:
        lock.release()

    assert verified == []


def test_apply_rolls_back_when_smoke_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """冒烟失败按激活失败处理：标记 failed、回滚到上一个 stable、返回非 0。"""
    config = _config(tmp_path)
    _stub_artifacts(monkeypatch)
    lock = _lock(tmp_path, monkeypatch)
    desired = load_desired(config)
    store = GenerationStore(desired.supervisor.state_dir)
    artifacts = ArtifactStore(
        desired.supervisor.state_dir, project_root=desired.base_dir
    )
    first = store.create(desired, artifacts, reason="apply")
    store.mark_stable(first)
    store.activate(first)
    _rewrite_value(config, "changed")

    bad = SmokeReport(
        checks=(
            Check(service="demo", name="smoke_tools", ok=False, detail="缺少 demo"),
        )
    )
    monkeypatch.setattr("blue.cli._notify_supervisor", _no_notify)
    monkeypatch.setattr("blue.cli._wait_for_activation", _wait_ok)

    def fake_smoke(desired: DesiredState, generation: Generation) -> SmokeReport:
        return bad

    monkeypatch.setattr("blue.cli._run_smoke", fake_smoke)
    try:
        code = main(["--config", str(config), "apply", "--wait"])
        active = store.current()
    finally:
        lock.release()

    assert code == 1
    assert active is not None and active.id == first.id, "必须回到上一个 stable"
    assert "冒烟失败" in capsys.readouterr().err


def test_apply_passes_smoke_and_records_tool_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """冒烟通过时把观测到的工具数记进 .stable（诊断用），并返回 0。"""
    config = _config(tmp_path)
    _stub_artifacts(monkeypatch)
    lock = _lock(tmp_path, monkeypatch)
    good = SmokeReport(
        checks=(Check(service="demo", name="smoke_tools", ok=True),),
        tools=("now", "get_weather"),
    )
    monkeypatch.setattr("blue.cli._notify_supervisor", _no_notify)
    monkeypatch.setattr("blue.cli._wait_for_activation", _wait_ok)

    def fake_smoke(desired: DesiredState, generation: Generation) -> SmokeReport:
        return good

    monkeypatch.setattr("blue.cli._run_smoke", fake_smoke)
    try:
        assert main(["--config", str(config), "apply", "--wait"]) == 0
        store = GenerationStore(tmp_path / ".blue")
        active = store.current()
        assert active is not None
        marker_text = (active.path / ".stable").read_text(encoding="utf-8")
    finally:
        lock.release()

    assert '"tools": 2' in marker_text
