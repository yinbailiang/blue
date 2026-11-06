"""跨进程独占文件锁（``fcntl.flock``）的最小封装。

控制面里有两把锁，语义不同但机制完全一样：

- ``supervisor.lock`` —— 调谐器的**单实例锁**，常驻持有；拿不到说明已有一个
  supervisor 在跑，属于致命错误；
- ``maintenance.lock`` —— ``apply``（构建产物）与 ``gc``（回收产物）的互斥锁；
  拿不到说明"这会儿正忙"，跳过本次即可。

两者的差别只在**失败怎么办**（抛错 vs 跳过）和**锁文件里写什么**（JSON 还是
pid），锁本身一模一样。所以这里只提供机制，不提供策略：

```python
lock = FileLock(state_dir / "supervisor.lock")
if not lock.acquire():
    raise SupervisorError(f"已在运行：{lock.holder()}")
try:
    lock.write(info.model_dump_json(), fsync=True)
finally:
    lock.release()
```

为什么用 ``flock`` 而不是"检查 PID 文件是否存在"：进程被 SIGKILL 后文件留在
盘上，按存在性判断会永久锁死；``flock`` 由内核在 fd 关闭时自动释放，崩溃即解锁。
"""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import TextIO

__all__ = ["FileLock"]


class FileLock:
    """一把**不可重入**的独占锁：``acquire`` 取、``release`` 放。

    为什么不可重入：重入会把两类 bug 变成静默行为——

    1. **作用域比你以为的长**：内层 ``with`` 看似已经出了临界区，实际外层还持着锁，
       竞争方在等一个你没意识到还在持有的锁；
    2. **少释放一次**：多取一次、只放一次，锁就永久泄漏，直到进程退出。

    重入计数能让这两种情况“跑得下去”，代价是把问题推迟到更难查的时刻。这里选择
    在**第一次重复进入**就抛 ``RuntimeError``。真遇到“内外层都要用”的场景，正确做法
    是把临界区提到共同祖先，而不是让锁重入。

    唯一的宽松处是 ``release``：没持锁时是 no-op，因为 ``finally`` 里要对“取锁失败”
    的路径也安全。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: TextIO | None = None

    @property
    def held(self) -> bool:
        """当前对象是否持有锁。"""
        return self._handle is not None

    def acquire(self, *, wait: float = 0.0, interval: float = 0.05) -> bool:
        """尝试取锁。

        :param wait: 最多等多久（秒）；``0`` 表示现在就给结果，不阻塞
        :param interval: 轮询间隔（秒）
        :return: 是否拿到了锁
        :raises RuntimeError: 本对象已持有该锁（重复取锁是编程错误，不是竞争）。
        """
        if self._handle is not None:
            # 再开一个 fd 去 flock 同一文件只会永远失败（flock 按 open file
            # description 计），看上去像“别人占着”，实际是自己——不如直接报错。
            raise RuntimeError(f"已持有锁，不能重复取：{self.path}")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # "a+" 而不是 "w"：不能用创建即截断的方式，否则两个进程会互相抹掉对方的 pid
        handle = self.path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.close()
                    return False
                time.sleep(interval)
                continue
            self._handle = handle
            return True

    def write(self, text: str, *, fsync: bool = False) -> None:
        """整体覆写锁文件内容（调用方必须已持有锁）。

        :param fsync: 是否落盘。对"别人会读它判断持有者"的锁（supervisor）需要，
            对纯互斥用的锁（maintenance）不必。
        :raises RuntimeError: 尚未持有锁。
        """
        handle = self._handle
        if handle is None:
            raise RuntimeError("尚未持有锁，不能写锁文件")
        handle.seek(0)
        handle.truncate()
        handle.write(text)
        handle.flush()
        if fsync:
            os.fsync(handle.fileno())

    def holder(self) -> str:
        """锁文件当前内容的快照，用于"谁占着这把锁"的诊断信息。

        只在**取锁失败之后**调用才有意义；取锁成功时内容可能还是上一位持有者
        留下的，直到 :meth:`write` 覆盖它。
        """
        with suppress(OSError):
            return self.path.read_text(encoding="utf-8").strip()
        return ""

    def release(self) -> None:
        """释放锁；没拿到过时什么也不做。

        只在这一点上宽松：``finally`` 里对“取锁失败”的路径也得安全。重复进入、
        重复取锁则一律报错（见类 docstring）。
        """
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        with suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        with suppress(OSError):
            handle.close()

    # ------------------------------------------------------------------
    # 上下文管理器
    # ------------------------------------------------------------------

    def __enter__(self) -> FileLock:
        """取锁后进入 ``with``；取不到抛 ``BlockingIOError``。

        需要等待就先显式调 :meth:`acquire`（可以带 ``wait``），``with`` 只管
        “拿到了没有”这个二选一：拿到就进、拿不到就报。

        :raises RuntimeError: 本对象已经持锁——嵌套 ``with`` 会直接报错，而不是
            静默重入。
        """
        if self._handle is not None:
            raise RuntimeError(f"锁已在当前作用域内持有，不能嵌套进入：{self.path}")
        if not self.acquire():
            raise BlockingIOError(
                f"锁已被占用：{self.path}（{self.holder() or '未知持有者'}）"
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        """退出 ``with`` 时释放；异常照常向上抛。"""
        self.release()
