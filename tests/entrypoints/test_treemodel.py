"""session.tree_view 纯逻辑单测——直接喂 Entry 列表,无 I/O。"""

from __future__ import annotations

import re

from nanocode.session import tree_view as TM
from nanocode.session import tree as T
from nanocode.tui.selector import cell_width
from nanocode.tui.session_pages.tree import SessionTreeModel


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _ANSI.sub("", text)


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


def test_linear_parent_child_chain_is_flat_like_pi():
    entries, leaf = _fork_tree()
    rows = TM.build_rows(entries[:5], leaf)  # start -> A -> B -> C -> D, no side branch
    assert [r.prefix for r in rows] == ["", "", "", ""]


def test_branching_parent_child_chain_shows_tree_like_pi():
    entries, leaf = _fork_tree()
    rows = TM.build_rows(entries, leaf)
    by = {r.entry.id: r for r in rows}
    assert by["A"].prefix == ""
    assert by["B"].prefix == ""
    assert by["C"].prefix.startswith("├")
    assert by["D"].prefix.startswith("│")
    assert by["E"].prefix.startswith("└")
    assert by["F"].prefix.startswith(" ")


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


def test_entry_detail_lines_include_structure_and_preview():
    entries, leaf = _fork_tree()
    row = {r.entry.id: r for r in TM.build_rows(entries, leaf)}["D"]
    lines = TM.entry_detail_lines(row)
    joined = "\n".join(lines)
    assert "type     message:assistant" in joined
    assert "id       D" in joined
    assert "parent   C" in joined
    assert "branch   active path · leaf" in joined
    assert "preview\nassistant: 当前分支" in joined or "preview\n当前分支" in joined


def test_tree_model_filter_chords_and_label():
    entries, leaf = _fork_tree()
    model = SessionTreeModel(entries, leaf, "default")
    item = model.items()[0]
    header = "\n".join(model.header_lines(100))
    assert "Session Tree" in header
    assert "leaf" not in header and "filter:" not in header
    assert model.search_line(80).endswith("Type to search:\x1b[0m")
    assert model.body_border_after_search()
    assert model.position_line(0, len(model.items()), 0, 5, 80) == f"  (1/{len(model.items())})"
    assert model.wrap_navigation()
    assert model.escape_clears_query()
    assert model.extra_keys() == ("c-o", "c-left", "c-right", "L", "T")
    model.on_key("c-o", item, 0)
    assert model.mode == "no-tools"
    assert model.status_suffix() == " [no-tools]"
    assert model.on_key("L", item, 0).edit_action == "label"
    assert model.on_key("T", item, 0).kind == "refresh"


def test_tree_model_initial_index_prefers_current_leaf():
    entries, leaf = _fork_tree()
    model = SessionTreeModel(entries, leaf, "default")
    assert model.items()[model.initial_index()].entry.id == leaf


def test_tree_model_rows_use_pi_cursor_and_cell_width():
    entries, leaf = _fork_tree()
    model = SessionTreeModel(entries, leaf, "default")
    row = next(r for r in model.items() if r.entry.id == "D")

    selected = _plain(model.list_text(row, True, 32))
    nonsel = _plain(model.list_text(row, False, 32))

    assert selected.startswith("› ")          # Pi accent 游标(U+203A),非 ASCII '>'
    assert "\x1b[7m" not in model.list_text(row, True, 32)   # 无反显
    assert selected.rstrip().endswith("◀ current")
    assert nonsel.startswith("  ")             # 非选中:两空格占位,cell 对齐
    assert cell_width(nonsel) <= 32


def test_tree_model_live_search_filters():
    entries, leaf = _fork_tree()
    model = SessionTreeModel(entries, leaf, "default")
    assert model.supports_query()          # 始终可搜（无 / 开关）
    model.set_query("轻量")
    assert [r.entry.id for r in model.items()] == ["E"]
    model.set_query("")
    assert len(model.items()) > 1


def test_tree_model_fold_hides_descendants():
    entries, leaf = _fork_tree()
    model = SessionTreeModel(entries, leaf, "default")
    rows = model.items()
    b = next(r for r in rows if r.entry.id == "B")
    assert not b.foldable                   # Pi: branch point itself jumps to the segment start
    jump = model.on_key("c-right", b, rows.index(b))
    assert jump.result == rows.index(next(r for r in rows if r.entry.id == "C"))
    c = next(r for r in rows if r.entry.id == "C")
    assert c.foldable                       # C 是 B 分叉后的 segment start
    n_before = len(rows)
    model.on_key("c-left", c, rows.index(c))   # 折叠 C
    ids = [r.entry.id for r in model.items()]
    assert "C" in ids and "D" not in ids and "E" in ids
    c2 = next(r for r in model.items() if r.entry.id == "C")
    assert c2.folded
    model.on_key("c-right", c2, 0)             # 展开 C
    assert len(model.items()) == n_before


def test_tree_model_foldability_recomputed_after_search():
    start = T.Entry(type=T.SESSION_START, id="start", parentId=None, sessionId="s",
                    timestamp="t", data={"cwd": "/x"})
    a = _msg("A", "start", "user", "root")
    b = _msg("B", "A", "assistant", "hidden")
    c = _msg("C", "B", "user", "keep parent")
    d = _msg("D", "C", "user", "keep child")
    model = SessionTreeModel([start, a, b, c, d], "D", "default")

    model.set_query("keep")
    rows = model.items()
    c_row = next(r for r in rows if r.entry.id == "C")
    assert [r.entry.id for r in rows] == ["C", "D"]
    assert c_row.foldable

    model.on_key("c-left", c_row, rows.index(c_row))
    assert [r.entry.id for r in model.items()] == ["C"]


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


def test_fork_model_lists_user_messages_newest_first_and_searches():
    from nanocode.tui.session_pages.fork import ForkModel
    entries, leaf = _fork_tree()
    model = ForkModel(entries)
    ids = [e.id for e in model.items()]
    assert set(ids) == {"A", "C", "E"}      # 仅 user 消息
    assert ids == ["E", "C", "A"]           # newest-first
    model.set_query("初始化")
    assert [e.id for e in model.items()] == ["A"]


def test_fork_model_initial_index_prefers_current_branch_user():
    from nanocode.tui.session_pages.fork import ForkModel
    entries, leaf = _fork_tree()
    model = ForkModel(entries, leaf_id=leaf)
    assert model.items()[model.initial_index()].id == "C"


def test_fork_model_rows_use_pi_cursor_and_cell_width():
    from nanocode.tui.session_pages.fork import ForkModel
    entries, leaf = _fork_tree()
    model = ForkModel(entries)

    raw = model.list_text(model.items()[0], True, 18)
    selected = _plain(raw)
    nonsel = _plain(model.list_text(model.items()[0], False, 18))

    assert selected.startswith("› ")          # Pi accent 游标
    assert "\x1b[7m" not in raw                # 无反显
    assert nonsel.startswith("  ")
    assert cell_width(nonsel) <= 18
