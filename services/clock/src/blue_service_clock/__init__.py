"""blue 外置工具：读取当前时间。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from blue_tool_sdk import run_tool


class ClockResult(BaseModel):
    """时间查询结果。"""

    iso: str = Field(description="ISO 8601 时间")
    epoch: float = Field(description="Unix 时间戳")
    zone: str = Field(description="时区标识")


def now(
    zone: Annotated[Literal["utc", "local"], "时区：utc 或 local"] = "utc",
) -> ClockResult:
    """返回当前时间。"""
    moment = datetime.now(UTC) if zone == "utc" else datetime.now().astimezone()
    return ClockResult(iso=moment.isoformat(), epoch=moment.timestamp(), zone=zone)


TOOL = now
"""工具进程入口。"""


def main() -> int:
    """控制台脚本入口。"""
    return run_tool(TOOL, "clock")


if __name__ == "__main__":
    raise SystemExit(main())
