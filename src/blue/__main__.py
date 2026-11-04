"""支持 ``python -m blue``：后台调谐器子进程与 execv 自重启都依赖它。"""

from __future__ import annotations

import sys

from blue.cli import main

if __name__ == "__main__":
    sys.exit(main())
