"""treemodel 纯逻辑单测(entrypoints/interactive/treemodel.py)——直接喂 Entry 列表,无 I/O。"""

from __future__ import annotations

from nanocode.entrypoints.interactive import treemodel as TM
from nanocode.session import tree as T


def _msg(eid, parent, role, text):
    return T.Entry(type=T.MESSAGE, id=eid, parentId=parent, sessionId="s", timestamp="t",
                   data={"message": {"role": role, "content": text}})


def _fork_tree():
    """A→B 后分叉:C(active,其下 D=leaf) 与 E(其下 F)。返回 (entries, leaf_id=D)。
        A user
        └ B assistant
          ├ C user      (active)
          │ └ D asst ◀ leaf
          └ E user
            └ F asst
    """
    start = T.Entry(type=T.SESSION_START, id="start", parentId=None, sessionId="s",
                    timestamp="t", data={"cwd": "/x"})
    A = _msg("A", "start", "user", "初始化")
    B = _msg("B", "A", "assistant", "读取 pyproject")
    C = _msg("C", "B", "user", "先做 tree")
    D = _msg("D", "C", "assistant", "当前分支")
    E = _msg("E", "B", "user", "轻量实现")
    F = _msg("F", "E", "assistant", "旧分支")
    return [start, A, B, C, D, E, F], "D"


def _ids(rows):
    return [r.entry.id for r in rows]


def test_session_start_not_a_node():
    entries, leaf = _fork_tree()
    rows = TM.build_rows(entries, leaf)
    assert "start" not in _ids(rows)  # header 不入树


def test_active_branch_sorted_first():
    entries, leaf = _fork_tree()
    rows = TM.build_rows(entries, leaf)
    ids = _ids(rows)
    # C(active) 分支整体排在 E 分支之前
    assert ids.index("C") < ids.index("E")
    assert ids.index("D") < ids.index("E")


def test_active_path_and_leaf_markers():
    entries, leaf = _fork_tree()
    rows = TM.build_rows(entries, leaf)
    by = {r.entry.id: r for r in rows}
    assert by["D"].is_leaf and not by["C"].is_leaf
    for i in ("A", "B", "C", "D"):
        assert by[i].on_active_path, i
    for i in ("E", "F"):
        assert not by[i].on_active_path, i


def test_connectors_present_at_branch():
    entries, leaf = _fork_tree()
    rows = TM.build_rows(entries, leaf)
    by = {r.entry.id: r for r in rows}
    # C 与 E 是 B 的两个子 → 应有连接线
    assert "├" in by["C"].prefix or "└" in by["C"].prefix
    assert "├" in by["E"].prefix or "└" in by["E"].prefix


def test_filter_user_only():
    entries, leaf = _fork_tree()
    rows = TM.build_rows(entries, leaf, mode="user-only")
    assert set(_ids(rows)) == {"A", "C", "E"}


def test_filter_labeled_only_and_label_shown():
    entries, leaf = _fork_tree()
    entries.append(T.Entry(type=T.LABEL, id="lbl1", parentId="D", sessionId="s", timestamp="t",
                           data={"targetId": "C", "label": "起点"}))
    rows = TM.build_rows(entries, leaf, mode="labeled-only")
    assert _ids(rows) == ["C"]
    assert rows[0].label == "起点"


def test_content_preview():
    entries, leaf = _fork_tree()
    rows = TM.build_rows(entries, leaf)
    by = {r.entry.id: r for r in rows}
    assert by["A"].content == "user: 初始化"
    assert by["D"].content == "assistant: 当前分支"


def test_render_text_has_current_marker():
    entries, leaf = _fork_tree()
    lines = TM.render_tree_text(entries, leaf)
    joined = "\n".join(lines)
    assert "◀ current" in joined
    # leaf 行带 current 标记
    leaf_line = [ln for ln in lines if "当前分支" in ln][0]
    assert "◀ current" in leaf_line


def test_empty_tree():
    assert TM.render_tree_text([], None) == ["  (no entries)"]
