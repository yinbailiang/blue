"""SDK 指纹：``src/blue/**/*.py`` 的内容必须参与计算。

这条测试是为了防住一个真实事故：指纹曾经只覆盖根 ``pyproject.toml``，
改 ``src/blue/worker.py`` 一个字节都不算变化，于是调谐器永远发现不了 SDK 改动。
"""

from __future__ import annotations

from pathlib import Path

from blue.control.desired import _load_sdk  # pyright: ignore[reportPrivateUsage]


def _fake_project(root: Path) -> Path:
    """造一个最小项目布局：根 pyproject.toml + src/blue/*.py。"""
    (root / "src" / "blue").mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "blue"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (root / "src" / "blue" / "worker.py").write_text("X = 1\n", encoding="utf-8")
    (root / "src" / "blue" / "sub").mkdir(exist_ok=True)
    (root / "src" / "blue" / "sub" / "mod.py").write_text("Y = 2\n", encoding="utf-8")
    return root


def test_sdk_hash_covers_python_sources(tmp_path: Path) -> None:
    """改 SDK 里的 .py 内容必须改变指纹。"""
    root = _fake_project(tmp_path)
    before = _load_sdk(root)
    (root / "src" / "blue" / "worker.py").write_text("X = 2\n", encoding="utf-8")
    after = _load_sdk(root)
    assert before.source_hash != after.source_hash


def test_sdk_hash_covers_nested_modules(tmp_path: Path) -> None:
    """子目录里的模块也算（``rglob`` 而不是只看顶层）。"""
    root = _fake_project(tmp_path)
    before = _load_sdk(root)
    (root / "src" / "blue" / "sub" / "mod.py").write_text("Y = 3\n", encoding="utf-8")
    assert _load_sdk(root).source_hash != before.source_hash


def test_sdk_hash_ignores_pycache_and_non_python(tmp_path: Path) -> None:
    """``__pycache__`` 与非 .py 文件不参与——否则每次编译都会误判"变了"。"""
    root = _fake_project(tmp_path)
    before = _load_sdk(root)
    cache = root / "src" / "blue" / "__pycache__"
    cache.mkdir()
    (cache / "worker.cpython-314.pyc").write_bytes(b"\x00\x01")
    (root / "src" / "blue" / "notes.txt").write_text("hi", encoding="utf-8")
    assert _load_sdk(root).source_hash == before.source_hash


def test_sdk_hash_is_stable_across_calls(tmp_path: Path) -> None:
    """同一份源码反复计算必须一致，否则会陷入永久重启循环。"""
    root = _fake_project(tmp_path)
    assert _load_sdk(root).source_hash == _load_sdk(root).source_hash


def test_sdk_version_comes_from_root_pyproject(tmp_path: Path) -> None:
    """版本号取根 pyproject.toml。"""
    root = _fake_project(tmp_path)
    assert _load_sdk(root).package_version == "0.1.0"


def test_missing_sdk_dir_yields_empty_fingerprint(tmp_path: Path) -> None:
    """配置不在项目根（没有 src/blue）时给空指纹，不炸。"""
    assert _load_sdk(tmp_path).source_hash == ""
