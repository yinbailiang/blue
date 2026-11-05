"""服务侧运行时辅助：读运行时配置、连总线、把工具/服务跑到退出。

每个外置服务只依赖本模块与 ``blue`` SDK，不读取核心 ``blue.infd``。
supervisor 通过服务目录下的 ``service_runtime_config.json`` 注入配置：
``config`` 段是该服务的业务配置，``infra`` 段是所有服务共享的基础设施参数。
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from infinity_bus import EventDeclaration
from pydantic import BaseModel, ConfigDict, Field

from blue.errors import RuntimeConfigError
from blue.lifecycle import LifecycleService, serve_forever
from blue.transport import RabbitRuntime

__all__ = [
    "CONFIG_SECTION",
    "DEFAULT_BUS_URL",
    "DEFAULT_RUNTIME_CONFIG",
    "INFRA_SECTION",
    "RUNTIME_CONFIG_VERSION",
    "InfraConfig",
    "Workspace",
    "WorkspaceConfig",
    "build_runtime",
    "bus_url",
    "configure_logging",
    "configure_workspace",
    "contract_modules",
    "load_contract_events",
    "load_infra_config",
    "load_runtime_config",
    "project_root",
    "registry_ttl",
    "reset_infra_config",
    "resolve_workspace",
    "resolve_workspace_path",
    "run_service",
    "runtime_config_path",
    "service_name",
    "state_dir",
    "tool_attachment",
    "workspace",
]

logger = logging.getLogger(__name__)

DEFAULT_RUNTIME_CONFIG = "service_runtime_config.json"
"""supervisor 生成并放在服务目录下的运行时配置文件名。"""

CONFIG_SECTION = "config"
"""运行时配置文件中「服务自身配置」的段名。"""

INFRA_SECTION = "infra"
"""运行时配置文件中「基础设施参数」的段名（所有服务共享）。"""

DEFAULT_BUS_URL = "amqp://guest:guest@127.0.0.1:5672/"
"""缺少基础设施配置时的开发默认值。"""

RUNTIME_CONFIG_VERSION = 2
"""运行时配置格式版本。

``service_runtime_config.json`` 的段结构发生不兼容变化时递增。supervisor 启动
时把版本写进锁文件，``blue apply`` 据此阻止新旧代码混跑。
"""

ConfigT = TypeVar("ConfigT", bound=BaseModel)


def runtime_config_path(path: str | Path | None = None) -> Path:
    """定位运行时配置：显式参数 > ``BLUE_CONFIG_FILE`` > 当前目录默认文件。"""
    if path is not None:
        return Path(path).expanduser().resolve()
    configured = os.environ.get("BLUE_CONFIG_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / DEFAULT_RUNTIME_CONFIG).resolve()


def _read_runtime_config(path: str | Path | None = None) -> dict[str, Any]:
    """读取运行时配置文件的完整内容，不做字段校验。"""
    target = runtime_config_path(path)
    if not target.is_file():
        raise RuntimeConfigError(f"运行时配置不存在：{target}")
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeConfigError(f"无法读取运行时配置：{target}：{error}") from error
    try:
        data = json.loads(text)
    except ValueError as error:
        raise RuntimeConfigError(
            f"运行时配置不是合法 JSON：{target}：{error}"
        ) from error
    if not isinstance(data, dict):
        raise RuntimeConfigError(f"运行时配置必须是 JSON 对象：{target}")
    return cast("dict[str, Any]", data)


def load_runtime_config[ConfigT: BaseModel](
    model: type[ConfigT], path: str | Path | None = None
) -> ConfigT:
    """读取并校验服务自身的运行时配置（文件中的 ``config`` 段）。

    基础设施参数在同一个文件的 ``infra`` 段里，由
    :func:`load_infra_config` 读取——服务只需要定位一份配置文件。

    :raises RuntimeConfigError: 文件不存在、不可读或字段校验失败。
    """
    payload = _read_runtime_config(path)
    section = payload.get(CONFIG_SECTION, payload)
    try:
        return model.model_validate(section)
    except Exception as error:
        raise RuntimeConfigError(
            f"运行时配置校验失败：{runtime_config_path(path)}：{error}"
        ) from error


class InfraConfig(BaseModel):
    """supervisor 下发的基础设施参数（所有服务共享）。

    与服务自身配置同处一个配置文件（``infra`` 段），服务只需定位一份文件。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_root: Path = Field(description="项目根，即 ``blue.infd`` 所在目录")
    bus_url: str = Field(description="AMQP 连接地址")
    state_dir: Path = Field(description="supervisor 状态与日志目录")
    contracts: dict[str, str] = Field(
        default_factory=dict,
        description="契约名 → 事件模块；由 supervisor 按 depends_contracts 注入",
    )
    registry_ttl: float = Field(default=15.0, gt=0, description="工具租约时长（秒）")
    registry_sweep_interval: float = Field(
        default=2.0, gt=0, description="工具租约巡检间隔（秒）"
    )


_infra_cache: InfraConfig | None = None


def _default_infra() -> InfraConfig:
    """手工运行（无 supervisor 下发）时的基础设施默认值。"""
    return InfraConfig(
        project_root=Path.cwd(),
        bus_url=DEFAULT_BUS_URL,
        state_dir=Path(".blue"),
    )


def _running_under_supervisor() -> bool:
    """当前进程是否由 supervisor 启动（配置由 supervisor 下发）。"""
    return bool(
        os.environ.get("BLUE_CONFIG_FILE") or os.environ.get("BLUE_SERVICE_NAME")
    )


def load_infra_config(path: str | Path | None = None) -> InfraConfig:
    """读取 supervisor 下发的基础设施参数（配置文件中的 ``infra`` 段）。

    单进程手工运行且没有配置文件时回退默认值；由 supervisor 启动时，配置缺失
    或缺少 ``infra`` 段属于生成缺陷，直接报错——静默回退到默认总线地址会把
    配置问题伪装成连接失败。

    首次读取后缓存，服务进程生命周期内不再重复解析。

    :param path: 显式文件路径；给定时不读也不写缓存。
    :raises RuntimeConfigError: 配置缺失、缺少 ``infra`` 段或字段校验失败。
    """
    global _infra_cache
    if path is None and _infra_cache is not None:
        return _infra_cache
    try:
        payload = _read_runtime_config(path)
    except RuntimeConfigError as error:
        if _running_under_supervisor():
            raise RuntimeConfigError(
                f"supervisor 下发的运行时配置不可用：{error}"
            ) from error
        config = _default_infra()
        if path is None:
            _infra_cache = config
        return config
    section = payload.get(INFRA_SECTION)
    if section is None:
        message = f"{runtime_config_path(path)} 缺少 {INFRA_SECTION!r} 段"
        if _running_under_supervisor():
            raise RuntimeConfigError(
                f"{message}；请执行 blue down && blue up 重新生成运行时配置"
            )
        logger.warning("%s，基础设施参数回退默认值", message)
        config = _default_infra()
    else:
        try:
            config = InfraConfig.model_validate(section)
        except Exception as error:
            raise RuntimeConfigError(
                f"基础设施配置校验失败：{runtime_config_path(path)}：{error}"
            ) from error
    if path is None:
        _infra_cache = config
    return config


def reset_infra_config() -> None:
    """清空基础设施配置缓存（测试与配置热重载用）。"""
    global _infra_cache
    _infra_cache = None


def bus_url() -> str:
    """取总线地址（supervisor 下发的基础设施配置）。"""
    return load_infra_config().bus_url


def state_dir() -> Path:
    """取 supervisor 状态目录（缺省 ``cwd/.blue``）。"""
    return load_infra_config().state_dir.expanduser().resolve()


def service_name(default: str = "service") -> str:
    """取当前服务名（supervisor 注入 ``BLUE_SERVICE_NAME``）。"""
    return os.environ.get("BLUE_SERVICE_NAME", default)


def tool_attachment(
    data: bytes, mime: str = "image/png", *, name: str | None = None
) -> dict[str, str | None]:
    """构造工具返回值里的 ``attachments`` 元素。

    工具把自己的图片/文件放进返回对象的 ``attachments`` 列表：

    ```python
    class Shot(BaseModel):
        text: str
        attachments: list[dict[str, str]]

    def screenshot() -> Shot:
        return Shot(text="截图如下", attachments=[tool_attachment(png, "image/png")])
    ```

    ``ToolWorker``（``blue_tool_sdk``）会把这个字段摘出来放进
    ``tool.result.attachments``，正文里不会出现
    base64；agent 再把它转成内容寻址的会话附件并交给模型。

    :param data: 原始字节
    :param mime: MIME 类型
    :param name: 可选文件名
    :return: ``{"mime": ..., "data": <base64>, "name": ...}``
    """
    payload: dict[str, str | None] = {
        "mime": mime,
        "data": base64.b64encode(data).decode("ascii"),
    }
    if name is not None:
        payload["name"] = name
    return payload


def registry_ttl() -> float:
    """工具租约时长（秒）：取 supervisor 下发的配置。"""
    return load_infra_config().registry_ttl


# supervisor 以服务目录作为进程 cwd（见 control/runner.py），若相对路径直接按
# cwd 解析会落到 services/<name>/ 下。文件类工具统一锚定项目根，与 shell 语义一致。
class WorkspaceConfig(BaseModel):
    """文件类工具的工作目录配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workdir: str | None = Field(
        default=None,
        description="相对路径的锚点；缺省为项目根，绝对路径不受影响",
    )


def project_root() -> Path:
    """项目根目录：``blue.infd`` 所在目录，由 supervisor 下发。"""
    return load_infra_config().project_root.expanduser().resolve()


def resolve_workspace(config: WorkspaceConfig) -> Path:
    """把配置解析为绝对工作目录。

    优先级：显式 ``workdir`` > 项目根 > 进程当前目录。
    """
    root = project_root()
    if config.workdir is None:
        return root
    configured = Path(config.workdir).expanduser()
    if configured.is_absolute():
        return configured.resolve()
    return (root / configured).resolve()


def resolve_workspace_path(path: str, workdir: Path) -> Path:
    """把用户给出的路径解析为绝对路径：绝对路径原样，相对路径按 ``workdir``。"""
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (workdir / candidate).resolve()


class Workspace:
    """工具进程内的路径解析上下文。"""

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir

    @property
    def workdir(self) -> Path:
        """相对路径的锚点目录。"""
        return self._workdir

    def resolve(self, path: str) -> Path:
        """把用户给出的路径解析为绝对路径。

        :raises ValueError: ``path`` 为空。
        """
        if not path.strip():
            raise ValueError("path 不能为空")
        return resolve_workspace_path(path, self._workdir)


_workspace: Workspace | None = None
"""工具进程级路径上下文；由 :func:`configure_workspace` 注入，未注入时锚定项目根。"""


def configure_workspace(config: WorkspaceConfig) -> None:
    """设置本进程后续工具调用的路径锚点。"""
    global _workspace
    _workspace = Workspace(resolve_workspace(config))


def workspace() -> Workspace:
    """当前进程的路径上下文；未配置时锚定项目根。"""
    if _workspace is None:
        return Workspace(project_root())
    return _workspace


def build_runtime(
    role: str | None = None,
    events: Iterable[type[EventDeclaration]] | None = None,
    url: str | None = None,
    strategy: Literal["restart", "offline"] = "offline",
) -> RabbitRuntime:
    """创建一个 RabbitMQ 总线成员。

    成员 ID 用服务名（supervisor 注入的 ``BLUE_SERVICE_NAME``）——它在服务
    生命周期内稳定，因此 fanout 队列名可预测、重启后复用；用随机的
    ``BLUE_INSTANCE_ID`` 会让队列名每次启动都变，durable 队列永远复用不上，
    旧队列无人消费却仍绑定在 fanout 上，无限积压。

    ``strategy`` 决定停机时的队列语义，默认 ``offline``：``join()`` 会解绑
    队列，退出后不再接收广播；``restart`` 保留绑定与积压，重启后补投。

    ``role`` 缺省取服务名；``events`` 缺省按服务配置里的 ``depends_contracts``
    自动扫描各契约包的 ``EVENTS``——服务代码不再手写事件列表。传入 ``events``
    时显式覆盖自动扫描结果（单测、特殊进程用）。

    :raises RuntimeConfigError: 配置里的契约模块缺失或没有导出 ``EVENTS``。
    """
    member = service_name()
    declarations = tuple(events) if events is not None else load_contract_events()
    return RabbitRuntime(
        url=url or bus_url(),
        member_id=f"{role or member}.{member}",
        events=declarations,
        strategy=strategy,
    )


def contract_modules() -> dict[str, str]:
    """当前服务依赖的契约：``{契约名: 事件模块}``。

    唯一真相是 ``service.infd`` 的 ``depends_contracts``；supervisor 把它展开成
    模块名写进运行时配置的 ``infra.contracts``，服务侧只负责导入。
    """
    return dict(load_infra_config().contracts)


def load_contract_events(
    modules: Mapping[str, str] | None = None,
) -> tuple[type[EventDeclaration], ...]:
    """导入契约模块并汇总它们的 ``EVENTS``。

    手工运行（没有 supervisor 下发的配置）时退化为扫描已安装的
    ``blue-contracts-*`` 发行包：开发时 `uv run blue-service-echo` 仍能直接工作。
    由 supervisor 启动时**不**做这个兜底——多注册事件会掩盖
    ``depends_contracts`` 漏配。

    :param modules: 显式指定 ``{契约名: 模块名}``；缺省读运行时配置。
    :raises RuntimeConfigError: 模块缺失、没有导出 ``EVENTS`` 或事件重名冲突。
    """
    configured = contract_modules() if modules is None else dict(modules)
    if not configured and not _running_under_supervisor():
        configured = _installed_contract_modules()

    events: dict[str, type[EventDeclaration]] = {}
    for contract, module in sorted(configured.items()):
        try:
            imported = importlib.import_module(module)
        except ModuleNotFoundError as error:
            raise RuntimeConfigError(
                f"契约 {contract!r} 的模块 {module!r} 不可用：检查"
                " service.infd 的 depends_contracts 与产物安装"
            ) from error
        declared = getattr(imported, "EVENTS", None)
        if not isinstance(declared, Iterable) or isinstance(declared, str | bytes):
            raise RuntimeConfigError(f"契约模块 {module!r} 没有导出 EVENTS")
        found = False
        for event in cast(Iterable[object], declared):
            if not (isinstance(event, type) and issubclass(event, EventDeclaration)):
                raise RuntimeConfigError(
                    f"契约模块 {module!r} 的 EVENTS 含非事件声明：{event!r}"
                )
            found = True
            existing = events.get(event.name)
            if existing is not None and existing is not event:
                raise RuntimeConfigError(
                    f"契约事件重名：{event.name!r} 同时来自 "
                    f"{existing.__module__} 与 {event.__module__}"
                )
            events[event.name] = event
        if not found:
            raise RuntimeConfigError(f"契约模块 {module!r} 的 EVENTS 为空")
    return tuple(events.values())


def _installed_contract_modules() -> dict[str, str]:
    """扫描已安装的 ``blue-contracts-*`` 发行包（仅手工运行时的兜底）。"""
    modules: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        package = distribution.metadata["Name"] or ""
        if not package.startswith("blue-contracts-"):
            continue
        module = package.replace("-", "_")
        if importlib.util.find_spec(module) is None:
            continue
        modules[package.removeprefix("blue-contracts-")] = module
    return modules


def configure_logging() -> None:
    """按 ``BLUE_LOG_LEVEL``（默认 INFO）把日志打到 stderr。

    服务进程的 stderr 由 supervisor 收集成 ``.blue/logs/<name>.log``。此前没有
    任何地方配置 logging，root logger 停在 WARNING，于是所有 ``logger.info``
    （服务上下线、会话回收…）都被丢掉了——后台在悄悄干活，前台却没有任何痕迹。
    """
    level_name = os.environ.get("BLUE_LOG_LEVEL", "INFO").strip().upper()
    level = logging.getLevelNamesMapping().get(level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stderr,
    )


def run_service(service: LifecycleService) -> int:
    """把生命周期服务跑到收到 SIGINT / SIGTERM。"""
    configure_logging()
    asyncio.run(serve_forever(service))
    return 0
