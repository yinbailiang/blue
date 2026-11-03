"""外置程序：工具处理器与用户界面。

核心（``blue`` 包）只提供本体聚合与总线契约，不管理外围程序；这里的每个
程序都独立运行，通过 RabbitMQ 总线与核心通信：

```bash
uv run python -m handlers.tools.echo          # 工具处理器（一个工具一个进程）
uv run python -m handlers.tools.clock
uv run python -m handlers.tools.text
uv run python -m handlers.ui.chat --text "你好"   # 用户界面
uv run python -m handlers.ui.inspect tools
```

公共设施（读配置、建总线、跑生命周期）在 :mod:`handlers.support`。
"""

from __future__ import annotations

__all__: list[str] = []
