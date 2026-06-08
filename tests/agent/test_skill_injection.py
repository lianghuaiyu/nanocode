import asyncio
from nanocode.agent.engine import Agent


def _agent():
    return Agent(api_key="test", trace_enabled=False)


def test_inject_listing_then_silent(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    d = tmp_path / ".nanocode" / "skills" / "k1"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: k1\ndescription: kk\n---\nbody")
    monkeypatch.chdir(tmp_path)
    discovery.reset_skill_cache()
    a = _agent()
    msgs = [{"role": "user", "content": "hi"}]
    a._inject_skill_listing(msgs)
    assert "<system-reminder>" in msgs[-1]["content"] and "k1" in msgs[-1]["content"]
    # 第二次无新增 → 不再注入
    before = [dict(m) for m in msgs]
    a._inject_skill_listing(msgs)
    assert msgs == before


def test_inject_listing_skipped_for_subagent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = Agent(api_key="test", trace_enabled=False, is_sub_agent=True)
    msgs = [{"role": "user", "content": "hi"}]
    a._inject_skill_listing(msgs)
    assert msgs == [{"role": "user", "content": "hi"}]


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
    msgs = []
    a._inject_pending_skill_bodies(msgs)
    assert "<command-name>commitx</command-name>" in msgs[0]["content"]
    assert "Do the thing now" in msgs[0]["content"]              # $ARGUMENTS 已替换
    assert a._pending_skill_bodies == []                          # 队列已清


def test_clear_history_resets_skill_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = _agent()
    a._sent_skill_names.add("x")
    a._pending_skill_bodies.append(("x", "b"))
    a.clear_history()
    assert a._sent_skill_names == set() and a._pending_skill_bodies == []


def test_paths_skill_activated_by_touch(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    d = tmp_path / ".nanocode" / "skills" / "pyhelp"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: pyhelp\ndescription: py\npaths:\n  - '*.py'\n---\nb")
    monkeypatch.chdir(tmp_path); discovery.reset_skill_cache()
    a = _agent()
    msgs = [{"role": "user", "content": "hi"}]
    a._inject_skill_listing(msgs)
    assert "pyhelp" not in str(msgs[-1]["content"])          # 未触碰 → 不在清单
    a._on_file_touched("read_file", {"file_path": "foo.py"})
    assert "pyhelp" in a._activated_path_skills
    msgs2 = [{"role": "user", "content": "next"}]
    a._inject_skill_listing(msgs2)
    assert "pyhelp" in str(msgs2[-1]["content"])              # 触碰后 → 进清单


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
    a.clear_history()
    assert a._activated_path_skills == set()
