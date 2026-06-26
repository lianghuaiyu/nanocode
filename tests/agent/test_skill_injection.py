import asyncio
from nanocode.agent.engine import Agent
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager

from .._helpers import attach_runtime_agent


def _custom_msgs(mgr, kind=None):
    return [e.data.get("content", "") for e in mgr.entries() if e.type == T.CUSTOM_MESSAGE
            and (kind is None or e.data.get("customType") == kind)]


def _agent():
    return Agent(api_key="test")


def test_inject_listing_then_silent(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    d = tmp_path / ".nanocode" / "skills" / "k1"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: k1\ndescription: kk\n---\nbody")
    monkeypatch.chdir(tmp_path)
    discovery.reset_skill_cache()
    a = _agent()
    a._session_mgr = SessionManager.create("ski_listing")
    a.agent_session.inject_skill_listing()
    cms = _custom_msgs(a._session_mgr, "skill_listing")
    assert len(cms) == 1 and "<system-reminder>" in cms[0] and "k1" in cms[0]
    # 第二次无新增 → 不再注入（dedup 已推进）
    a.agent_session.inject_skill_listing()
    assert len(_custom_msgs(a._session_mgr, "skill_listing")) == 1


def test_inject_listing_skipped_for_subagent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = Agent(api_key="test", is_sub_agent=True)
    a._session_mgr = SessionManager.create("ski_sub")
    a.agent_session.inject_skill_listing()
    assert _custom_msgs(a._session_mgr) == []          # 子 agent → no-op


def test_skill_tool_returns_stub_and_queues_body(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    d = tmp_path / ".nanocode" / "skills" / "commitx"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: commitx\ndescription: c\ncontext: inline\n---\nDo the thing $ARGUMENTS")
    monkeypatch.chdir(tmp_path)
    discovery.reset_skill_cache()
    a = _agent()
    res = asyncio.run(a._execute_skill_tool({"skill_name": "commitx", "args": "now"}))
    assert "loaded" in res.lower() and "commitx" in res        # tool_result 是 stub
    assert "Do the thing" not in res                              # body 不在 tool_result
    assert a._pending_skill_bodies and a._pending_skill_bodies[0][0] == "commitx"
    a._session_mgr = SessionManager.create("ski_body")
    a.agent_session.inject_pending_skill_bodies()
    (body,) = _custom_msgs(a._session_mgr, "skill_body")
    assert "<command-name>commitx</command-name>" in body
    assert "Do the thing now" in body                            # $ARGUMENTS 已替换
    assert a._pending_skill_bodies == []                          # 队列已清


def test_clear_history_resets_skill_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = _agent()
    a._sent_skill_names.add("x")
    a._pending_skill_bodies.append(("x", "b"))
    attach_runtime_agent(a)
    a.agent_session.clear_history()
    assert a._sent_skill_names == set() and a._pending_skill_bodies == []


def test_paths_skill_activated_by_touch(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    d = tmp_path / ".nanocode" / "skills" / "pyhelp"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: pyhelp\ndescription: py\npaths:\n  - '*.py'\n---\nb")
    monkeypatch.chdir(tmp_path); discovery.reset_skill_cache()
    a = _agent()
    a._session_mgr = SessionManager.create("ski_paths")
    a.agent_session.inject_skill_listing()
    assert "pyhelp" not in str(_custom_msgs(a._session_mgr))   # 未触碰 → 不在清单
    a._on_file_touched("read_file", {"file_path": "foo.py"})
    assert "pyhelp" in a._activated_path_skills
    a.agent_session.inject_skill_listing()
    assert "pyhelp" in str(_custom_msgs(a._session_mgr))       # 触碰后 → 进清单


def test_disable_model_invocation_rejected(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    d = tmp_path / ".nanocode" / "skills" / "noai"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: noai\ndescription: x\ndisable-model-invocation: true\ncontext: inline\n---\nbody")
    monkeypatch.chdir(tmp_path); discovery.reset_skill_cache()
    a = _agent()
    res = asyncio.run(a._execute_skill_tool({"skill_name": "noai", "args": ""}))
    assert "cannot be invoked" in res.lower() and a._pending_skill_bodies == []


def test_clear_resets_activation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = _agent()
    a._activated_path_skills.add("x")
    attach_runtime_agent(a)
    a.agent_session.clear_history()
    assert a._activated_path_skills == set()
