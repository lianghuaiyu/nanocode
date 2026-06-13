"""Step 3 接线测试:/tree 非交互文本路径 + manager.append_label。

selector(Application)需真 TTY,无法在测试里跑;此处只验证非交互(默认 interactive=False)
的文本回退与 label 写入语义。交互路径的纯逻辑已由 test_treemodel 覆盖。
"""

from __future__ import annotations

import asyncio

from nanocode.agent.engine import Agent
from nanocode.entrypoints.commands.builtin import _tree
from nanocode.entrypoints.commands.types import CommandContext, Local
from nanocode.agent import AgentSession
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager


def _agent(sid):
    return Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")


def _ctx(a, interactive=False):
    return CommandContext(agent=a, session=AgentSession(a), out=a._sink, interactive=interactive)


def test_append_label_writes_and_reads_back():
    a = _agent("LBL1")
    mgr = SessionManager.create("LBL1")
    a._session_mgr = mgr
    u = mgr.append_message(T.user_message("hello"))
    mgr.append_label(u.id, "起点")
    assert mgr.labels().get(u.id) == "起点"
    # 空白 = tombstone 清除
    mgr.append_label(u.id, "")
    assert u.id not in mgr.labels()


def test_append_label_does_not_move_leaf():
    a = _agent("LBL2")
    mgr = SessionManager.create("LBL2")
    a._session_mgr = mgr
    u = mgr.append_message(T.user_message("hi"))
    mgr.append_label(u.id, "x")
    assert mgr.get_leaf() == u.id  # label 是注解型,不推进 leaf


def test_tree_no_arg_non_interactive_prints_text_tree(capsys):
    a = _agent("TREETXT")
    mgr = SessionManager.create("TREETXT")
    a._session_mgr = mgr
    u = mgr.append_message(T.user_message("初始化项目"))
    mgr.append_message(T.assistant_message([T.text_block("好的")], provider="anthropic",
                       api="anthropic", model="claude-x", stop_reason="stop"))
    res = asyncio.run(_tree(_ctx(a, interactive=False), ""))
    out = capsys.readouterr().out
    assert isinstance(res, Local)
    assert "session tree [TREETXT]" in out
    assert "user: 初始化项目" in out
    assert "assistant: 好的" in out
    assert "◀ current" in out  # leaf 标记


def test_tree_no_canonical_tree(capsys):
    a = _agent("NOTREE")
    # 不建树,不设 _session_mgr
    a._session_mgr = None
    res = asyncio.run(_tree(_ctx(a), ""))
    assert isinstance(res, Local)
    assert "No canonical session tree" in capsys.readouterr().out
