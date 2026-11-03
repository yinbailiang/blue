"""期望状态加载测试：blue.infd + service.infd + 运行时配置投影。"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from blue.config import ConfigError, load_desired, render_state, write_runtime_configs
from blue.control.desired import digest_package, scan_package


def _service(
    root: Path,
    name: str,
    *,
    template: str = '~ServiceTemplate {\n  value: str = "default"\n}\n',
    metadata: str | None = None,
) -> Path:
    """写入一个最小服务的 service.infd。"""
    service_dir = root / "services" / name
    service_dir.mkdir(parents=True, exist_ok=True)
    meta = metadata or (
        f"service {{\n"
        f'  name = "{name}"\n'
        f'  kind = "tool"\n'
        f'  package = "demo-{name}"\n'
        f'  module = "demo_{name}"\n'
        f'  depends_on = ["bus"]\n'
        f"}}\n"
    )
    (service_dir / "service.infd").write_text(template + "\n" + meta, encoding="utf-8")
    return service_dir


def _config(root: Path, source: str) -> Path:
    path = root / "blue.infd"
    path.write_text(source, encoding="utf-8")
    return path


BUS_META = """service {
  name = "bus"
  kind = "infra"
  package = "demo-bus"
  module = "demo_bus"
  depends_on = []
}
"""

DEMO_META = """service {
  name = "demo"
  kind = "tool"
  package = "demo-demo"
  module = "demo_demo"
  depends_on = ["bus"]
}
"""


def _workspace(tmp_path: Path) -> None:
    """写入 bus 与 demo 两个服务。"""
    _service(tmp_path, "bus", metadata=BUS_META)
    _service(tmp_path, "demo")


def test_load_desired_parses_services_and_supervisor(tmp_path: Path) -> None:
    """顶层键成为服务，supervisor 块独立解析。"""
    _workspace(tmp_path)
    config = _config(
        tmp_path,
        """
!from p"services/bus/service.infd" import ServiceTemplate as BusService
!from p"services/demo/service.infd" import ServiceTemplate as DemoService

supervisor {
  state_dir: str = ".state"
  reconcile_interval: <float, positive> = 0.5
}

bus = BusService(value = "bus")
demo = DemoService(value = "custom")
""",
    )

    desired = load_desired(config)

    assert set(desired.services) == {"bus", "demo"}
    assert desired.services["demo"].config == {"value": "custom"}
    assert desired.services["demo"].meta.name == "demo"
    assert desired.services["demo"].config_path.name == "service_runtime_config.json"
    assert desired.supervisor.reconcile_interval == pytest.approx(0.5)
    assert desired.supervisor.state_dir == (tmp_path / ".state").resolve()
    assert desired.bus_url.startswith("amqp://")


def test_load_desired_authorizes_declared_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式 !env import 的变量被授权并注入配置。"""
    monkeypatch.setenv("BLUE_TEST_VALUE", "from-env")
    _workspace(tmp_path)
    config = _config(
        tmp_path,
        """
!env import BLUE_TEST_VALUE as value
!from p"services/bus/service.infd" import ServiceTemplate as BusService
!from p"services/demo/service.infd" import ServiceTemplate as DemoService

bus = BusService()
demo = DemoService(value = $value)
""",
    )

    desired = load_desired(config)
    assert desired.services["demo"].config["value"] == "from-env"


def test_load_desired_missing_service_directory(tmp_path: Path) -> None:
    """顶层键没有对应服务目录时报错。"""
    _service(tmp_path, "bus", metadata=BUS_META)
    config = _config(
        tmp_path,
        """
!from p"services/bus/service.infd" import ServiceTemplate as BusService
bus = BusService()
ghost = BusService()
""",
    )

    with pytest.raises(ConfigError, match="ghost"):
        load_desired(config)


def test_load_desired_name_mismatch(tmp_path: Path) -> None:
    """blue.infd 顶层键与 service.name 必须一致。"""
    _workspace(tmp_path)
    _service(
        tmp_path,
        "demo",
        metadata="""service {
  name = "other"
  kind = "tool"
  package = "demo-demo"
  module = "demo_demo"
}
""",
    )
    config = _config(
        tmp_path,
        """
!from p"services/bus/service.infd" import ServiceTemplate as BusService
!from p"services/demo/service.infd" import ServiceTemplate as DemoService
bus = BusService()
demo = DemoService()
""",
    )

    with pytest.raises(ConfigError, match="服务名不一致"):
        load_desired(config)


def test_load_desired_rejects_dependency_cycle(tmp_path: Path) -> None:
    """依赖环被拒绝。"""
    _workspace(tmp_path)
    _service(
        tmp_path,
        "a",
        metadata="""service {
  name = "a"
  kind = "tool"
  package = "a"
  module = "a"
  depends_on = ["b"]
}
""",
    )
    _service(
        tmp_path,
        "b",
        metadata="""service {
  name = "b"
  kind = "tool"
  package = "b"
  module = "b"
  depends_on = ["a"]
}
""",
    )
    config = _config(
        tmp_path,
        """
!from p"services/a/service.infd" import ServiceTemplate as A
!from p"services/b/service.infd" import ServiceTemplate as B
a = A()
b = B()
""",
    )

    with pytest.raises(ConfigError, match="环"):
        load_desired(config)


def test_load_desired_rejects_missing_dependency(tmp_path: Path) -> None:
    """依赖不存在的服务时报错。"""
    _workspace(tmp_path)
    config = _config(
        tmp_path,
        """
!from p"services/demo/service.infd" import ServiceTemplate as DemoService
demo = DemoService()
""",
    )

    with pytest.raises(ConfigError, match="bus"):
        load_desired(config)


def test_load_desired_rejects_config_constraint_violation(tmp_path: Path) -> None:
    """服务配置实例违反模板时编译失败。"""
    _workspace(tmp_path)
    config = _config(
        tmp_path,
        """
!from p"services/demo/service.infd" import ServiceTemplate as DemoService
demo = DemoService(unknown = 1)
""",
    )

    with pytest.raises(ConfigError, match="配置编译失败"):
        load_desired(config)


def test_write_runtime_configs_is_atomic_and_private(tmp_path: Path) -> None:
    """运行时配置写入服务目录，权限 0600，内容为配置实例。"""
    _workspace(tmp_path)
    config = _config(
        tmp_path,
        """
!from p"services/bus/service.infd" import ServiceTemplate as BusService
!from p"services/demo/service.infd" import ServiceTemplate as DemoService
bus = BusService()
demo = DemoService(value = "hello")
""",
    )
    desired = load_desired(config)

    changed = write_runtime_configs(desired)

    assert set(changed) == {"bus", "demo"}
    target = desired.services["demo"].config_path
    assert json.loads(target.read_text(encoding="utf-8")) == {"value": "hello"}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    assert write_runtime_configs(desired) == []


def test_render_state_redacts_secrets(tmp_path: Path) -> None:
    """渲染期望状态时默认隐藏密钥字段。"""
    _workspace(tmp_path)
    _service(
        tmp_path,
        "demo",
        template="""~ServiceTemplate(allow_extra = true) {
  value: str = "default"
}
""",
    )
    config = _config(
        tmp_path,
        """
!from p"services/bus/service.infd" import ServiceTemplate as BusService
!from p"services/demo/service.infd" import ServiceTemplate as DemoService
bus = BusService()
demo = DemoService(api_key = "sk-secret")
""",
    )
    desired = load_desired(config)
    rendered = render_state(desired)

    assert rendered["services"]["demo"]["config"]["api_key"] == "***"


def test_load_desired_reports_missing_file(tmp_path: Path) -> None:
    """配置文件不存在时给出明确错误。"""
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load_desired(tmp_path / "missing.infd")


def test_load_desired_uses_proxy_endpoint(tmp_path: Path) -> None:
    """bus 开启代理时，其他服务拿到 proxy_listen 地址。"""
    _service(
        tmp_path,
        "bus",
        template="""~ServiceTemplate {
  url: str = "amqp://guest:guest@127.0.0.1:5672/blue"
  proxy: bool = false
  proxy_listen: str = "amqp://127.0.0.1:5673/blue"
}
""",
        metadata=BUS_META,
    )
    _service(tmp_path, "demo")
    config = _config(
        tmp_path,
        """
!from p"services/bus/service.infd" import ServiceTemplate as BusService
!from p"services/demo/service.infd" import ServiceTemplate as DemoService
bus = BusService(proxy = true)
demo = DemoService()
""",
    )

    desired = load_desired(config)
    assert desired.bus_url == "amqp://127.0.0.1:5673/blue"


def test_generation_changes_with_config(tmp_path: Path) -> None:
    """配置变化会改变 generation。"""
    _workspace(tmp_path)
    config = _config(
        tmp_path,
        """
!from p"services/bus/service.infd" import ServiceTemplate as BusService
!from p"services/demo/service.infd" import ServiceTemplate as DemoService
bus = BusService()
demo = DemoService(value = "a")
""",
    )
    first = load_desired(config)

    config.write_text(
        config.read_text(encoding="utf-8").replace('"a"', '"b"'), encoding="utf-8"
    )
    second = load_desired(config)

    assert first.generation != second.generation


def test_generation_changes_with_package_version(tmp_path: Path) -> None:
    """配置不变、仅服务包版本变化时 generation 与渲染结果也应更新。"""
    _workspace(tmp_path)
    pyproject = tmp_path / "services" / "demo" / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo-demo"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    config = _config(
        tmp_path,
        """
!from p"services/bus/service.infd" import ServiceTemplate as BusService
!from p"services/demo/service.infd" import ServiceTemplate as DemoService
bus = BusService()
demo = DemoService()
""",
    )
    first = load_desired(config)

    pyproject.write_text(
        '[project]\nname = "demo-demo"\nversion = "0.2.0"\n', encoding="utf-8"
    )
    second = load_desired(config)

    assert first.services["demo"].config_hash == second.services["demo"].config_hash
    assert first.services["demo"].package_version == "0.1.0"
    assert second.services["demo"].package_version == "0.2.0"
    assert first.generation != second.generation
    rendered = render_state(second)["services"]
    assert isinstance(rendered, dict)
    assert rendered["demo"]["package_version"] == "0.2.0"


def test_package_version_is_empty_without_pyproject(tmp_path: Path) -> None:
    """缺少 pyproject.toml 时版本为空串，不阻断编译。"""
    _workspace(tmp_path)
    config = _config(
        tmp_path,
        """
!from p"services/bus/service.infd" import ServiceTemplate as BusService
!from p"services/demo/service.infd" import ServiceTemplate as DemoService
bus = BusService()
demo = DemoService()
""",
    )

    desired = load_desired(config)

    assert desired.services["demo"].package_version == ""


def test_generation_changes_with_source_content(tmp_path: Path) -> None:
    """配置与版本都不变、仅源码内容变化时，source_hash 与 generation 也应变。"""
    _workspace(tmp_path)
    service_dir = tmp_path / "services" / "demo"
    (service_dir / "pyproject.toml").write_text(
        '[project]\nname = "demo-demo"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    source = service_dir / "src" / "demo"
    source.mkdir(parents=True)
    module = source / "__init__.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    config = _config(
        tmp_path,
        """
!from p"services/bus/service.infd" import ServiceTemplate as BusService
!from p"services/demo/service.infd" import ServiceTemplate as DemoService
bus = BusService()
demo = DemoService()
""",
    )
    first = load_desired(config)

    module.write_text("VALUE = 2\n", encoding="utf-8")
    second = load_desired(config)

    assert (
        first.services["demo"].package_version
        == second.services["demo"].package_version
    )
    assert first.services["demo"].source_hash != second.services["demo"].source_hash
    assert first.generation != second.generation


def test_digest_package_tracks_content_not_mtime(tmp_path: Path) -> None:
    """指纹随内容/文件集合变化，但忽略仅修改时间的变化。"""
    service_dir = tmp_path / "services" / "demo"
    source = service_dir / "src" / "demo"
    source.mkdir(parents=True)
    (service_dir / "pyproject.toml").write_text(
        '[project]\nname = "demo-demo"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    module = source / "__init__.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")

    first_scan = scan_package(service_dir)
    first = digest_package(service_dir, first_scan)

    future = time.time() + 10
    os.utime(module, (future, future))
    touched_scan = scan_package(service_dir)
    assert touched_scan != first_scan, "stat 签名应反映修改时间变化"
    assert digest_package(service_dir, touched_scan) == first, "内容未变则指纹不变"

    module.write_text("VALUE = 2\n", encoding="utf-8")
    changed = digest_package(service_dir)
    assert changed != first, "内容变化应改变指纹"

    (source / "extra.py").write_text("X = 1\n", encoding="utf-8")
    assert digest_package(service_dir) != changed, "新增文件应改变指纹"


def test_env_var_without_value_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已授权但未设置的环境变量给出明确诊断。"""
    monkeypatch.delenv("BLUE_MISSING_VALUE", raising=False)
    _workspace(tmp_path)
    config = _config(
        tmp_path,
        """
!env import BLUE_MISSING_VALUE as value
!from p"services/demo/service.infd" import ServiceTemplate as DemoService
demo = DemoService(value = $value)
""",
    )

    with pytest.raises(ConfigError, match="BLUE_MISSING_VALUE"):
        load_desired(config)


def test_config_paths_are_absolute(tmp_path: Path) -> None:
    """期望状态中的路径已解析为绝对路径。"""
    _workspace(tmp_path)
    config = _config(
        tmp_path,
        """
!from p"services/bus/service.infd" import ServiceTemplate as BusService
!from p"services/demo/service.infd" import ServiceTemplate as DemoService
bus = BusService()
demo = DemoService()
""",
    )
    desired = load_desired(config)

    assert desired.config_path.is_absolute()
    assert desired.services["demo"].service_dir.is_absolute()
    assert desired.services["demo"].config_path.is_absolute()
    assert os.sep in str(desired.services["demo"].config_path)
