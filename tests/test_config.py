"""期望状态加载测试：blue.infd + service.infd + 运行时配置投影。"""

from __future__ import annotations

import json
import logging
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


def _contract(root: Path, name: str = "tool") -> None:
    """写入一个最小契约包（contract.infd + pyproject.toml + src）。"""
    directory = root / "contracts" / name
    module = directory / "src" / f"blue_contracts_{name}"
    module.mkdir(parents=True, exist_ok=True)
    (module / "__init__.py").write_text("EVENTS = ()\n", encoding="utf-8")
    (directory / "contract.infd").write_text(
        f'contract {{\n  name = "{name}"\n  package = "blue-contracts-{name}"\n'
        f'  module = "blue_contracts_{name}"\n}}\n',
        encoding="utf-8",
    )
    (directory / "pyproject.toml").write_text(
        f'[project]\nname = "blue-contracts-{name}"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )


def _sdk(root: Path, name: str = "tool") -> None:
    """写入一个最小 SDK 运行时包（sdk.infd + pyproject.toml + src）。"""
    directory = root / "sdk" / name
    module = directory / "src" / f"blue_{name}_sdk"
    module.mkdir(parents=True, exist_ok=True)
    (module / "__init__.py").write_text("", encoding="utf-8")
    (directory / "sdk.infd").write_text(
        f'sdk {{\n  name = "{name}"\n  package = "blue-{name}-sdk"\n'
        f'  module = "blue_{name}_sdk"\n}}\n',
        encoding="utf-8",
    )
    (directory / "pyproject.toml").write_text(
        f'[project]\nname = "blue-{name}-sdk"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )


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
    """运行时配置写入服务目录，权限 0600，含业务配置与基础设施两段。"""
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
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["config"] == {"value": "hello"}
    assert payload["infra"]["project_root"] == str(tmp_path)
    assert payload["infra"]["bus_url"] == desired.bus_url
    assert payload["infra"]["state_dir"] == str(tmp_path / ".blue")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    assert write_runtime_configs(desired) == []


def test_runtime_config_projects_contract_modules(tmp_path: Path) -> None:
    """``depends_contracts`` 展开成 ``infra.contracts``，未声明的服务为空。"""
    _workspace(tmp_path)
    _contract(tmp_path)
    _service(
        tmp_path,
        "demo",
        metadata=(
            'service {\n  name = "demo"\n  kind = "tool"\n  package = "demo-demo"\n'
            '  module = "demo_demo"\n  depends_on = ["bus"]\n'
            '  depends_contracts = ["tool"]\n}\n'
        ),
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
    desired = load_desired(config)

    write_runtime_configs(desired)

    demo = json.loads(desired.services["demo"].config_path.read_text(encoding="utf-8"))
    bus = json.loads(desired.services["bus"].config_path.read_text(encoding="utf-8"))
    assert demo["infra"]["contracts"] == {"tool": "blue_contracts_tool"}
    assert bus["infra"]["contracts"] == {}


def test_load_desired_rejects_unknown_sdk(tmp_path: Path) -> None:
    """``depends_sdk`` 指向不存在的 SDK 时直接报错。"""
    _workspace(tmp_path)
    _service(
        tmp_path,
        "demo",
        metadata=(
            'service {\n  name = "demo"\n  kind = "tool"\n  package = "demo-demo"\n'
            '  module = "demo_demo"\n  depends_on = ["bus"]\n'
            '  depends_sdk = ["nope"]\n}\n'
        ),
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

    with pytest.raises(ConfigError, match="不存在的 SDK"):
        load_desired(config)


def test_sdk_metadata_enters_desired_state(tmp_path: Path) -> None:
    """``sdk/<name>/sdk.infd`` 的元数据进入期望状态，按 SDK 名记录。"""
    _workspace(tmp_path)
    _sdk(tmp_path)
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

    package = desired.sdk.packages["tool"]
    assert package.meta.package == "blue-tool-sdk"
    assert package.meta.module == "blue_tool_sdk"
    assert package.package_version == "0.1.0"
    assert len(package.source_hash) == 64


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


def test_digest_package_covers_non_python_assets(tmp_path: Path) -> None:
    """包内静态资源（如 webui 的 web/**）必须进指纹。

    wheel 会带上 ``source-include`` 声明的非 .py 文件；指纹若只覆盖 .py，
    就会出现「改了前端资源、指纹不变、apply 认为无事可做」，资源永远不生效。
    """
    service_dir = tmp_path / "services" / "web"
    source = service_dir / "src" / "demo_web"
    source.mkdir(parents=True)
    (service_dir / "pyproject.toml").write_text(
        '[project]\nname = "demo-web"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (source / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    asset = source / "web" / "app.js"
    asset.parent.mkdir()
    asset.write_text("console.log(1);\n", encoding="utf-8")

    before = digest_package(service_dir)
    asset.write_text("console.log(2);\n", encoding="utf-8")
    assert digest_package(service_dir) != before, "静态资源内容变化应改变指纹"

    removed = digest_package(service_dir)
    asset.unlink()
    assert digest_package(service_dir) != removed, "静态资源被删也应改变指纹"


def test_digest_package_ignores_caches(tmp_path: Path) -> None:
    """缓存与字节码不进指纹，否则跑完测试就会触发无意义的重建。"""
    service_dir = tmp_path / "services" / "demo"
    source = service_dir / "src" / "demo"
    source.mkdir(parents=True)
    (service_dir / "pyproject.toml").write_text(
        '[project]\nname = "demo-demo"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (source / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    baseline = digest_package(service_dir)

    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "__init__.cpython-314.pyc").write_bytes(b"\x00\x01")
    (source / "stray.pyc").write_bytes(b"\x00\x02")
    (source / ".pytest_cache").mkdir()
    (source / ".pytest_cache" / "README.md").write_text("x", encoding="utf-8")

    assert digest_package(service_dir) == baseline, "缓存文件不应改变指纹"
    assert all(
        "__pycache__" not in name and not name.endswith(".pyc")
        for name, _size, _mtime in scan_package(service_dir)
    )


def test_generation_changes_with_static_asset(tmp_path: Path) -> None:
    """配置、版本、.py 都不变、只有静态资源变化时，generation 也要变。"""
    _workspace(tmp_path)
    service_dir = tmp_path / "services" / "demo"
    (service_dir / "pyproject.toml").write_text(
        '[project]\nname = "demo-demo"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    source = service_dir / "src" / "demo_web"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    asset = source / "style.css"
    asset.write_text("body { color: red }\n", encoding="utf-8")
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

    asset.write_text("body { color: blue }\n", encoding="utf-8")
    second = load_desired(config)

    assert (
        first.services["demo"].package_version
        == second.services["demo"].package_version
    )
    assert first.services["demo"].source_hash != second.services["demo"].source_hash
    assert first.generation != second.generation


NULLABLE_TEMPLATE = "~ServiceTemplate {\n  value: <str?> = null\n}\n"


def _secret_workspace(tmp_path: Path) -> None:
    """bus + 使用可空 value 的 demo 服务，便于验证 !file 导入。"""
    _service(tmp_path, "bus", metadata=BUS_META)
    _service(tmp_path, "demo", template=NULLABLE_TEMPLATE)


SECRET_CONFIG = """
!file p"secrets.toml" as toml import .demo.api_key as demo_key

!from p"services/bus/service.infd" import ServiceTemplate as BusService
!from p"services/demo/service.infd" import ServiceTemplate as DemoService
bus = BusService()
demo = DemoService(value = $demo_key)
"""


def test_load_desired_imports_secrets_file(tmp_path: Path) -> None:
    """secrets.toml 中的值经 !file 进入服务配置。"""
    _secret_workspace(tmp_path)
    (tmp_path / "secrets.toml").write_text(
        '[demo]\napi_key = "sk-secret"\n', encoding="utf-8"
    )
    config = _config(tmp_path, SECRET_CONFIG)

    desired = load_desired(config)

    assert desired.services["demo"].config["value"] == "sk-secret"


def test_load_desired_tolerates_missing_secrets_file(tmp_path: Path) -> None:
    """secrets.toml 缺失时取值为 null（noexist），编译仍成功。"""
    _secret_workspace(tmp_path)
    config = _config(tmp_path, SECRET_CONFIG)

    desired = load_desired(config)

    assert desired.services["demo"].config["value"] is None


def test_load_desired_denies_non_secret_file_import(tmp_path: Path) -> None:
    """沙盒只授权密钥文件，其它工作区文件的 !file 导入直接报错。

    与「密钥文件缺失」不同：越权导入是错误（fail loud），缺失只是 warning。
    """
    _secret_workspace(tmp_path)
    (tmp_path / "other.toml").write_text(
        '[demo]\napi_key = "leaked"\n', encoding="utf-8"
    )
    config = _config(
        tmp_path,
        SECRET_CONFIG.replace('p"secrets.toml"', 'p"other.toml"'),
    )

    with pytest.raises(ConfigError, match="access_denied"):
        load_desired(config)


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


def test_unknown_supervisor_key_warns_but_does_not_fail(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """未知 supervisor 键：放行 + 告警。

    这是升级路径的硬需求：`blue.infd` 由**正在跑的那个**调谐器解析。若一律拒绝
    未知键，加一个新配置项就会死锁——旧代码热重载失败、又因"配置编译失败"放弃
    execv 自重启，只能人工 `blue down && blue up`（实测卡过）。
    """
    from blue.control.desired import _load_supervisor  # pyright: ignore[reportPrivateUsage]

    with caplog.at_level(logging.WARNING):
        settings = _load_supervisor(
            {"state_dir": ".blue", "future_knob": 1, "smoke_enabled": False}, tmp_path
        )

    assert settings.smoke_enabled is False
    assert settings.model_extra == {"future_knob": 1}
    assert "future_knob" in caplog.text


def test_bad_supervisor_value_still_fails(tmp_path: Path) -> None:
    """值本身非法仍然要报错——放行未知键不等于放行错值。"""
    from blue.control.desired import _load_supervisor  # pyright: ignore[reportPrivateUsage]
    from blue.errors import ConfigError

    with pytest.raises(ConfigError):
        _load_supervisor({"activation_timeout": -1}, tmp_path)
