"""对话树：会话消息的树形几何，纯计算、无 I/O。

会话不再是一条线性表：每条消息挂在父消息下，父节点用 ``current`` 记住「当前
查看的是哪个子分支」。沿 ``current`` 从根一路走到底，就是界面显示、也是发给
模型的那条链；在同一个父节点下再挂一个孩子，就得到一条新分支，旧分支原样保留。

本模块只回答几何问题（谁是孩子、当前链是哪条、子树有哪些），不碰消息内容，
也不碰存储。session 服务与 agent 共用同一份实现，避免两边各算一套。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from blue.errors import BlueError

__all__ = [
    "ROOT_ID",
    "Tree",
    "TreeError",
    "TreeNode",
    "TreeNodeView",
    "pick_current",
]

ROOT_ID = 0
"""会话根的虚拟节点 id。真实消息的 id 从 1 开始，所以 0 永远可以当哨兵。"""


class TreeError(BlueError):
    """树结构非法（重复 id、挂载点不存在、出现环）。"""


class TreeNodeView(Protocol):
    """参与树运算的最小形状：任何带这三个属性的对象都能直接进树。

    用只读 property 声明，字段（frozen dataclass、pydantic 模型）都能满足它。
    """

    @property
    def id(self) -> int: ...
    @property
    def parent(self) -> int: ...
    @property
    def current(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class TreeNode:
    """可直接构造的树节点（测试与轻量调用方使用）。"""

    id: int
    parent: int = ROOT_ID
    current: int | None = None


def pick_current(children: Sequence[int]) -> int | None:
    """孩子里该选中谁：最后一个（最新分支）；没有孩子就不选中。"""
    return children[-1] if children else None


class Tree[NodeT: TreeNodeView]:
    """会话树的只读视图。

    节点只要求有 ``id`` / ``parent`` / ``current`` 三个属性（见
    :class:`TreeNodeView`），所以协议模型、数据库记录、测试用的
    :class:`TreeNode` 都能直接塞进来，不用中转转换。

    结构异常（父节点不存在、出现环）不抛错：这样的节点只是从根不可达，
    查询自然看不到它。只有「拿一个不存在的节点当挂载点」这种写路径上的
    错误才由 :meth:`require` 抛 :class:`TreeError`。
    """

    def __init__(
        self, nodes: Iterable[NodeT], *, root_current: int | None = None
    ) -> None:
        """索引节点并预计算孩子表。

        :param nodes: 会话里的全部消息节点。
        :param root_current: 根当前选中的顶层分支；``None`` 表示从未发过消息。
        :raises TreeError: id 重复。
        """
        index: dict[int, NodeT] = {}
        for node in nodes:
            if node.id in index:
                raise TreeError(f"节点 id 重复：{node.id}")
            index[node.id] = node
        children: dict[int, list[int]] = {}
        # 按 id 升序遍历 → 每个父节点的孩子表天然是创建顺序
        for node_id in sorted(index):
            children.setdefault(index[node_id].parent, []).append(node_id)
        self._index = index
        self._children = children
        self._root_current = root_current

    @property
    def root_current(self) -> int | None:
        """根当前选中的顶层分支。"""
        return self._root_current

    def __len__(self) -> int:
        """节点总数（不含根）。"""
        return len(self._index)

    def get(self, node_id: int) -> NodeT | None:
        """按 id 取节点；不存在返回 ``None``。"""
        return self._index.get(node_id)

    def require(self, node_id: int) -> NodeT:
        """按 id 取节点。

        :raises TreeError: 节点不存在——写路径上说明挂载点或目标已过期。
        """
        node = self._index.get(node_id)
        if node is None:
            raise TreeError(f"节点不存在：{node_id}")
        return node

    def children(self, node_id: int = ROOT_ID) -> list[int]:
        """某个节点的孩子（创建顺序）；``ROOT_ID`` 表示顶层分支。"""
        return list(self._children.get(node_id, ()))

    def siblings(self, node_id: int) -> list[int]:
        """同父的兄弟节点（含自己，创建顺序）；节点不存在时返回空列表。"""
        node = self._index.get(node_id)
        if node is None:
            return []
        return self.children(node.parent)

    def chain(self) -> list[int]:
        """当前链：从根沿 ``current`` 走到叶，返回节点 id（早 → 新）。"""
        return self._walk(self._root_current)

    def chain_to(self, node_id: int) -> list[int]:
        """从根到指定节点的路径（含该节点，早 → 新）。

        这是「以某条消息为父继续生成」时要发给模型的历史；与当前选中的分支
        无关——即使该节点的后续另有一个被选中的兄弟分支。
        """
        path: list[int] = []
        seen: set[int] = set()
        current: int = node_id
        while current != ROOT_ID and current in self._index and current not in seen:
            seen.add(current)
            path.append(current)
            current = self._index[current].parent
        path.reverse()
        return path

    def leaf(self) -> int | None:
        """当前链的末端（下一次发言的默认挂载点）。"""
        chain = self.chain()
        return chain[-1] if chain else None

    def subtree(self, node_id: int) -> list[int]:
        """该节点及其全部后代（升序）；节点不存在时返回空列表。"""
        if node_id not in self._index:
            return []
        found: set[int] = set()
        stack = [node_id]
        while stack:
            current = stack.pop()
            if current in found:
                continue
            found.add(current)
            stack.extend(self._children.get(current, ()))
        return sorted(found)

    def collect(self, ids: Iterable[int]) -> list[NodeT]:
        """按给定的 id 顺序取出节点对象，跳过不存在的 id。"""
        return [self._index[node_id] for node_id in ids if node_id in self._index]

    def _walk(self, start: int | None) -> list[int]:
        """从 ``start`` 沿 ``current`` 往下走，防环。"""
        path: list[int] = []
        seen: set[int] = set()
        current = start
        while current is not None and current not in seen:
            node = self._index.get(current)
            if node is None:
                break
            seen.add(current)
            path.append(current)
            current = node.current
        return path
