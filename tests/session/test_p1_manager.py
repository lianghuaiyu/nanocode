"""P1 SessionManager 单测：create/append/leaf/branch/build_context/fork/clone/torn-line/导航。

验证盘上 JSONL ↔ 内存 parity、leaf 进日志折叠重建、child header 回指导航。
"""

import json

from nanocode.session import tree
from nanocode.session.manager import (SessionManager, children, parent_of, session_file, siblings)
from nanocode.session.render import ModelCtx, render


def test_create_writes_session_start_root_and_leaf():
    m = SessionManager.create("sA", cwd="/tmp/x")
    root = m.entries()[0]
    assert root.type == tree.SESSION_START and root.parentId is None
    assert root.data["cwd"] == "/tmp/x"
    assert m.get_leaf() is None  # docs/14：session_start 是 header，不推进 leaf；空会话 leaf=None
    # 盘上确有一行
    assert session_file("sA").exists()
    lines = session_file("sA").read_text().strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["type"] == "session_start"


def test_append_advances_leaf_and_parent_chain():
    m = SessionManager.create("sB")
    root = m.get_leaf()
    e1 = m.append_message(tree.user_message("hi"))
    assert e1.parentId == root and m.get_leaf() == e1.id
    e2 = m.append_message(tree.assistant_message([tree.text_block("yo")], provider="anthropic",
                          api="anthropic", model="claude-x", stop_reason="stop"))
    assert e2.parentId == e1.id and m.get_leaf() == e2.id


def test_empty_session_has_no_leaf_and_empty_context():
    # docs/14 §4.1 acceptance：空会话 leaf=None、context 空；session_start 仅 header、不入 branch。
    m = SessionManager.create("sEmpty")
    assert m.get_leaf() is None
    assert m.get_branch() == []
    assert m.build_context().messages == []
    u = m.append_message(tree.user_message("hi"))     # 首条消息成为 branch root
    assert u.parentId is None
    assert [e.type for e in m.get_branch()] == [tree.MESSAGE]   # branch 不含 session_start


def test_set_leaf_is_logged_and_refold_recovers():
    m = SessionManager.create("sC")
    a = m.append_message(tree.user_message("a"))   # a.parentId=None（branch root；header 不当父）
    assert m.get_leaf() == a.id
    m.set_leaf(None)                       # rewind 到空上下文（写一条 leaf targetId=None）
    assert m.get_leaf() is None
    b = m.append_message(tree.user_message("b"))  # 在空之上派生 sibling 分支
    assert b.parentId is None
    # reopen：leaf 从日志折叠重建（无 state.json 权威）
    m2 = SessionManager.open("sC")
    assert m2.get_leaf() == b.id
    assert [e.id for e in m2.get_branch()] == [b.id]   # a 不在 b 的 branch 上（b.parentId=None）
    assert a.id in {e.id for e in m2.entries()}        # a 仍在文件里（非破坏性）


def test_build_context_feeds_render():
    m = SessionManager.create("sD")
    m.append_message(tree.user_message("hi"))
    m.append_message(tree.assistant_message(
        [tree.text_block("ok"), tree.tool_call_block("t1", "run", {})],
        provider="anthropic", api="anthropic", model="claude-x", stop_reason="toolUse"))
    m.append_message(tree.tool_result_message(tool_call_id="t1", tool_name="run", content="done"))
    ctx = m.build_context()
    assert [mm["role"] for mm in ctx.messages] == ["user", "assistant", "toolResult"]
    payload = render(ctx.messages, ModelCtx("anthropic", "anthropic", "claude-x"))
    roles = [mm["role"] for mm in payload["messages"]]
    assert roles == ["user", "assistant", "user"]  # toolResult → anthropic user 消息


def test_jsonl_memory_parity_on_reopen():
    m = SessionManager.create("sE")
    for i in range(3):
        m.append_message(tree.user_message(f"m{i}"))
    before = [e.to_dict() for e in m.entries()]
    after = [e.to_dict() for e in SessionManager.open("sE").entries()]
    assert before == after


def test_torn_last_line_tolerated():
    m = SessionManager.create("sF")
    m.append_message(tree.user_message("ok"))
    # 模拟崩溃半写：追加一行残缺 JSON
    with session_file("sF").open("a", encoding="utf-8") as f:
        f.write('{"id":"ent_partial","type":"mess')   # 无换行、残缺
    m2 = SessionManager.open("sF")          # 不抛，丢弃残缺末行
    assert all(e.id != "ent_partial" for e in m2.entries())
    assert m2.get_leaf() is not None


def test_clone_copies_path_to_root_with_parent_link():
    src = SessionManager.create("sG")
    a = src.append_message(tree.user_message("a"))
    b = src.append_message(tree.user_message("b"))
    child = src.clone(b.id, new_session_id="sG_clone")
    # parentSession 回指
    ps = child.parent_session()
    assert ps == {"sessionId": "sG", "entryId": b.id}
    # 复制了 path-to-root 的消息（a、b），但有自己的 session_start
    contents = [e.data["message"]["content"] for e in child.entries() if e.type == tree.MESSAGE]
    assert contents == ["a", "b"]
    assert child.entries()[0].type == tree.SESSION_START and child.entries()[0].sessionId == "sG_clone"


def test_cross_session_navigation_via_header_backref():
    parent = SessionManager.create("P")
    SessionManager.create("C1", parent_session={"sessionId": "P", "entryId": "x"})
    SessionManager.create("C2", parent_session={"sessionId": "P", "entryId": "y"})
    SessionManager.create("Other")  # 无 parent
    assert children("P") == ["C1", "C2"]
    assert parent_of("C1") == "P"
    assert siblings("C1") == ["C2"]
    assert children("Other") == []
