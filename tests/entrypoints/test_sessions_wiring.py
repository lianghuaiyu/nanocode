"""Step 5:/sessions —— sessionmodel 纯逻辑 + _sessions 非交互文本路径。

交互 selector 需真 TTY,测纯逻辑(嵌套树/相对时间/详情/scope)与文本回退。
"""

from __future__ import annotations

import asyncio

from nanocode.agent import AgentSession
from nanocode.agent.engine import Agent
from nanocode.entrypoints.commands.builtin import _resume, _session
from nanocode.entrypoints.commands.types import CommandContext, Local
from nanocode.entrypoints.interactive import sessionmodel as SM
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager


def _info(sid, parent=None, origin="root", modified=0.0, name=None, cwd="/x", n=1):
    return SM.SessionInfo(sid=sid, name=name, first_message=f"msg of {sid}", message_count=n,
                          modified=modified, cwd=cwd, parent_sid=parent, origin=origin,
                          leaf="L"+sid, latest_role="user", latest_text="hi")


def test_build_session_tree_nests_children_under_parent():
    infos = [_info("a7f3", modified=100), _info("b91c", parent="a7f3", origin="fork", modified=200),
             _info("c42d", parent="a7f3", origin="clone", modified=300)]
    roots = SM.build_session_tree(infos)
    assert len(roots) == 1 and roots[0].info.sid == "a7f3"
    kids = [c.info.sid for c in roots[0].children]
    assert set(kids) == {"b91c", "c42d"}
    # 子按 modified 降序
    assert kids == ["c42d", "b91c"]


def test_flatten_and_prefix_depths():
    infos = [_info("a7f3", modified=100), _info("b91c", parent="a7f3", modified=200)]
    flats = SM.flatten_session_tree(SM.build_session_tree(infos))
    assert [f.info.sid for f in flats] == ["a7f3", "b91c"]
    assert SM.tree_prefix(flats[0]) == ""
    assert "└" in SM.tree_prefix(flats[1]) or "├" in SM.tree_prefix(flats[1])


def test_format_session_date():
    assert SM.format_session_date(1000, 1000) == "now"
    assert SM.format_session_date(1000, 1000 + 5 * 60) == "5m"
    assert SM.format_session_date(1000, 1000 + 2 * 3600) == "2h"
    assert SM.format_session_date(1000, 1000 + 3 * 86400) == "3d"


def test_filter_by_scope():
    infos = [_info("a", cwd="/p1"), _info("b", cwd="/p2")]
    assert {i.sid for i in SM.filter_by_scope(infos, "current", "/p1")} == {"a"}
    assert {i.sid for i in SM.filter_by_scope(infos, "all", "/p1")} == {"a", "b"}


def test_session_detail_lines():
    lines = SM.session_detail_lines(_info("c42d", parent="a7f3", origin="clone", n=18))
    joined = "\n".join(lines)
    assert "c42d" in joined and "origin   clone" in joined and "entries  18" in joined


def test_resume_no_arg_non_interactive_nests(tmp_path, monkeypatch):
    # 建 root + fork 子 session,/resume 无参非交互 → 按 parent 嵌套的文本列表(render_sessions_text)
    root = SessionManager.create("ROOTSID")
    root.append_message(T.user_message("root convo"))
    root.close()
    child = SessionManager.create("CHILDSID", parent_session={"sessionId": "ROOTSID",
                                  "forkedBeforeEntryId": "x"})
    child.append_message(T.user_message("child convo"))
    child.close()

    a = Agent(api_key="test", session_id="ROOTSID", permission_mode="bypassPermissions")
    a._session_mgr = SessionManager.open("ROOTSID")
    ctx = CommandContext(agent=a, session=AgentSession(a), out=a._sink, interactive=False)
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        res = asyncio.run(_resume(ctx, ""))
    out = buf.getvalue()
    assert isinstance(res, Local)
    assert "Resumable sessions" in out
    assert "ROOTSID"[-8:] in out and "CHILDSID"[-8:] in out
    assert "fork" in out


def test_session_shows_current_stats(capsys):
    a = Agent(api_key="test", session_id="CURSTAT", permission_mode="bypassPermissions")
    mgr = SessionManager.create("CURSTAT")
    a._session_mgr = mgr
    mgr.append_message(T.user_message("hi"))
    ctx = CommandContext(agent=a, session=AgentSession(a), out=a._sink, interactive=False)
    res = asyncio.run(_session(ctx, ""))
    out = capsys.readouterr().out
    assert isinstance(res, Local)
    assert "Session CURSTAT" in out
    assert "origin   root" in out
    assert "entries  1 messages" in out
