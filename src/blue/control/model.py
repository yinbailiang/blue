"""控制面数据模型：通用服务元数据、期望状态、实际状态与 supervisor 配置。"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ActualService",
    "ActualState",
    "DesiredService",
    "DesiredState",
    "ReadinessProbe",
    "RestartPolicy",
    "ServiceKind",
    "ServiceMeta",
    "ServiceStatus",
    "SupervisorSettings",
]

ServiceKind = Literal["infra", "agent", "session", "tool", "ui"]
RestartPolicy = Literal["always", "on-failure", "never"]
ReadinessProbe = Literal["process", "tcp", "none"]


class ServiceMeta(BaseModel):
    """`service.infd` 中的通用系统元数据（与具体服务配置无关）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="服务名 / 目录名")
    kind: ServiceKind = Field(description="服务类别")
    package: str = Field(description="uv workspace 包名")
    module: str = Field(description="Python 模块名")
    command: tuple[str, ...] = Field(
        default=(), description="自定义启动命令；为空时使用 uv run"
    )
    depends_on: tuple[str, ...] = Field(default=(), description="依赖的服务名")
    restart: RestartPolicy = Field(default="on-failure", description="重启策略")
    stop_timeout: float = Field(default=10.0, gt=0, description="优雅停止超时（秒）")
    startup_timeout: float = Field(default=30.0, gt=0, description="启动就绪超时（秒）")
    readiness: ReadinessProbe = Field(default="process", description="就绪探针")
    ready_tcp: str | None = Field(
        default=None, description="TCP 就绪目标（host:port）；缺省时 bus 自动推导"
    )
    env: dict[str, str] = Field(default_factory=dict, description="附加环境变量")


class SupervisorSettings(BaseModel):
    """`blue.infd` 中 `supervisor` 块的配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state_dir: Path = Field(default=Path(".blue"), description="状态/日志/锁目录")
    reconcile_interval: float = Field(
        default=2.0, gt=0, description="调谐轮询间隔（秒）"
    )
    shutdown_timeout: float = Field(default=10.0, gt=0, description="整体停机超时")
    log_max_bytes: int = Field(default=10 * 1024 * 1024, gt=0, description="单日志上限")
    log_backups: int = Field(default=3, ge=0, le=20, description="日志轮转保留数")
    restart_backoff: float = Field(default=0.5, gt=0, description="重启退避基数（秒）")
    restart_backoff_max: float = Field(
        default=30.0, gt=0, description="重启退避上限（秒）"
    )
    restart_max_attempts: int = Field(
        default=10, ge=1, description="连续失败多少次后进入 CrashLoopBackOff"
    )
    stable_after: float = Field(
        default=60.0, gt=0, description="稳定运行多久后重置失败计数（秒）"
    )
    registry_ttl: float = Field(default=15.0, gt=0, description="工具租约时长")
    registry_sweep_interval: float = Field(
        default=2.0, gt=0, description="工具租约巡检间隔"
    )
    uv_frozen: bool = Field(default=True, description="uv run 是否使用 --frozen")


class DesiredService(BaseModel):
    """一个期望运行的服务：元数据 + 配置实例 + 落盘位置。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    meta: ServiceMeta
    config: dict[str, Any] = Field(default_factory=dict)
    config_hash: str
    package_version: str = ""
    """服务包在 ``pyproject.toml`` 中声明的版本；缺失时为空串。"""
    source_hash: str = ""
    """服务包内容指纹（版本声明 + 源码内容），用于判断是否需要重新加载。"""
    service_dir: Path
    config_path: Path


class DesiredState(BaseModel):
    """一次配置编译得到的完整期望状态。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    generation: str
    config_path: Path
    base_dir: Path
    supervisor: SupervisorSettings
    services: dict[str, DesiredService]
    bus_url: str
    bus_transport: Literal["rabbit", "memory"]


class ServiceStatus(StrEnum):
    """服务实际状态。"""

    PENDING = "Pending"
    STARTING = "Starting"
    RUNNING = "Running"
    READY = "Ready"
    BACKOFF = "BackOff"
    CRASH_LOOP = "CrashLoopBackOff"
    STOPPING = "Stopping"
    STOPPED = "Stopped"
    FAILED = "Failed"


class ActualService(BaseModel):
    """一个服务的实际运行快照。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    kind: ServiceKind
    status: ServiceStatus = ServiceStatus.PENDING
    pid: int | None = None
    pgid: int | None = None
    instance_id: str | None = None
    restarts: int = 0
    consecutive_failures: int = 0
    last_exit_code: int | None = None
    last_error: str | None = None
    started_at: float | None = None
    ready_at: float | None = None
    config_hash: str = ""
    package_version: str = ""
    source_hash: str = ""
    log_path: Path | None = None


class ActualState(BaseModel):
    """supervisor 写入 `.blue/state.json` 的实际状态。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    version: int = 1
    generation: str
    supervisor_pid: int
    started_at: float
    updated_at: float
    config_error: str | None = None
    services: dict[str, ActualService] = Field(default_factory=dict)
