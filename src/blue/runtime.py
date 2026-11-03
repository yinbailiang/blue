"""服务侧运行时辅助：读运行时配置、连总线、把工具/服务跑到退出。

每个外置服务只依赖本模块与 ``blue`` SDK，不读取核心 ``blue.infd``。
supervisor 通过环境变量注入连接信息，通过 ``service_runtime_config.json``
注入服务配置。
"""

from __future__ import annotations

import asyncio
import base64
import os
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TypeVar

from blue_contracts_tool import EVENTS as TOOL_EVENTS
from infinity_bus import EventDeclaration
from pydantic import BaseModel, ConfigDict, Field

from blue.errors import RuntimeConfigError
from blue.lifecycle import LifecycleService, serve_forever
from blue.transport import RabbitRuntime
from blue.worker import ToolWorker

__all__ = [
    "DEFAULT_BUS_URL",
    "DEFAULT_RUNTIME_CONFIG",
    "PROJECT_ROOT_ENV",
    "Workspace",
    "WorkspaceConfig",
    "build_runtime",
    "bus_url",
    "configure_workspace",
    "load_runtime_config",
    "project_root",
    "registry_ttl",
    "resolve_workspace",
    "resolve_workspace_path",
    "run_service",
    "run_tool",
    "runtime_config_path",
    "service_name",
    "tool_attachment",
    "workspace",
]

DEFAULT_RUNTIME_CONFIG = "service_runtime_config.json"
"""supervisor 生成并放在服务目录下的运行时配置文件名。"""

DEFAULT_BUS_URL = "amqp://guest:guest@127.0.0.1:5672/"
"""未注入 ``BLUE_BUS_URL`` 时的开发默认值。"""

PROJECT_ROOT_ENV = "BLUE_PROJECT_ROOT"
"""supervisor 注入的项目根目录环境变量（``blue.infd`` 所在目录）。"""

ConfigT = TypeVar("ConfigT", bound=BaseModel)


def runtime_config_path(path: str | Path | None = None) -> Path:
    """定位运行时配置：显式参数 > ``BLUE_CONFIG_FILE`` > 当前目录默认文件。"""
    if path is not None:
        return Path(path).expanduser().resolve()
    configured = os.environ.get("BLUE_CONFIG_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / DEFAULT_RUNTIME_CONFIG).resolve()


def load_runtime_config[ConfigT: BaseModel](
    model: type[ConfigT], path: str | Path | None = None
) -> ConfigT:
    """读取并校验运行时配置。

    :raises RuntimeConfigError: 文件不存在、不可读或字段校验失败。
    """
    target = runtime_config_path(path)
    if not target.is_file():
        raise RuntimeConfigError(f"运行时配置不存在：{target}")
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeConfigError(f"无法读取运行时配置：{target}：{error}") from error
    try:
        return model.model_validate_json(text)
    except Exception as error:
        raise RuntimeConfigError(f"运行时配置校验失败：{target}：{error}") from error


def bus_url() -> str:
    """取总线地址（supervisor 注入 ``BLUE_BUS_URL``）。"""
    return os.environ.get("BLUE_BUS_URL", DEFAULT_BUS_URL)


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

    ``ToolWorker`` 会把这个字段摘出来放进 ``tool.result.attachments``，正文里不会出现
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


def registry_ttl(default: float = 15.0) -> float:
    """工具租约时长（秒）：读 supervisor 注入的 ``BLUE_REGISTRY_TTL``。

    环境变量缺失、无法解析或非正数时回退 ``default``，避免把一个坏值变成
    「立刻过期」的租约。
    """
    raw = os.environ.get("BLUE_REGISTRY_TTL")
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


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
    """项目根目录：``blue.infd`` 所在目录，由 supervisor 注入。"""
    configured = os.environ.get(PROJECT_ROOT_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.cwd()


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
    role: str,
    events: Iterable[type[EventDeclaration]],
    url: str | None = None,
) -> RabbitRuntime:
    """创建一个 RabbitMQ 总线成员。

    成员 ID 优先使用 supervisor 注入的 ``BLUE_INSTANCE_ID``，保证重启后
    身份可追踪；手工运行时退化为随机短 ID。

    ``events`` 是该成员需要注册的事件声明；每个进程只注册自己生产/消费的事件。
    """
    instance = os.environ.get("BLUE_INSTANCE_ID") or uuid.uuid4().hex[:8]
    return RabbitRuntime(
        url=url or bus_url(),
        member_id=f"{role}.{instance}",
        events=events,
    )


def run_service(service: LifecycleService) -> int:
    """把生命周期服务跑到收到 SIGINT / SIGTERM。"""
    asyncio.run(serve_forever(service))
    return 0


def run_tool(
    tool: Callable[..., Any],
    name: str,
    *,
    ttl: float | None = None,
) -> int:
    """把工具函数作为独立进程运行。

    :param tool: 带完整类型签名的工具函数。
    :param name: 工具名（用于成员命名与日志）。
    :param ttl: 注册租约；缺省读 ``BLUE_REGISTRY_TTL``，再缺省 15 秒。
    """
    lease = ttl if ttl is not None else registry_ttl()
    worker = ToolWorker(
        runtime=build_runtime(f"tool.{name}", TOOL_EVENTS),
        tool=tool,
        ttl=lease,
    )
    return run_service(worker)
