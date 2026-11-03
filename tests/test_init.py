"""`blue` 包入口的冒烟测试。"""

import blue


def test_package_exposes_docstring() -> None:
    """包可以被导入，且带有模块级说明。"""
    assert blue.__doc__ is not None
