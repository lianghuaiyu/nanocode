import asyncio
from nanocode.agent.engine import Agent


def _agent():
    return Agent(api_key="test", trace_enabled=False)


def test_skill_invocation_registers_hooks(tmp_path, monkeypatch):
    from nanocode.skills import discovery
    d = tmp_path / ".claude" / "skills" / "guard"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: guard\ncontext: inline\nhooks:\n  pre-tool-use:\n"
        "    - matcher: edit_file\n      command: 'true'\n---\nbody"
    )
    monkeypatch.chdir(tmp_path); discovery.reset_skill_cache()
    a = _agent()
    asyncio.run(a._execute_skill_tool({"skill_name": "guard", "args": ""}))
    assert any(h["skill"] == "guard" and h["event"] == "pre-tool-use" for h in a._active_hooks)


def test_pre_hook_failure_blocks_tool(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "x.txt"; f.write_text("hi")
    a = _agent()
    a._active_hooks = [{"skill": "g", "event": "pre-tool-use", "matcher": ["read_file"],
                        "command": "exit 1", "timeout_ms": 5000}]
    res = asyncio.run(a._execute_tool_call("read_file", {"file_path": str(f)}))
    assert "block" in res.lower()           # 被拦
    assert "hi" not in res                    # 原文件未被读出


def test_post_hook_failure_appends_warning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "y.txt"; f.write_text("CONTENT")
    a = _agent()
    a._active_hooks = [{"skill": "g", "event": "post-tool-use", "matcher": ["read_file"],
                        "command": "exit 1", "timeout_ms": 5000}]
    res = asyncio.run(a._execute_tool_call("read_file", {"file_path": str(f)}))
    assert "CONTENT" in res and "warning" in res.lower()   # 原结果在 + 追加 warning


def test_dangerous_hook_command_blocked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "z.txt"; f.write_text("hi")
    a = _agent()
    # 危险命令在 default 模式走 check_permission→confirm；无人值守等价拒绝（auto-deny）→ 阻断
    from nanocode.agent.engine import _auto_deny_confirm
    a.confirm_fn = _auto_deny_confirm
    a._active_hooks = [{"skill": "g", "event": "pre-tool-use", "matcher": ["*"],
                        "command": "rm -rf /tmp/whatever", "timeout_ms": 5000}]
    res = asyncio.run(a._execute_tool_call("read_file", {"file_path": str(f)}))
    assert "block" in res.lower()             # 确认被拒，命令未执行


def test_meta_tools_not_hooked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = _agent()
    a._active_hooks = [{"skill": "g", "event": "pre-tool-use", "matcher": ["*"],
                        "command": "exit 1", "timeout_ms": 5000}]
    # skill 是 meta 工具，不应被 hook 拦（会落到 unknown skill，不是 block）
    res = asyncio.run(a._execute_tool_call("skill", {"skill_name": "nope"}))
    assert "block" not in res.lower()


def test_clear_resets_hooks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = _agent()
    a._active_hooks = [{"skill": "g", "event": "pre-tool-use", "matcher": ["*"], "command": "true", "timeout_ms": 1}]
    a.clear_history()
    assert a._active_hooks == []


def test_register_skill_hooks_idempotent(tmp_path, monkeypatch):
    from nanocode.skills.discovery import SkillDefinition
    monkeypatch.chdir(tmp_path)
    a = _agent()
    sk = SkillDefinition(name="g", hooks={"pre-tool-use": [{"matcher": ["*"], "command": "true", "timeout_ms": 1}]})
    a._register_skill_hooks(sk); a._register_skill_hooks(sk)
    assert len(a._active_hooks) == 1   # 去重
