"""对话树几何测试：孩子表、当前链、路径、子树与非法结构。"""

from __future__ import annotations

import pytest

from blue.tree import ROOT_ID, Tree, TreeError, TreeNode


def _branched() -> list[TreeNode]:
    """u1 下挂两条分支：a1（旧）、a2（新，当前选中）。"""
    return [
        TreeNode(1, ROOT_ID, 3),
        TreeNode(2, 1),
        TreeNode(3, 1),
    ]


def test_root_current_points_at_the_visible_chain() -> None:
    """当前链从根指针开始，沿 current 走到底。"""
    tree = Tree(_branched(), root_current=1)
    assert tree.chain() == [1, 3]
    assert tree.leaf() == 3


def test_no_messages_means_empty_chain() -> None:
    """还没发过消息：链为空，末端也是 None。"""
    tree = Tree[TreeNode]([], root_current=None)
    assert tree.chain() == []
    assert tree.leaf() is None


def test_children_and_siblings_keep_creation_order() -> None:
    """孩子按 id（创建顺序）排列，兄弟含自己。"""
    tree = Tree(_branched(), root_current=1)
    assert tree.children(1) == [2, 3]
    assert tree.children(ROOT_ID) == [1]
    assert tree.siblings(3) == [2, 3]
    assert tree.siblings(99) == []


def test_chain_to_ignores_selected_branch_below_the_node() -> None:
    """以某条消息为父取历史时，只看根到它的路径，与它的后续分支无关。"""
    tree = Tree(_branched(), root_current=1)
    assert tree.chain_to(1) == [1]
    assert tree.chain_to(2) == [1, 2]
    # 当前选中 3，但 3 自己的路径不会混进 2 的历史
    assert tree.chain_to(3) == [1, 3]
    assert tree.chain_to(ROOT_ID) == []
    assert tree.chain_to(999) == []


def test_switching_current_changes_only_the_chain() -> None:
    """切换分支只改指针：另一条分支仍然完整保留。"""
    visible_new = Tree(_branched(), root_current=1)
    visible_old = Tree(
        [TreeNode(1, ROOT_ID, 2), TreeNode(2, 1), TreeNode(3, 1)],
        root_current=1,
    )
    assert visible_new.chain() == [1, 3]
    assert visible_old.chain() == [1, 2]
    # 两条链共用同一棵树：换指针只是「看哪条」，不动任何节点
    assert visible_new.subtree(1) == visible_old.subtree(1) == [1, 2, 3]


def test_subtree_covers_all_descendants() -> None:
    """子树含自身与全部后代，与当前选中哪条无关。"""
    tree = Tree(
        [
            TreeNode(1, ROOT_ID, 2),
            TreeNode(2, 1, 4),
            TreeNode(3, 1),
            TreeNode(4, 2),
            TreeNode(5, ROOT_ID),
        ],
        root_current=1,
    )
    assert tree.subtree(1) == [1, 2, 3, 4]
    assert tree.subtree(2) == [2, 4]
    assert tree.subtree(5) == [5]
    assert tree.subtree(999) == []


def test_deep_chain_and_leaf() -> None:
    """长链：每一步都只有一个孩子，链长度等于消息数。"""
    nodes = [TreeNode(1, ROOT_ID, 2), TreeNode(2, 1, 3), TreeNode(3, 2, 4)]
    nodes.append(TreeNode(4, 3, 5))
    nodes.append(TreeNode(5, 4))
    tree = Tree(nodes, root_current=1)
    assert tree.chain() == [1, 2, 3, 4, 5]
    assert tree.leaf() == 5


def test_current_pointing_at_a_stranger_stops_the_walk() -> None:
    """current 指向不属于自己的孩子：链在那里停下，不会跑偏。"""
    tree = Tree([TreeNode(1, ROOT_ID, 7), TreeNode(2, 1)], root_current=1)
    assert tree.chain() == [1]


def test_cycles_do_not_hang() -> None:
    """环（脏数据）不会让遍历死循环。"""
    tree = Tree(
        [TreeNode(1, 2, 2), TreeNode(2, 1, 1)],
        root_current=1,
    )
    assert tree.chain() == [1, 2]
    assert sorted(tree.chain_to(1)) == [1, 2]


def test_duplicate_ids_are_rejected() -> None:
    """重复 id 是编程错误，直接报出来。"""
    with pytest.raises(TreeError):
        Tree([TreeNode(1, ROOT_ID), TreeNode(1, ROOT_ID)])


def test_require_reports_missing_nodes() -> None:
    """写路径上拿不存在的节点当挂载点必须显式失败。"""
    tree = Tree([TreeNode(1, ROOT_ID)], root_current=1)
    assert tree.require(1).id == 1
    with pytest.raises(TreeError):
        tree.require(2)


def test_collect_skips_unknown_ids() -> None:
    """按链取对象时，脏 id 跳过而不是连坐整条链。"""
    tree = Tree([TreeNode(1, ROOT_ID, 2), TreeNode(2, 1)], root_current=1)
    assert [node.id for node in tree.collect([1, 99, 2])] == [1, 2]
    assert len(tree) == 2


def test_pick_current_prefers_newest_and_clears_when_empty() -> None:
    """删掉分支后父节点该选中谁：剩下最年轻的；一个都不剩就是 None。"""
    from blue.tree import pick_current

    assert pick_current([2, 3, 4]) == 4
    assert pick_current([]) is None
