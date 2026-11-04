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
    "browser",
    "browser_ops",
    "bus",
    "chat",
    "clock",
    "edit_file",
    "echo",
    "inspect",
    "mcp_bridge",
    "mcp_server",
    "read_file",
    "registry",
    "session",
    "shell",
    "text_stats",
    "view_image",
    "weather",
    "webui",
    "write_file",
)
MANAGED = {
    "agent",
    "browser",
    "browser_ops",
    "bus",
    "clock",
    "edit_file",
    "read_file",
    "registry",
    "session",
    "shell",
    "text_stats",
    "view_image",
    "weather",
    "webui",
    "write_file",
}
CONTRACTS = ("session", "tool")
EXPECTED_CONTRACTS: dict[str, set[str]] = {
    "agent": {"tool", "session"},
    "browser": {"tool"},
    "browser_ops": {"tool"},
    "bus": set(),
    "chat": {"session"},
    "clock": {"tool"},
    "edit_file": {"tool"},
    "echo": {"tool"},
    "inspect": {"tool", "session"},
    "mcp_bridge": {"tool"},
    "mcp_server": {"tool"},
    "read_file": {"tool"},
    "registry": {"tool"},
    "session": {"session"},
    "shell": {"tool"},
    "text_stats": {"tool"},
    "view_image": {"tool"},
    "weather": {"tool"},
    "webui": {"tool", "session"},
    "write_file": {"tool"},
}


def _sandbox() -> SandboxConfig:
    services = (ROOT / "services").resolve()
    contracts = (ROOT / "contracts").resolve()
    return SandboxConfig(
        allow_templates=[str(services / "**"), str(contracts / "**")],
        allow_files=[str(services / "**"), str(contracts / "**")],
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
        if name in {
            "agent",
            "browser",
            "browser_ops",
            "bus",
            "edit_file",
            "read_file",
            "session",
            "shell",
            "view_image",
            "weather",
            "webui",
            "write_file",
        }:
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


@pytest.mark.parametrize("name", CONTRACTS)
def test_contract_directory_is_complete(name: str) -> None:
    """每个契约包都有 pyproject 与合法 contract.infd。"""
    contract_dir = ROOT / "contracts" / name
    pyproject = contract_dir / "pyproject.toml"
    manifest = contract_dir / "contract.infd"
    assert pyproject.is_file(), f"{name} 缺少 pyproject.toml"
    assert manifest.is_file(), f"{name} 缺少 contract.infd"

    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    result = load(manifest, sandbox=_sandbox())
    assert not result.has_errors, result.diagnostics
    raw = result.value.get("contract")
    assert isinstance(raw, dict)
    meta = cast(dict[str, Any], raw)
    assert meta["name"] == name
    assert meta["package"] == project["project"]["name"]


def test_desired_state_loads_contracts_with_fingerprints() -> None:
    """契约包进入期望状态，并带有源码指纹。"""
    desired = load_desired(ROOT / "blue.infd")

    assert set(desired.contracts) == set(CONTRACTS)
    for contract in desired.contracts.values():
        assert contract.package_version
        assert len(contract.source_hash) == 64


def test_service_metadata_declares_contract_dependencies() -> None:
    """每个服务的契约依赖与预期一致。"""
    for name, expected in EXPECTED_CONTRACTS.items():
        manifest = ROOT / "services" / name / "service.infd"
        result = load(manifest, sandbox=_sandbox())
        raw = result.value.get("service")
        assert isinstance(raw, dict)
        meta = cast(dict[str, Any], raw)
        assert set(meta.get("depends_contracts", [])) == expected


def test_service_contract_dependencies_exist() -> None:
    """服务声明的契约依赖都能在期望状态中找到。"""
    desired = load_desired(ROOT / "blue.infd")

    for name, service in desired.services.items():
        for contract in service.meta.depends_contracts:
            assert contract in desired.contracts, f"{name} 依赖不存在契约 {contract}"
