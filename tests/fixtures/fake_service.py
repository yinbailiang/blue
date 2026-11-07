"""测试用假服务：支持正常、退出、崩溃与一次性崩溃模式。"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="sleep")
    parser.add_argument("--state-file", default=None)
    args = parser.parse_args()

    name = os.environ.get("BLUE_SERVICE_NAME", "unknown")
    config_path = os.environ.get("BLUE_CONFIG_FILE", "")
    print(f"start name={name} config={config_path}", flush=True)

    match args.mode:
        case "exit0":
            return 0
        case "exit1":
            print("boom", file=sys.stderr, flush=True)
            return 1
        case "exit1-late":
            # 熬过 readiness="process" 的 100ms 观察期再退出：探针因此会把它判成
            # READY，而它随即崩溃。用来验证激活闸门不会把这种「起来就死」的服务
            # 当成激活成功（否则坏世代会被标成 stable，永不回滚）。
            time.sleep(0.15)
            print("late-boom", file=sys.stderr, flush=True)
            return 1
        case "crash-once":
            path = Path(args.state_file or "fake.state")
            if not path.exists():
                path.write_text("1", encoding="utf-8")
                return 2
            return _sleep()
        case _:
            return _sleep()


def _sleep() -> int:
    try:
        while True:
            time.sleep(0.2)
            print("tick", flush=True)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
