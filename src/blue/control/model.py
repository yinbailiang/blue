"""控制面数据模型：通用服务元数据、期望状态、实际状态与 supervisor 配置。"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ActivationState",
    "ActualContract",
    "ActualService",
    "ActualState",
    "ContractMeta",
    "DesiredContract",
    "DesiredSdk",
    "DesiredSdkPackage",
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
    depends_contracts: tuple[str, ...] = Field(
        default=(), description="依赖的事件契约名"
    )
    depends_sdk: tuple[str, ...] = Field(
        default=(), description="依赖的 SDK 运行时包名（sdk/* 目录名）"
    )
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
    activation_timeout: float = Field(
        default=120.0,
        gt=0,
        description="激活新世代后等全部服务 Ready 的上限（秒）；超时按失败处理",
    )
    generation_keep: int = Field(
        default=5, ge=1, le=1000, description="gc 至少保留多少个最近世代"
    )
    generation_auto_rollback: bool = Field(
        default=True, description="激活验证失败时自动回滚到上一个 stable 世代"
    )
    artifact_python: str = Field(
        default="3.14", description="建 artifact venv 用的解释器（交给 uv 解析/下载）"
    )
    artifact_link_mode: str = Field(
        default="hardlink", description="uv 安装产物时的 link mode"
    )
    artifact_warn_cross_device: bool = Field(
        default=True, description="硬链接跨分区（会退化成拷贝）时是否告警"
    )


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
    cwd: Path | None = None
    """进程工作目录；``None`` 表示用 ``service_dir``（未被世代化时的旧行为）。

    世代化之后固定为**项目根**：服务进程的相对路径语义与文件工具一致，而不是
    "相对于自己被启动的那个目录"——后者曾经让 `read_file("AGENT.md")` 读到服务目录里。
    """
    artifact_hash: str = ""
    """产物（artifact）指纹；空串表示还没构建产物，退回 ``uv run`` 启动。"""
    artifact_python: Path | None = None
    """产物 venv 里的解释器；给了就用它直接 ``-m`` 启动，不再经过 uv。"""


class ContractMeta(BaseModel):
    """`contract.infd` 中的契约元数据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="契约名 / 目录名")
    package: str = Field(description="uv workspace 包名")
    module: str = Field(description="Python 模块名")
    description: str = Field(default="", description="契约说明")


class DesiredContract(BaseModel):
    """一个契约包：元数据 + 包版本 + 内容指纹。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    meta: ContractMeta
    package_version: str = ""
    source_hash: str = ""
    contract_dir: Path
    contract_path: Path


class SdkMeta(BaseModel):
    """``sdk.infd`` 中的运行时包元数据（与 ``ContractMeta`` 同构）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="SDK 名 / 目录名")
    package: str = Field(description="uv workspace 包名")
    module: str = Field(description="Python 模块名")
    description: str = Field(default="", description="说明")


class DesiredSdkPackage(BaseModel):
    """一个 workspace 运行时包（``sdk/*``）：元数据 + 版本 + 内容指纹。"""

    meta: SdkMeta
    package_version: str = ""
    source_hash: str = ""


class DesiredSdk(BaseModel):
    """blue SDK 自身的指纹：它是每个服务的隐式运行时依赖。

    ``source_hash`` 只覆盖 ``blue`` 核心（每个服务都 import）；``packages`` 是按
    服务按需安装的 workspace 运行时包（``sdk/<name>/sdk.infd`` 声明元数据，
    如 ``tool`` → ``blue-tool-sdk``）——它们的变化只重启声明了 ``depends_sdk``
    的服务。
    """

    package_version: str = ""
    source_hash: str = ""
    packages: dict[str, DesiredSdkPackage] = Field(default_factory=dict)


class DesiredState(BaseModel):
    """一次配置编译得到的完整期望状态。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    generation: str
    config_path: Path
    base_dir: Path
    supervisor: SupervisorSettings
    services: dict[str, DesiredService]
    contracts: dict[str, DesiredContract] = Field(default_factory=dict)
    sdk: DesiredSdk = Field(default_factory=DesiredSdk)
    bus_url: str
    bus_transport: Literal["rabbit", "memory"]
    deps_hash: str = ""
    """``uv.lock`` 的内容指纹。

    依赖变了但配置与源码都没变时，``generation`` 必须跟着变——否则 apply 会认为
    "无事可做"，产物也永远不会重建。它也进 ``artifact_hash``（产物闭包）。
    """


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
    artifact_hash: str = ""
    """当前进程是从哪个产物启动的（空串 = 未经世代化，用工作区 `uv run` 启动）。"""
    config: dict[str, Any] = Field(default_factory=dict)
    """脱敏后的配置快照，供 ``blue plan --diff`` 做字段级对比。

    旧版本写下的 state.json 没有这个字段（默认空 dict），此时预览会跳过
    「具体改了哪个字段」，只报告 config 变了。
    """
    log_path: Path | None = None


class ActualContract(BaseModel):
    """一个契约包的实际快照（非进程，只用于展示与变更追踪）。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    package: str
    module: str
    description: str = ""
    package_version: str = ""
    source_hash: str = ""


class ActualSdk(BaseModel):
    """上一轮部署时蓝本 SDK 的指纹（用于发现「SDK 改了但服务没重启」）。"""

    package_version: str = ""
    source_hash: str = ""
    packages: dict[str, DesiredSdkPackage] = Field(default_factory=dict)


class ActivationState(BaseModel):
    """一次世代激活的验证进度（调谐器维护，供 ``apply --wait`` 与 ``status`` 读取）。"""

    phase: Literal["pending", "stable", "failed", "rolled_back"] = "pending"
    generation_id: int | None = None
    """正在验证的世代号。"""
    previous_id: int | None = None
    """回滚的目标世代号（``rolled_back`` 时有意义）。"""
    started_at: float = 0.0
    deadline: float = 0.0
    """验证截止的绝对时间（epoch 秒）。"""
    message: str = ""
    """失败/回滚原因：哪些服务没就绪。"""
    failures: list[str] = Field(default_factory=list[str])


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
    contracts: dict[str, ActualContract] = Field(default_factory=dict)
    sdk: ActualSdk | None = None
    """旧快照没有这一段；缺省表示「无法判断」，首次升级会把所有服务重启一遍。"""
    generation_id: int | None = None
    """当前 ``current`` 指向的世代号（旧快照没有，按未世代化处理）。"""
    last_stable_id: int | None = None
    """最近一次验证通过的世代号，崩溃回退与自动回滚都用它。"""
    activation: ActivationState | None = None
