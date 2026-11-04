"""CLI 测试：config-check、render、status 与无 supervisor 时的 apply。"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from blue.cli import main
from blue.control.model import (
    ActualContract,
    ActualService,
    ActualState,
    ServiceStatus,
)
from blue.control.state import StateStore
from blue.runtime import RUNTIME_CONFIG_VERSION

ROOT = Path(__file__).resolve().parents[1]


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
    assert "demo-contracts" in output
    assert "0.1.0" in output


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


def test_apply_without_supervisor_returns_error(tmp_path: Path) -> None:
    """无 supervisor 时 apply 返回配置错误码。"""
    assert main(["--config", str(_config(tmp_path)), "apply"]) == 2


def test_apply_rejects_old_config_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """运行中的 supervisor 配置格式过旧时拒绝热重载。"""
    config = _config(tmp_path)
    store = StateStore(tmp_path / ".blue")
    store.acquire(
        generation="gen-1",
        blue_version="0.1.0",
        config_version=RUNTIME_CONFIG_VERSION - 1,
    )
    try:
        code = main(["--config", str(config), "apply"])
    finally:
        store.release()

    assert code == 2
    assert "blue down && blue up" in capsys.readouterr().err


def test_apply_rejects_blue_version_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """代码版本不同也不允许热重载。"""
    config = _config(tmp_path)
    monkeypatch.setattr("blue.cli._blue_version", lambda: "9.9.9")
    store = StateStore(tmp_path / ".blue")
    store.acquire(
        generation="gen-1", blue_version="0.0.0", config_version=RUNTIME_CONFIG_VERSION
    )
    try:
        code = main(["--config", str(config), "apply"])
    finally:
        store.release()

    assert code == 2
    assert "blue down && blue up" in capsys.readouterr().err


def test_apply_signals_compatible_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """版本一致时正常发送 SIGHUP。"""
    config = _config(tmp_path)
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
