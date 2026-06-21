"""Step 5:/sessions —— session.listing 纯逻辑 + _sessions 非交互文本路径。

交互 selector 需真 TTY,测纯逻辑(嵌套树/相对时间/详情/scope)与文本回退。
"""

from __future__ import annotations

import asyncio
import re

from nanocode.agent import AgentRuntime, AgentSession
from nanocode.agent.engine import Agent
from nanocode.entrypoints.commands.builtin import _resume, _session
from nanocode.entrypoints.commands.types import CommandContext, Local
from nanocode.session import listing as SM
from nanocode.session import search as SS
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager
from nanocode.tui.selector import cell_width
from nanocode.tui.session_pages.resume import ResumeSessionModel, _write_name


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _ANSI.sub("", text)


def _info(sid, parent=None, origin="root", modified=0.0, name=None, cwd="/x", n=1):
    return SM.SessionInfo(sid=sid, path=f"/sessions/{sid}/session.jsonl", name=name,
                          first_message=f"msg of {sid}", all_messages_text=f"msg of {sid} hi",
                          message_count=n, created=modified - 10 if modified else 0.0,
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


def test_search_phrase_regex_and_named_filter():
    infos = [
        _info("alpha", name="Build cache", modified=10),
        _info("beta", modified=20),
        _info("gamma", modified=30),
    ]
    infos[1].all_messages_text = "fix flaky tree view rendering"
    assert [i.sid for i in SS.filter_and_sort_sessions(infos, '"tree view"', "relevance")] == ["beta"]
    assert [i.sid for i in SS.filter_and_sort_sessions(infos, "re:build", "relevance")] == ["alpha"]
    assert [i.sid for i in SS.filter_and_sort_sessions(infos, "", "recent", "named")] == ["alpha"]


def test_search_invalid_regex_returns_empty():
    assert SS.filter_and_sort_sessions([_info("alpha")], "re:[", "relevance") == []


def test_resume_model_pi_style_ctrl_keys():
    model = ResumeSessionModel([_info("alpha", name="A")], None, "/x", "current", 1000)
    item = model.items()[0]
    assert model.search_line(80) == "> "
    assert model.position_line(0, 1, 0, 1, 80) is None
    assert "c-s" in model.extra_keys() and "r" not in model.extra_keys()
    model.on_key("c-s", item, 0)
    assert model.sort_mode == "recent"
    model.on_key("c-n", item, 0)
    assert model.name_filter == "named"
    model.on_key("c-p", item, 0)
    assert model.show_path is True
    assert model.on_key("c-r", item, 0).edit_action == "rename"


def test_resume_model_initial_index_prefers_current_session():
    infos = [_info("old", modified=10), _info("cur", modified=20), _info("new", modified=30)]
    model = ResumeSessionModel(infos, "cur", "/x", "current", 1000, sort_mode="recent")
    assert model.items()[model.initial_index()].info.sid == "cur"


def test_resume_model_rows_use_pi_cursor_and_cell_width_alignment():
    model = ResumeSessionModel([
        _info("alpha", name="中文标题很长很长", modified=1000, n=12),
    ], "alpha", "/x", "current", 1000)

    raw = model.list_text(model.items()[0], True, 36)
    selected = _plain(raw)
    idle = _plain(model.list_text(model.items()[0], False, 36))

    assert selected.startswith("› ")          # Pi accent 游标(U+203A)
    assert "\x1b[7m" not in raw               # 无反显
    assert cell_width(selected) == 36
    assert cell_width(idle) == 36
    assert selected.rstrip().endswith("12 now")


def test_resume_model_child_rows_use_spacious_box_prefix():
    infos = [
        _info("root", modified=100),
        _info("child_a", parent="root", origin="fork", modified=90),
        _info("child_b", parent="root", origin="fork", modified=80),
        _info("grand", parent="child_a", origin="fork", modified=70),
    ]
    model = ResumeSessionModel(infos, None, "/x", "current", 1000)
    child = next(i for i in model.items() if i.info.sid == "child_b")
    grand = next(i for i in model.items() if i.info.sid == "grand")

    child_rendered = _plain(model.list_text(child, False, 48))
    grand_rendered = _plain(model.list_text(grand, False, 48))

    assert child_rendered.startswith("    └─  ") or child_rendered.startswith("    ├─  ")
    assert grand_rendered.startswith("    │   └─  ") or grand_rendered.startswith("    │   ├─  ")
    assert "-> " not in child_rendered + grand_rendered
    assert cell_width(child_rendered) == 48
    assert cell_width(grand_rendered) == 48


def test_resume_model_pi_scroll_indicator_only_when_needed():
    infos = [_info(f"s{i}", modified=float(i)) for i in range(11)]
    model = ResumeSessionModel(infos, None, "/x", "current", 1000)
    assert model.position_line(0, 10, 0, 10, 80) is None
    assert model.position_line(5, 11, 1, 11, 80) == "  (6/11)"


def test_resume_model_pi_empty_messages():
    current = ResumeSessionModel([], None, "/x", "current", 1000)
    assert "No sessions in current folder" in current.empty_text(80)
    all_scope = ResumeSessionModel([], None, "/x", "all", 1000)
    assert all_scope.empty_text(80) == "  No sessions found"
    named = ResumeSessionModel([], None, "/x", "current", 1000, name_filter="named")
    assert "No named sessions in current folder" in named.empty_text(80)


def test_resume_delete_confirm_and_protect_current(monkeypatch):
    infos = [_info("CUR", cwd="/x", modified=100), _info("OTH", cwd="/x", modified=50)]
    model = ResumeSessionModel(infos, "CUR", "/x", "current", 1000)
    by = {i.info.sid: i for i in model.items()}
    assert "c-d" in model.extra_keys()
    # 禁删当前 session
    model.on_key("c-d", by["CUR"], 0)
    assert not model.confirming() and "cannot delete" in model.status
    # 非当前 → 进确认；confirm → 调 delete_session 并移除
    model.on_key("c-d", by["OTH"], 0)
    assert model.confirming()
    calls = []
    monkeypatch.setattr(SM, "delete_session", lambda sid: (calls.append(sid) or "session deleted"))
    model.on_key("confirm", by["OTH"], 0)
    assert calls == ["OTH"] and not model.confirming()
    assert "OTH" not in [i.info.sid for i in model.items()]


def test_resume_delete_abort_keeps_session():
    infos = [_info("CUR", cwd="/x", modified=100), _info("OTH", cwd="/x", modified=50)]
    model = ResumeSessionModel(infos, "CUR", "/x", "current", 1000)
    oth = {i.info.sid: i for i in model.items()}["OTH"]
    model.on_key("c-d", oth, 0)
    assert model.confirming()
    model.on_key("abort", oth, 0)
    assert not model.confirming()
    assert "OTH" in [i.info.sid for i in model.items()]


def test_resume_current_rename_uses_runtime_callback():
    calls = []

    err = _write_name("CUR", "CUR", "new name", rename_current=calls.append)

    assert err is None
    assert calls == ["new name"]


def test_scan_sessions_hides_unnamed_header_only_sessions():
    empty = SessionManager.create("EMPTYTOP")
    empty.close()
    child_empty = SessionManager.create("EMPTYCHILD", parent_session={"sessionId": "P", "entryId": "x"})
    child_empty.close()
    named = SessionManager.create("NAMEDEMPTY")
    named.append_session_info("keep me")
    named.close()

    infos = SM.scan_sessions()
    ids = {i.sid for i in infos}

    assert "EMPTYTOP" not in ids
    assert "EMPTYCHILD" not in ids
    assert "NAMEDEMPTY" in ids
    assert all(i.first_message != "(no messages)" for i in infos)


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
    ctx = CommandContext(thread=AgentRuntime()._attach_agent(a), interactive=False)
    res = asyncio.run(_resume(ctx, ""))
    out = res.output or ""                       # docs/18 step 5：命令返回结构化 output（不再 print）
    assert isinstance(res, Local)
    assert "Resumable sessions" in out
    assert "ROOTSID"[-8:] in out and "CHILDSID"[-8:] in out
    assert "fork" in out


def test_resume_current_session_requests_transcript_refresh():
    sid = "CURRESUM"
    mgr = SessionManager.create(sid)
    mgr.append_message(T.user_message("current convo"))
    mgr.close()

    a = Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")
    a._session_mgr = SessionManager.open(sid)
    ctx = CommandContext(thread=AgentRuntime()._attach_agent(a), interactive=False)

    res = asyncio.run(_resume(ctx, sid))

    assert isinstance(res, Local)
    assert res.refresh_transcript is True


def test_session_shows_current_stats():
    a = Agent(api_key="test", session_id="CURSTAT", permission_mode="bypassPermissions")
    mgr = SessionManager.create("CURSTAT")
    a._session_mgr = mgr
    mgr.append_message(T.user_message("hi"))
    ctx = CommandContext(thread=AgentRuntime()._attach_agent(a), interactive=False)
    res = asyncio.run(_session(ctx, ""))
    out = res.output or ""
    assert isinstance(res, Local)
    assert "Session CURSTAT" in out
    assert "origin   root" in out
    assert "entries  1 messages" in out
