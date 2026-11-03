"""服务清单测试：目录、pyproject、service.infd 与运行时配置契约。"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
from infinity_data import SandboxConfig, load

from blue.config import load_desired

ROOT = Path(__file__).resolve().parents[1]
SERVICES = (
    "agent",
    "bus",
    "chat",
    "clock",
    "edit_file",
    "echo",
    "inspect",
    "read_file",
    "session",
    "shell",
    "text_stats",
    "weather",
    "webui",
    "write_file",
)
MANAGED = {
    "agent",
    "bus",
    "clock",
    "edit_file",
    "read_file",
    "session",
    "shell",
    "text_stats",
    "weather",
    "webui",
    "write_file",
}


def _sandbox() -> SandboxConfig:
    services = (ROOT / "services").resolve()
    return SandboxConfig(
        allow_templates=[str(services / "**")],
        allow_files=[str(services / "**")],
    )


@pytest.mark.parametrize("name", SERVICES)
def test_service_directory_is_complete(name: str) -> None:
    """每个服务都有 pyproject 与合法 service.infd。"""
    service_dir = ROOT / "services" / name
    pyproject = service_dir / "pyproject.toml"
    manifest = service_dir / "service.infd"
    assert pyproject.is_file(), f"{name} 缺少 pyproject.toml"
    assert manifest.is_file(), f"{name} 缺少 service.infd"

    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    result = load(manifest, sandbox=_sandbox())
    assert not result.has_errors, result.diagnostics
    raw = result.value.get("service")
    assert isinstance(raw, dict)
    meta = cast(dict[str, Any], raw)
    assert meta["name"] == name
    assert meta["package"] == project["project"]["name"]


def test_blue_infd_compiles_and_matches_service_packages() -> None:
    """核心配置能编译，且每个服务的配置实例能通过其运行时模型。"""
    desired = load_desired(ROOT / "blue.infd")

    assert set(desired.services) == MANAGED
    for name, service in desired.services.items():
        module = importlib.import_module(f"blue_service_{name}")
        if name in {"agent", "bus", "session", "shell", "weather", "webui"}:
            config_model = module.CONFIG_MODEL
            config_model.model_validate(service.config)
        else:
            assert service.config == {}


def test_service_metadata_depends_on_existing_services() -> None:
    """除 bus 外，所有被管理服务都依赖 bus。"""
    for name in MANAGED - {"bus"}:
        manifest = ROOT / "services" / name / "service.infd"
        result = load(manifest, sandbox=_sandbox())
        raw = result.value.get("service")
        assert isinstance(raw, dict)
        meta = cast(dict[str, Any], raw)
        assert "bus" in meta["depends_on"]
