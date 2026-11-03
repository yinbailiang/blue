"""外置工具处理器：一个工具一个模块、一个进程。

每个模块暴露 ``TOOL`` 可调用对象，并可直接作为独立进程启动：

```bash
uv run python -m handlers.tools.echo        # 回显
uv run python -m handlers.tools.clock       # 当前时间
uv run python -m handlers.tools.text        # 文本统计
uv run python -m handlers.tools.read_file   # 读取文件
uv run python -m handlers.tools.write_file  # 写入文件
uv run python -m handlers.tools.edit_file   # 精确替换
uv run python -m handlers.tools.shell       # 执行 shell 命令
```

工具进程启动后会向核心的工具目录自我注册（``tool.register``），并周期续约。
"""

from __future__ import annotations

__all__ = ["TOOLS"]

TOOLS: dict[str, str] = {
    "clock": "handlers.tools.clock",
    "echo": "handlers.tools.echo",
    "edit_file": "handlers.tools.edit_file",
    "read_file": "handlers.tools.read_file",
    "run_shell": "handlers.tools.shell",
    "text_stats": "handlers.tools.text",
    "write_file": "handlers.tools.write_file",
}
"""短名 → 模块路径（供文档与自定义启动脚本参考，核心不依赖此表）。"""
