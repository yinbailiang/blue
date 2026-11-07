"""`blue.control.smoke` 的镜像测试：服务自己声明的检查怎么判定。

冒烟是"激活失败就回滚"的判据，判错方向代价很大——所以这里既测「该失败的失败」，
也测「没声明的别拦」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from blue.control.artifacts import ArtifactStore
from blue.control.model import (
    DesiredService,
    DesiredState,
    ServiceMeta,
    SupervisorSettings,
)
from blue.control.smoke import (
    Check,
    SmokeReport,
    _check_tools,
    _command_for,
    _declared,
    _Plan,
    _query_tools,
    _run_command,
    run_smoke,
)

# pyright: reportPrivateUsage=false


def _meta(name: str, **overrides: object) -> ServiceMeta:
    base: dict[str, object] = {
        "name": name,
        "kind": "tool",
        "package": f"blue-service-{name}",
        "module": f"blue_service_{name}",
    }
    base.update(overrides)
    return ServiceMeta.model_validate(base)


def _service(tmp_path: Path, name: str, **overrides: object) -> DesiredService:
    return DesiredService(
        meta=_meta(name, **overrides),
        config={},
        config_hash="cfg",
        service_dir=tmp_path / "services" / name,
        config_path=tmp_path / "services" / name / "service_runtime_config.json",
        cwd=tmp_path,
    )


def _desired(tmp_path: Path, **services: DesiredService) -> DesiredState:
    return DesiredState(
        generation="gen",
        config_path=tmp_path / "blue.infd",
        base_dir=tmp_path,
        supervisor=SupervisorSettings(state_dir=tmp_path / ".blue"),
        services=dict(services),
        bus_url="amqp://guest:guest@127.0.0.1:5672/",
        bus_transport="rabbit",
    )


def _plan(service: DesiredService) -> _Plan:
    return _Plan(
        service=service,
        tools=service.meta.smoke_tools,
        min_tools=service.meta.smoke_min_tools,
        command=service.meta.smoke_command,
    )


def test_only_declared_services_are_checked(tmp_path: Path) -> None:
    """没声明的服务不该被拦——显式优于默认拦截。"""
    desired = _desired(
        tmp_path,
        quiet=_service(tmp_path, "quiet"),
        loud=_service(tmp_path, "loud", smoke_tools=["now"]),
    )

    plans = _declared(desired)

    assert [plan.service.meta.name for plan in plans] == ["loud"]


def test_declared_tool_missing_fails(tmp_path: Path) -> None:
    """声明了 smoke_tools 而注册表里没有 → 失败并点名缺哪个。"""
    service = _service(tmp_path, "weather", smoke_tools=["get_weather"])

    checks = _check_tools(_plan(service), frozenset({"now"}), "")

    assert [check.ok for check in checks] == [False]
    assert "get_weather" in checks[0].detail


def test_declared_tool_present_passes(tmp_path: Path) -> None:
    """工具都在就通过。"""
    service = _service(tmp_path, "weather", smoke_tools=["get_weather"])

    checks = _check_tools(_plan(service), frozenset({"now", "get_weather"}), "")

    assert [check.ok for check in checks] == [True]


def test_catalog_unavailable_fails_instead_of_skipping(tmp_path: Path) -> None:
    """查不到目录是失败，不是"没声明"——否则注册中心挂了会被当成没事。"""
    service = _service(tmp_path, "weather", smoke_tools=["get_weather"])

    checks = _check_tools(_plan(service), None, "工具目录查询失败：连接被拒绝")

    assert checks[0].ok is False
    assert "连接被拒绝" in checks[0].detail


def test_min_tools_counts_declared_names(tmp_path: Path) -> None:
    """列了具体名字时，min_tools 按列出的那些计数。"""
    service = _service(
        tmp_path,
        "terminal",
        smoke_tools=["terminal_open", "terminal_write"],
        smoke_min_tools=2,
    )

    both = _check_tools(
        _plan(service), frozenset({"terminal_open", "terminal_write"}), ""
    )
    only_one = _check_tools(_plan(service), frozenset({"terminal_open"}), "")

    assert all(check.ok for check in both)
    assert any(not check.ok for check in only_one)


def test_min_tools_without_names_falls_back_to_catalog(tmp_path: Path) -> None:
    """只声明数量时退化成看全局目录（抓"全军覆没"）。"""
    service = _service(tmp_path, "terminal", smoke_min_tools=1)

    assert _check_tools(_plan(service), frozenset({"now"}), "")[0].ok is True
    assert _check_tools(_plan(service), frozenset(), "")[0].ok is False


def test_report_summary_counts_and_names_failures() -> None:
    """汇总要能一眼看出哪条挂了。"""
    report = SmokeReport(
        checks=(
            Check(
                service="weather",
                name="smoke_tools",
                ok=False,
                detail="缺少工具：get_weather",
            ),
            Check(service="clock", name="smoke_tools", ok=True),
        )
    )

    assert report.ok is False
    assert report.declared == 2
    summary = report.summary()
    assert "1/2 通过" in summary
    assert "smoke_tools×2" in summary, "类型分布要能看出跑了哪几类检查"
    assert "weather.smoke_tools" in summary
    assert "get_weather" in summary


def test_report_without_checks_is_ok() -> None:
    """没有服务声明时不该让 apply 失败。"""
    report = SmokeReport()

    assert report.ok is True
    assert report.declared == 0
    assert "没有服务声明" in report.summary()


def test_command_for_replaces_python_with_artifact_interpreter(
    tmp_path: Path,
) -> None:
    """声明里的 python 要换成该服务产物的解释器（跑的就是这个世代的代码）。"""
    interpreter = tmp_path / "venv" / "bin" / "python"
    service = _service(
        tmp_path,
        "session",
        smoke_command=("python", "-m", "blue_service_session.smoke"),
    ).model_copy(update={"artifact_python": interpreter})
    artifacts = ArtifactStore(tmp_path / ".blue", project_root=tmp_path)

    command = _command_for(service, artifacts)

    assert command[0] == str(interpreter)
    assert command[1:] == ["-m", "blue_service_session.smoke"]


def test_run_command_reports_nonzero_exit(tmp_path: Path) -> None:
    """命令失败要把退出码和输出尾部带回来，别只说"失败"。"""
    service = _service(
        tmp_path,
        "session",
        smoke_command=("python", "-c", 'import sys; print("炸了"); sys.exit(3)'),
    )
    artifacts = ArtifactStore(tmp_path / ".blue", project_root=tmp_path)

    check = asyncio.run(
        _run_command(
            _desired(tmp_path, session=service),
            service,
            artifacts,
            timeout=10.0,
        )
    )

    assert check.ok is False
    assert "退出码 3" in check.detail
    assert "炸了" in check.detail


def test_run_command_reports_timeout(tmp_path: Path) -> None:
    """超时也要说清楚（长时间挂着绝不能算通过）。"""
    service = _service(
        tmp_path,
        "slow",
        smoke_command=("python", "-c", "import time; time.sleep(30)"),
    )
    artifacts = ArtifactStore(tmp_path / ".blue", project_root=tmp_path)

    check = asyncio.run(
        _run_command(_desired(tmp_path, slow=service), service, artifacts, timeout=0.3)
    )

    assert check.ok is False
    assert "超时" in check.detail


def test_run_smoke_without_declarations_needs_no_bus(tmp_path: Path) -> None:
    """没有服务声明时不碰总线，直接通过。"""
    desired = _desired(tmp_path, quiet=_service(tmp_path, "quiet"))

    report = asyncio.run(
        run_smoke(
            desired,
            artifacts=ArtifactStore(tmp_path / ".blue", project_root=tmp_path),
        )
    )

    assert report.ok is True
    assert report.checks == ()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("smoke_tools", ("now",)),
        ("smoke_min_tools", 1),
        ("smoke_command", ("python", "-c", "print(1)")),
    ],
)
def test_service_meta_accepts_smoke_declarations(name: str, value: object) -> None:
    """三个声明字段都要能通过 ServiceMeta 校验（infd 模板用得到）。"""
    meta = _meta("demo", **{name: value})

    assert getattr(meta, name) == value


def test_query_tools_uses_offline_so_no_durable_queue_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """冒烟查目录用的是一次性成员，必须 ``offline``，别在 broker 上留持久队列。

    用默认的 ``restart``：停机只停「消费」、保留「路由」，这个成员队列会继续
    接收整个 fanout 的全部事件，直到队列守卫的 expires（30 分钟）才消失——
    每次 ``blue apply`` 漏一个。``offline`` 停机时解绑路由并排空，不再累积。
    """
    captured: dict[str, object] = {}
    stopped: list[bool] = []

    class _Stub:
        async def start(self) -> None:
            raise RuntimeError("到此为止：不真连 broker")

        async def stop(self) -> None:
            stopped.append(True)

        def proxy(self, role: str) -> object:
            return object()

    def fake_build(
        role: str | None = None,
        events: object = None,
        url: str | None = None,
        strategy: str = "restart",
    ) -> object:
        captured["role"] = role
        captured["strategy"] = strategy
        return _Stub()

    monkeypatch.setattr("blue.runtime.build_runtime", fake_build)

    tools, reason = asyncio.run(_query_tools(_desired(tmp_path), timeout=1.0))

    assert tools is None
    assert "到此为止" in reason
    assert captured["role"] == "agent.smoke"
    assert captured["strategy"] == "offline", (
        "冒烟成员必须是 offline，否则每次 apply 都在 broker 上留一个持久队列"
    )
    assert stopped == [True], "无论成败都要停掉运行时，否则连接泄漏"
