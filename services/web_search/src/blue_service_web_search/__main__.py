"""web_search 服务进程入口。"""

from __future__ import annotations

from blue_service_web_search import main

__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
