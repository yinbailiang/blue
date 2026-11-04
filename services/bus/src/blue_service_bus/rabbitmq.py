"""RabbitMQ 生命周期与配置管理。"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import shutil
import signal
import socket
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from blue.errors import RuntimeConfigError
from blue.runtime import state_dir
from blue_service_bus.config import BusConfig

logger = logging.getLogger(__name__)

__all__ = [
    "AmqpEndpoint",
    "RabbitMQManager",
    "parse_amqp_url",
    "resolve_password",
]


def resolve_password(config: BusConfig) -> str:
    """解析 RabbitMQ 密码。

    取自 ``secrets.toml`` 的 ``[bus].password``（经 supervisor 写入运行时
    配置），**不读环境变量**。留空且 ``user = "guest"`` 时回退到 RabbitMQ
    默认密码；其它用户留空视为配置缺失。

    :raises RuntimeConfigError: 非 guest 用户未配置密码。
    """
    if config.password:
        return config.password
    if config.user == "guest":
        return "guest"
    raise RuntimeConfigError(
        "未设置 RabbitMQ 密码：请在 secrets.toml 的 [bus].password 中配置"
    )


@dataclass(frozen=True)
class AmqpEndpoint:
    """AMQP URL 解析结果。"""

    host: str
    port: int
    vhost: str


def parse_amqp_url(url: str) -> AmqpEndpoint:
    """解析 ``amqp://user:pass@host:port/vhost``。

    :raises RuntimeConfigError: URL 缺少主机或端口非法。
    """
    try:
        parsed = urlsplit(url)
    except ValueError as error:
        raise RuntimeConfigError(f"AMQP URL 非法：{url}") from error
    if parsed.scheme not in ("amqp", "amqps"):
        raise RuntimeConfigError(f"AMQP URL 协议必须为 amqp/amqps：{url}")
    host = parsed.hostname
    if not host:
        raise RuntimeConfigError(f"AMQP URL 缺少主机：{url}")
    try:
        port = parsed.port or (5671 if parsed.scheme == "amqps" else 5672)
    except ValueError as error:
        raise RuntimeConfigError(f"AMQP URL 端口非法：{url}") from error
    vhost = unquote(parsed.path.lstrip("/")) or "/"
    return AmqpEndpoint(host=host, port=port, vhost=vhost)


class RabbitMQManager:
    """按 managed / external 模式准备 RabbitMQ。"""

    def __init__(self, config: BusConfig) -> None:
        self._config = config
        self._endpoint = parse_amqp_url(config.url)
        self._process: asyncio.subprocess.Process | None = None
        prefix = "blue" if config.mode == "managed" else "rabbit"
        self._node_name = os.environ.get("RABBITMQ_NODENAME") or (
            f"{prefix}@{socket.gethostname()}"
        )

    @property
    def endpoint(self) -> AmqpEndpoint:
        """上游 AMQP 端点。"""
        return self._endpoint

    async def start(self) -> None:
        """启动或连接 RabbitMQ，并下发 vhost/用户配置。"""
        if self._config.mode == "external":
            await self._wait_tcp()
            if self._config.configure:
                await self._configure()
            return

        if await self._tcp_open():
            logger.info(
                "检测到已有 RabbitMQ，按 managed 模式复用：%s", self._config.url
            )
            if self._config.configure:
                await self._configure()
            return

        self._process = await self._spawn()
        await self._wait_tcp()
        if self._config.configure:
            await self._configure()

    async def stop(self) -> None:
        """仅停止由本服务启动的 RabbitMQ 进程。"""
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        _signal_group(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=self._config.stop_timeout)
        except TimeoutError:
            _signal_group(process.pid, signal.SIGKILL)
            await process.wait()

    async def _spawn(self) -> asyncio.subprocess.Process:
        binary = shutil.which(self._config.rabbitmq_bin)
        if binary is None:
            raise RuntimeConfigError(
                f"未找到 RabbitMQ 可执行文件：{self._config.rabbitmq_bin}"
            )
        data_dir = self._config.data_dir
        if not data_dir.is_absolute():
            data_dir = (state_dir() / data_dir).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update(
            {
                "RABBITMQ_NODENAME": self._node_name,
                "RABBITMQ_NODE_PORT": str(self._endpoint.port),
                "RABBITMQ_MNESIA_BASE": str(data_dir / "mnesia"),
                "RABBITMQ_LOG_BASE": str(data_dir / "log"),
                "RABBITMQ_DEFAULT_USER": self._config.user,
                "RABBITMQ_DEFAULT_PASS": self._password(),
                "RABBITMQ_DEFAULT_VHOST": self._config.vhost,
            }
        )
        try:
            return await asyncio.create_subprocess_exec(
                binary,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                start_new_session=True,
                preexec_fn=_set_pdeathsig if sys.platform.startswith("linux") else None,
            )
        except OSError as error:
            raise RuntimeConfigError(f"启动 RabbitMQ 失败：{error}") from error

    async def _wait_tcp(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._config.startup_timeout
        while loop.time() < deadline:
            if self._process is not None and self._process.returncode is not None:
                raise RuntimeConfigError(
                    f"RabbitMQ 提前退出（exit={self._process.returncode}）"
                )
            if await self._tcp_open():
                return
            await asyncio.sleep(0.2)
        raise RuntimeConfigError(
            f"RabbitMQ 在 {self._config.startup_timeout}s 内未监听 "
            f"{self._endpoint.host}:{self._endpoint.port}"
        )

    async def _tcp_open(self) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._endpoint.host, self._endpoint.port),
                timeout=0.5,
            )
        except OSError, TimeoutError:
            return False
        del reader
        writer.close()
        await writer.wait_closed()
        return True

    async def _configure(self) -> None:
        """通过 rabbitmqctl 下发 vhost、用户与权限。"""
        ctl = shutil.which(self._config.rabbitmqctl_bin)
        if ctl is None:
            raise RuntimeConfigError(
                f"未找到 rabbitmqctl：{self._config.rabbitmqctl_bin}"
            )
        env = dict(os.environ)
        env["RABBITMQ_NODENAME"] = self._node_name
        commands = [
            ["add_vhost", self._config.vhost],
            ["add_user", self._config.user, self._password()],
            ["change_password", self._config.user, self._password()],
            [
                "set_permissions",
                "-p",
                self._config.vhost,
                self._config.user,
                ".*",
                ".*",
                ".*",
            ],
        ]
        for arguments in commands:
            await self._run_ctl(ctl, arguments, env, ignore_failure=True)
        await self.ensure_queue_policy(ctl, env)

    async def ensure_queue_policy(self, ctl: str, env: dict[str, str]) -> None:
        """给 fanout 队列下发「空闲即过期」策略。

        成员队列是 durable + 非 auto-delete（``restart`` 策略要留积压），所以杀掉
        进程后队列会留在 broker 上继续接收广播。加 ``expires`` 之后，最后一个消费
        者断开 30 分钟（可配）内没有新消费者，队列自动删除——孤儿不会累积。
        """
        expires = self._config.queue_expires_ms
        if expires <= 0:
            return
        definition = json.dumps({"expires": expires}, separators=(",", ":"))
        await self._run_ctl(
            ctl,
            [
                "set_policy",
                "-p",
                self._config.vhost,
                "blue-fanout-expiry",
                "^infinity_bus\\.fanout\\.",
                definition,
                "--apply-to",
                "queues",
            ],
            env,
            ignore_failure=True,
        )

    async def _run_ctl(
        self,
        ctl: str,
        arguments: list[str],
        env: dict[str, str],
        *,
        ignore_failure: bool,
    ) -> None:
        process = await asyncio.create_subprocess_exec(
            ctl,
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0 and not ignore_failure:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeConfigError(
                f"rabbitmqctl {' '.join(arguments)} 失败：{message}"
            )
        logger.debug(
            "rabbitmqctl %s -> %s %s",
            " ".join(arguments),
            process.returncode,
            stdout.decode("utf-8", errors="replace").strip(),
        )

    def _password(self) -> str:
        """RabbitMQ 密码（供 rabbitmqctl 与 broker 环境使用）。"""
        return resolve_password(self._config)


def _signal_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(os.getpgid(pid), sig)
    except ProcessLookupError:
        return
    except PermissionError:
        return


def _set_pdeathsig() -> None:
    """Linux：父进程退出时向 RabbitMQ 发送 SIGTERM。"""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:
        try:
            libc = ctypes.CDLL(None, use_errno=True)
        except OSError:
            return
    try:
        prctl: Any = libc.prctl
        prctl(1, signal.SIGTERM, 0, 0, 0)
    except Exception:
        return
