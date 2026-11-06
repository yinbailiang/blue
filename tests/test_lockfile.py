"""共享文件锁测试：一把 ``flock`` 封装，两种失败策略各自在上层。

``control.state`` 里有两把锁（调谐器单实例锁、apply/gc 互斥锁），机制相同、
失败语义不同。这里只测机制：
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from blue.lockfile import FileLock


def test_acquire_is_exclusive(tmp_path: Path) -> None:
    """同一文件第二把锁拿不到；释放后立刻能拿。"""
    path = tmp_path / "demo.lock"
    first = FileLock(path)
    second = FileLock(path)

    assert first.acquire() is True
    assert first.held is True
    assert second.acquire() is False
    assert second.held is False

    first.release()
    assert first.held is False

    assert second.acquire() is True
    second.release()


def test_wait_polls_until_released(tmp_path: Path) -> None:
    """``wait`` 期间轮询：持有者释放后应当拿到锁。"""
    path = tmp_path / "demo.lock"
    first = FileLock(path)
    second = FileLock(path)
    assert first.acquire()

    def release_soon() -> None:
        time.sleep(0.1)
        first.release()

    releaser = threading.Thread(target=release_soon)
    releaser.start()
    try:
        assert second.acquire(wait=2.0, interval=0.01) is True
    finally:
        releaser.join()
        second.release()


def test_wait_gives_up_at_deadline(tmp_path: Path) -> None:
    """超时后返回 ``False``，不会无限等下去。"""
    path = tmp_path / "demo.lock"
    first = FileLock(path)
    assert first.acquire()
    try:
        started = time.monotonic()
        assert FileLock(path).acquire(wait=0.15, interval=0.01) is False
        assert time.monotonic() - started >= 0.15
    finally:
        first.release()


def test_release_is_idempotent(tmp_path: Path) -> None:
    """没拿到锁 / 重复释放都不该抛——调用方总写在 ``finally`` 里。"""
    lock = FileLock(tmp_path / "demo.lock")

    lock.release()

    assert lock.acquire() is True
    lock.release()
    lock.release()


def test_write_requires_the_lock(tmp_path: Path) -> None:
    """没持锁就写锁文件是编程错误，直接报出来。"""
    lock = FileLock(tmp_path / "demo.lock")
    with pytest.raises(RuntimeError):
        lock.write("pid=1\n")


def test_write_advances_and_holder_reads_it(tmp_path: Path) -> None:
    """写入会整体覆写（不是追加）；``holder()`` 读到的就是当前内容。"""
    path = tmp_path / "demo.lock"
    lock = FileLock(path)

    assert lock.holder() == "", "文件还不存在时应返回空串"

    assert lock.acquire()
    try:
        lock.write("pid=111\n")
        assert lock.holder() == "pid=111"
        lock.write("pid=222\n")
        assert lock.holder() == "pid=222", "旧内容必须被截断，而不是追加"
    finally:
        lock.release()


def test_holder_reports_the_other_process_on_failure(tmp_path: Path) -> None:
    """拿不到锁时 ``holder()`` 能说清是谁占着——报错信息靠它。"""
    path = tmp_path / "demo.lock"
    first = FileLock(path)
    second = FileLock(path)

    assert first.acquire()
    first.write("pid=4242\n")
    try:
        assert second.acquire() is False
        assert second.holder() == "pid=4242"
    finally:
        first.release()


def test_parent_directory_is_created(tmp_path: Path) -> None:
    """锁文件的父目录不存在时自动建出来（``.blue`` 可能还没初始化）。"""
    path = tmp_path / "nested" / "deeper" / "demo.lock"
    lock = FileLock(path)

    assert lock.acquire() is True
    try:
        assert path.exists()
    finally:
        lock.release()


def test_context_manager_acquires_and_releases(tmp_path: Path) -> None:
    """``with`` 进出即取锁 / 放锁。"""
    path = tmp_path / "demo.lock"
    lock = FileLock(path)

    with lock as held:
        assert held is lock
        assert lock.held is True
        assert FileLock(path).acquire() is False

    assert lock.held is False
    other = FileLock(path)
    assert other.acquire() is True
    other.release()


def test_context_manager_releases_on_error(tmp_path: Path) -> None:
    """块内抛异常也要放锁，否则 ``finally`` 就白写了。"""
    path = tmp_path / "demo.lock"
    lock = FileLock(path)

    with pytest.raises(ValueError, match="boom"), lock:
        raise ValueError("boom")

    other = FileLock(path)
    assert other.acquire() is True
    other.release()


def test_context_manager_raises_when_held(tmp_path: Path) -> None:
    """拿不到锁直接抛 ``BlockingIOError``，并把持有者写进消息。"""
    path = tmp_path / "demo.lock"
    first = FileLock(path)
    assert first.acquire()
    first.write("pid=4242\n")
    try:
        with pytest.raises(BlockingIOError, match="4242"), FileLock(path):
            pytest.fail("拿不到锁就不该进入 with 块")
    finally:
        first.release()


def test_acquire_twice_raises(tmp_path: Path) -> None:
    """同一对象重复取锁是编程错误，不是竞争。

    再开一个 fd 去 flock 同一文件只会永远失败，看上去像"别人占着"——实际是自己。
    报错比返回 ``False`` 诚实。
    """
    lock = FileLock(tmp_path / "demo.lock")
    assert lock.acquire() is True
    try:
        with pytest.raises(RuntimeError, match="重复取"):
            lock.acquire()
    finally:
        lock.release()

    assert lock.held is False


def test_context_manager_rejects_reentry(tmp_path: Path) -> None:
    """嵌套 ``with`` 直接报错，而不是静默重入。

    重入会把两类 bug 藏起来：作用域比你以为的长、以及少释放一次。
    """
    lock = FileLock(tmp_path / "demo.lock")

    with lock, pytest.raises(RuntimeError, match="嵌套进入"), lock:
        pytest.fail("嵌套进入应当抛错")

    assert lock.held is False, "外层仍然正常释放"
