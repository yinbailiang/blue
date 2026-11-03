"""CLI 测试：config-check、render、status 与无 supervisor 时的 apply。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blue.cli import main

ROOT = Path(__file__).resolve().parents[1]


def _config(root: Path) -> Path:
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
    """config-check 输出服务摘要。"""
    code = main(["--config", str(_config(tmp_path)), "config-check"])
    output = capsys.readouterr().out
    assert code == 0
    assert "services   : 1" in output
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
    """没有 supervisor 时 status 正常退出并显示 stopped。"""
    code = main(["--config", str(_config(tmp_path)), "status"])
    output = capsys.readouterr().out
    assert code == 0
    assert "stopped" in output


def test_apply_without_supervisor_returns_error(tmp_path: Path) -> None:
    """无 supervisor 时 apply 返回配置错误码。"""
    assert main(["--config", str(_config(tmp_path)), "apply"]) == 2


def test_logs_missing_returns_error(tmp_path: Path) -> None:
    """日志不存在时 logs 返回 1。"""
    assert main(["--config", str(_config(tmp_path)), "logs", "demo"]) == 1


def test_real_config_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """仓库自带的 blue.infd 可编译。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    code = main(["--config", str(ROOT / "blue.infd"), "config-check"])
    output = capsys.readouterr().out
    assert code == 0
    assert "agent" in output
    assert "bus" in output
