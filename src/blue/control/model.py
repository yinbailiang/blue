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
    stop_timeout: float = Field(
        default=30.0,
        gt=0,
        description=(
            "本服务的优雅停止预算（秒）：supervisor 到点 SIGKILL。服务据此把总线的"
            "排空/任务超时压在它之内"
        ),
    )
    startup_timeout: float = Field(default=30.0, gt=0, description="启动就绪超时（秒）")
    readiness: ReadinessProbe = Field(default="process", description="就绪探针")
    ready_settle: float = Field(
        default=0.3,
        gt=0,
        description=(
            "readiness=process 的观察期（秒）：先等这么久再看进程是否还活着。"
            "它是「进程起来后立刻崩溃」与「真的起来了」之间的分界——太短会把"
            "起来就崩的服务误判成就绪（python 解释器启动本身就要 30~100ms，"
            "负载高时更久）。"
        ),
    )
    ready_tcp: str | None = Field(
        default=None, description="TCP 就绪目标（host:port）；缺省时 bus 自动推导"
    )
    env: dict[str, str] = Field(default_factory=dict, description="附加环境变量")
    smoke_tools: tuple[str, ...] = Field(
        default=(),
        description="冒烟：这些工具名必须出现在注册表里（服务自己声明，控制面只核对）",
    )
    smoke_min_tools: int = Field(
        default=0,
        ge=0,
        description="冒烟：本服务至少要注册几个工具（0 = 不检查；抓「一个都没注册」）",
    )
    smoke_command: tuple[str, ...] = Field(
        default=(),
        description=(
            "冒烟：自定义检查命令，退出码 0 视为通过。首元素为 python 时替换成"
            "该服务产物的解释器；cwd 为项目根；环境里带本世代的 BLUE_CONFIG_FILE"
        ),
    )
    smoke_timeout: float = Field(default=20.0, gt=0, description="冒烟命令超时（秒）")


class LogSettings(BaseModel):
    """日志轮转：每个服务一份，落在 ``state_dir/logs/<service>.log``。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_bytes: int = Field(
        default=10 * 1024 * 1024, gt=0, description="单日志上限（字节）"
    )
    backups: int = Field(default=3, ge=0, le=20, description="日志轮转保留数")


class RestartSettings(BaseModel):
    """进程重启退避与「这次崩溃算不算数」的判定。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backoff: float = Field(default=0.5, gt=0, description="重启退避基数（秒）")
    backoff_max: float = Field(default=30.0, gt=0, description="重启退避上限（秒）")
    max_attempts: int = Field(
        default=10, ge=1, description="连续失败多少次后进入 CrashLoopBackOff"
    )
    stable_after: float = Field(
        default=60.0, gt=0, description="稳定运行多久后重置失败计数（秒）"
    )


class RegistrySettings(BaseModel):
    """工具注册中心的租约：工具靠心跳续约，过期即从目录摘除。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ttl: float = Field(default=15.0, gt=0, description="工具租约时长（秒）")
    sweep_interval: float = Field(default=2.0, gt=0, description="租约巡检间隔（秒）")


class IdempotencySettings(BaseModel):
    """事件去重（at-least-once 的兜底）；去重库落在 ``state_dir/dedupe``。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = Field(
        default=True, description="总开关；下发到各服务的 infra.idempotency"
    )
    retention: float = Field(
        default=24 * 3600.0,
        gt=0,
        description="去重记录保留时长（秒）；必须覆盖任何重投窗口",
    )
    sweep_interval: float = Field(
        default=600.0, gt=0, description="去重表清理巡检间隔（秒）"
    )


class JournalPayloadSettings(BaseModel):
    """审计日志里事件正文的存储策略。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["digest", "full"] = Field(
        default="digest", description="负载存储：digest 只存 size+sha256，full 存正文"
    )
    allowlist: tuple[str, ...] = Field(
        default=(),
        description="这些事件名即使 digest 模式也存正文，用于定位关键事件",
    )
    max_bytes: int = Field(
        default=8192,
        gt=0,
        description="单个负载正文上限；超过只留摘要（避免把图片/长输出灌进库）",
    )


class JournalSettings(BaseModel):
    """审计日志：全量记录各服务**发射**的事件，每服务一个库。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = Field(
        default=False,
        description=(
            "总开关；下发到 infra.journal。库里只增不减，"
            "必须配 retention/max_rows/max_bytes"
        ),
    )
    retention: float = Field(
        default=24 * 3600.0, gt=0, description="保留时长（秒）；到期由后台 GC 删除"
    )
    max_rows: int | None = Field(
        default=None, ge=1, description="单库行数上限；None 表示只按时间回收"
    )
    max_bytes: int | None = Field(
        default=None, ge=1, description="库文件软上限（字节）；超出后从最老的成批删除"
    )
    payload: JournalPayloadSettings = Field(
        default_factory=JournalPayloadSettings, description="正文存储策略"
    )
    sweep_interval: float = Field(
        default=600.0, gt=0, description="GC 巡检间隔（秒）：每这么久最多清一次"
    )


class SmokeSettings(BaseModel):
    """闸门通过后跑服务自己声明的冒烟检查，失败按激活失败处理并回滚。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = Field(
        default=True,
        description="闸门通过后是否跑冒烟探针（验证「行为对」，不只是「起来了」）",
    )
    timeout: float = Field(
        default=20.0,
        gt=0,
        description="冒烟里总线请求的超时（命令超时由服务自己声明）",
    )


class ActivationSettings(BaseModel):
    """新世代激活后的健康验证。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout: float = Field(
        default=120.0,
        gt=0,
        description="激活后等全部服务 Ready 的上限（秒）；超时按失败处理",
    )
    await_control_change: bool = Field(
        default=True,
        description="检测到控制面代码变化（会 execv 自重启）时，apply 自动等待验证结果",
    )
    smoke: SmokeSettings = Field(
        default_factory=SmokeSettings, description="闸门通过后的冒烟阶段"
    )


class ArtifactSettings(BaseModel):
    """构建产物（每个世代一份 venv）的生成方式。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    python: str = Field(
        default="3.14", description="建 artifact venv 用的解释器（交给 uv 解析/下载）"
    )
    link_mode: str = Field(default="hardlink", description="uv 安装产物时的 link mode")
    warn_cross_device: bool = Field(
        default=True, description="硬链接跨分区（会退化成拷贝）时是否告警"
    )


class GenerationSettings(BaseModel):
    """世代（generation）机制：原子切换与回滚，见 ``docs/generations.md``。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    keep: int = Field(default=5, ge=1, le=1000, description="gc 至少保留多少个最近世代")
    auto_rollback: bool = Field(
        default=True, description="激活验证失败时自动回滚到上一个 stable 世代"
    )
    artifact: ArtifactSettings = Field(
        default_factory=ArtifactSettings, description="产物的构建方式"
    )


class SupervisorSettings(BaseModel):
    """`blue.infd` 中 `supervisor` 块的配置。

    结构与 ``supervisor.inft`` 的模板**同构**：配置怎么写、模型就怎么读，中间
    没有映射层。加一个配置项只改两处（模板 + 这里），不会出现"漏改映射表导致
    某项配置永远改不动"——``desired.py`` 里记过 ``idempotency`` 就这么死过一次。

    ``extra="allow"`` 是**升级路径**的必需品：这份配置由当前正在跑的调谐器解析，
    拒绝未知键会让"加了新配置项"变成既不能热重载、又不能自重启的死锁。
    未知键由 :func:`blue.control.desired.load_desired` 告警点名。
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    state_dir: Path = Field(default=Path(".blue"), description="状态/日志/锁目录")
    reconcile_interval: float = Field(
        default=2.0, gt=0, description="调谐轮询间隔（秒）"
    )
    shutdown_timeout: float = Field(
        default=60.0,
        gt=0,
        description=(
            "整场停机的墙钟预算（秒）：按依赖层级倒序、层内并发，每层最多等最慢的那个"
            "服务；到点仍未退出的进程被 SIGKILL"
        ),
    )
    uv_frozen: bool = Field(default=True, description="uv run 是否使用 --frozen")
    log: LogSettings = Field(default_factory=LogSettings, description="日志轮转")
    restart: RestartSettings = Field(
        default_factory=RestartSettings, description="重启退避与失败判定"
    )
    registry: RegistrySettings = Field(
        default_factory=RegistrySettings, description="工具注册中心租约"
    )
    idempotency: IdempotencySettings = Field(
        default_factory=IdempotencySettings, description="事件去重"
    )
    journal: JournalSettings = Field(
        default_factory=JournalSettings, description="审计日志"
    )
    activation: ActivationSettings = Field(
        default_factory=ActivationSettings, description="激活验证"
    )
    generation: GenerationSettings = Field(
        default_factory=GenerationSettings, description="世代机制"
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
    generation_id: int = 0
    """这份期望来自哪个世代（0 = 尚未世代化）。

    用来回答"这个进程现在用的是哪一代的配置"——``gc`` 必须据此保住**运行中
    进程仍在引用**的世代，否则会把活着的服务脚下的配置文件删掉。
    """


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
    generation_id: int = 0
    """当前进程用的配置来自哪个世代（0 = 未经世代化）。"""
    meta: ServiceMeta | None = None
    """该进程**启动时**的服务清单（kind / depends_on / depends_contracts / ...）。

    ``blue plan`` 靠它判断"只改了清单字段"要不要重启——reconciler 用内存里的
    ``DesiredService`` 做同一件事，但 CLI 是独立进程读不到内存，只能靠这一列。
    旧版本写下的 ``state.json`` 没有它（``None``），此时预览退回"无法判定"的说法。
    """
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
