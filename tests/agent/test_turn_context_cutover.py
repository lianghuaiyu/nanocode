"""docs/16 #6（STEP E）专项：date/git 移出 system prompt → per-turn volatile tail。

- system prompt 不再含 date / git（stale live bug 的根源）；
- 每个请求的**尾部**带 per-turn 快照（render-time 装饰）；树保持干净原文（不入树）；
- git subprocess 每 turn 只跑一次（turn 内多迭代复用，per-turn 缓存）；
- 子 agent 不注入（其 system prompt 来自 manifest，从未含 date/git）；
- per-turn ContextLedger 拿到全量注入记账（/context 可见性）。
"""

import asyncio
from datetime import date

from nanocode.agent.engine import Agent
from nanocode.session import tree as T
from nanocode.session.manager import SessionManager


class _FakeBlock:
    def __init__(self, type="text", **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeUsage:
    input_tokens = 10
    output_tokens = 5


class _FakeResp:
    def __init__(self, content):
        self.content = content
        self.usage = _FakeUsage()


def _agent(sid):
    a = Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")
    a._mcp_initialized = True
    a.model = "claude-x"
    return a


def test_system_prompt_no_longer_bakes_date_or_git():
    a = _agent("ctx6_sys")
    assert date.today().isoformat() not in a._system_prompt   # 过午夜即 stale 的烤入已删
    assert "Git branch:" not in a._system_prompt
    assert "{{date}}" not in a._system_prompt                 # 模板变量也没有残留字面量
    assert "{{git_context}}" not in a._system_prompt


def test_turn_requests_carry_volatile_tail_and_tree_stays_clean(monkeypatch):
    monkeypatch.setattr("nanocode.prompt.get_git_context",
                        lambda: "\nGit branch: feature-x\nRecent commits:\nabc123 wip")
    a = _agent("ctx6_live")
    captured = {"reqs": [], "n": 0}

    async def fake(**kw):
        captured["reqs"].append(kw["messages"])
        captured["n"] += 1
        if captured["n"] == 1:
            return _FakeResp([_FakeBlock("tool_use", id="t1", name="list_files", input={"path": "."})])
        return _FakeResp([_FakeBlock("text", text="done")])

    a._provider.stream = fake
    asyncio.run(a.chat("hello"))

    today = date.today().isoformat()
    for req in captured["reqs"]:                              # 每个请求（含 turn 内第二迭代）都带快照
        tail = str(req[-1])                                   # volatile tail 置尾（合并进末条 user）
        assert today in tail and "feature-x" in tail
        assert "Per-turn context" in tail
    # 树保持干净原文：volatile 不入树（无 env/git custom_message；MESSAGE 不含快照标记）
    entries = SessionManager.open("ctx6_live").entries()
    blob = str([e.to_dict() for e in entries])
    assert "Per-turn context" not in blob and "feature-x" not in blob


def test_git_subprocess_runs_once_per_turn(monkeypatch):
    calls = {"n": 0}

    def counting_git():
        calls["n"] += 1
        return "\nGit branch: main"

    monkeypatch.setattr("nanocode.prompt.get_git_context", counting_git)
    a = _agent("ctx6_git")
    n_iter = {"n": 0}

    async def fake(**_kw):
        n_iter["n"] += 1
        if n_iter["n"] < 3:                                   # 3 次迭代的 turn（2 个工具回合）
            return _FakeResp([_FakeBlock("tool_use", id=f"t{n_iter['n']}", name="list_files",
                                         input={"path": "."})])
        return _FakeResp([_FakeBlock("text", text="done")])

    a._provider.stream = fake
    asyncio.run(a.chat("go"))
    assert n_iter["n"] == 3
    assert calls["n"] == 1                                    # per-turn 缓存：迭代间不重跑 git
    asyncio.run(a.chat("again"))
    assert calls["n"] == 2                                    # 下一 turn 重新收集（新鲜度即修复的 bug）


def test_sub_agent_gets_no_volatile_tail():
    sub = Agent(api_key="test", session_id="ctx6_sub", permission_mode="bypassPermissions",
                is_sub_agent=True, custom_system_prompt="manifest prompt")
    sub._mcp_initialized = True
    sub.model = "claude-x"
    captured = {}

    async def fake(**kw):
        captured["messages"] = kw["messages"]
        return _FakeResp([_FakeBlock("text", text="ok")])

    sub._provider.stream = fake
    sub._session_mgr = SessionManager.create("ctx6_sub.child")
    asyncio.run(sub.chat("task"))
    assert "Per-turn context" not in str(captured["messages"])


def test_context_ledger_records_full_turn_visibility():
    a = _agent("ctx6_ledger")

    async def fake(**_kw):
        return _FakeResp([_FakeBlock("text", text="hi")])

    a._provider.stream = fake
    asyncio.run(a.chat("hello"))

    led = a._context_ledger
    assert led is not None and led.entries
    kinds = {e.pack.kind for e in led.entries}
    # volatile（env 必有；git 视环境）+ session-context（项目指令至少在本仓库存在）
    assert "env" in kinds
    assert "project_instructions" in kinds
    # 全部 entry 带 included/reason（/context render 不炸）
    assert led.render_summary()


def test_volatile_tail_absent_when_no_plan():
    # turn 外直调 project_request（无 _turn_context_plan）→ 纯树渲染，无尾巴。
    a = _agent("ctx6_noplan")
    a._session_mgr = SessionManager.create("ctx6_noplan")
    a._session_mgr.append_message(T.user_message("bare"))
    proj = a.agent_session.project_request()
    assert "Per-turn context" not in str(proj.messages)


# ─── repo map 上 live 请求路径（aider-style，docs/15 §9）─────────────────────

def _repo_with_code(tmp_path, monkeypatch):
    from nanocode.codeintel import reset_services
    reset_services()
    (tmp_path / "applib.py").write_text("def unique_marker_fn():\n    pass\n")
    (tmp_path / "caller.py").write_text("from applib import unique_marker_fn\n"
                                        "def use():\n    unique_marker_fn()\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_repo_map_in_request_tail_never_in_tree(tmp_path, monkeypatch):
    _repo_with_code(tmp_path, monkeypatch)
    a = _agent("ctx6_rmap")
    captured = {}

    async def fake(**kw):
        captured["messages"] = kw["messages"]
        return _FakeResp([_FakeBlock("text", text="ok")])

    a._provider.stream = fake
    asyncio.run(a.chat("hello"))
    tail = str(captured["messages"][-1])
    assert "# Repo map" in tail and "unique_marker_fn" in tail   # volatile tail 携带
    blob = str([e.to_dict() for e in SessionManager.open("ctx6_rmap").entries()])
    assert "# Repo map" not in blob and "unique_marker_fn" not in blob   # 绝不落树


def test_repo_map_excludes_files_read_and_uses_mentions(tmp_path, monkeypatch):
    _repo_with_code(tmp_path, monkeypatch)
    a = _agent("ctx6_rmap2")
    a._files_read.add(str(tmp_path / "caller.py"))               # 宿主观测：已读
    captured = {}

    async def fake(**kw):
        captured["messages"] = kw["messages"]
        return _FakeResp([_FakeBlock("text", text="ok")])

    a._provider.stream = fake
    asyncio.run(a.chat("look at unique_marker_fn"))
    tail = str(captured["messages"][-1])
    assert "applib.py" in tail                                   # 被已读文件引用 + 提及 → 入图
    assert "caller.py:" not in tail                              # personal（已读）不渲染


def test_repo_map_settings_escape_hatch(tmp_path, monkeypatch):
    _repo_with_code(tmp_path, monkeypatch)
    from nanocode.tools import reset_permission_cache
    (tmp_path / ".nanocode").mkdir(exist_ok=True)
    (tmp_path / ".nanocode" / "settings.json").write_text('{"context": {"repo_map": false}}')
    reset_permission_cache()
    a = _agent("ctx6_rmap3")
    captured = {}

    async def fake(**kw):
        captured["messages"] = kw["messages"]
        return _FakeResp([_FakeBlock("text", text="ok")])

    a._provider.stream = fake
    asyncio.run(a.chat("hello"))
    reset_permission_cache()
    assert "# Repo map" not in str(captured["messages"])         # 逃生阀关闭注入
