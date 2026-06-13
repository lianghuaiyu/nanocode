"""session.tree_view 纯逻辑单测——直接喂 Entry 列表,无 I/O。"""

from __future__ import annotations

from nanocode.session import tree_view as TM
from nanocode.session import tree as T
from nanocode.tui.session_pages.tree import SessionTreeModel


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
    assert "c-u" in model.extra_keys() and "c-a" in model.extra_keys()
    model.on_key("c-u", item, 0)
    assert model.mode == "user-only"
    model.on_key("c-u", item, 0)   # toggle back to default
    assert model.mode == "default"
    model.on_key("c-a", item, 0)
    assert model.mode == "all"
    assert model.on_key("L", item, 0).edit_action == "label"


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
    assert b.foldable                       # B 有两个子(C,E) → 可折叠
    n_before = len(rows)
    model.on_key("c-left", b, rows.index(b))   # 折叠 B
    ids = [r.entry.id for r in model.items()]
    assert "B" in ids and "C" not in ids and "E" not in ids
    b2 = next(r for r in model.items() if r.entry.id == "B")
    assert b2.folded
    model.on_key("c-right", b2, 0)             # 展开 B
    assert len(model.items()) == n_before


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
