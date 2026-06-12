"""REPL slash-command 分发的 characterization 测试（锁定 cli.run_repl 当前行为）。

用途：作为 CMD-P0「把 cli.py:562-721 的 if/startswith 链逐字搬进 commands/ registry」的
安全网 —— **同一套断言必须在改造前后都绿**。覆盖 docs/11 列出的 load-bearing 不变量：

  1. most-specific-first：'/memory eval generate' 不被 '/memory eval' 吃掉
  2. 落空→chat：未知 '/foo' 当普通文本发给模型（session.run_turn("/foo")）
  3. 双闭包：命令复用 run_repl 现有的 AgentSession（此处用录制替身验证 run_turn 被调）
  4. 全角 ／ 归一化为 /
  5. 裸词 exit/quit 跳出循环（不读后续行、不进 chat）
  6. !shell 直跑用户 shell（不进 chat）

驱动方式：**不重构 run_repl**。monkeypatch cli._async_read_line 喂脚本化输入 + EOF 收尾；
monkeypatch cli.AgentSession 为录制替身（绕开真实 SessionContextBuilder / agent.chat）；
把 run_repl 触达的模块级 helper 与 tasks_tool 的延迟 import 全部换成录制 stub。所有 handler
命中都落到一个 `calls` 列表，按输入断言「哪个分支触发」。

两档断言：
  · Tier A（refactor-stable）—— chat/shell 落点的分区、顺序、exit、全角归一。改造后仍成立。
  · Tier B（较细，改造后可能需轻改）—— 每条命令命中了哪个 Agent 方法 / helper。
    搬迁后这些 helper 会移进 commands/builtin/*，断言可能要跟着改 import 目标，但「命中哪条
    命令分支」的意图不变。
"""

import asyncio
import signal

import pytest

from nanocode.entrypoints import cli


# ─── 录制替身 ────────────────────────────────────────────────────

class _FakeTaskManager:
    """can_switch() 读 list_subagents()；dispatch 命令族的 tasks_tool 调用已被 monkeypatch 拦掉。"""
    def list_subagents(self): return []
    def list_tasks(self, status=None): return []


class _FakeAgent:
    """记录 run_repl 直接触达的 Agent 面；不跑真实模型循环。"""

    def __init__(self, calls: list):
        self.calls = calls
        self.session_id = "sess-test"
        self._aborted = False
        self.is_processing = False
        self.task_manager = _FakeTaskManager()
        self._background_tasks = set()          # can_switch 取 len()/真值（docs/14 P2 Control 路由会查闸）
        self._sink = None  # CommandContext(out=agent._sink)；CMD-P0 handler 不用它
        # CMD-P2.5：RuntimeThread.run 读取 token 计数（turn 前后取差）
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    # 审批注入点（run_repl 开头调用）
    def set_confirm_fn(self, fn): self.calls.append(("set_confirm_fn",))
    def set_plan_approval_fn(self, fn): self.calls.append(("set_plan_approval_fn",))

    # 直接改 Agent 状态的命令
    def clear_history(self): self.calls.append(("clear_history",))
    def toggle_plan_mode(self): self.calls.append(("toggle_plan_mode",))
    def show_cost(self): self.calls.append(("show_cost",))

    @property
    def agent_session(self):
        # docs/16 #3a：/compact 走 agent_session.compact()（compaction owner = turn shell）。
        calls = self.calls
        class _S:
            async def compact(self): calls.append(("compact",))
        return _S()

    async def _spawn_memory_consolidate(self): self.calls.append(("_spawn_memory_consolidate",)); return ""
    async def _spawn_memory_eval(self): self.calls.append(("_spawn_memory_eval",)); return ""
    async def _spawn_memory_optimize(self): self.calls.append(("_spawn_memory_optimize",)); return ""
    def _register_skill_hooks(self, skill): self.calls.append(("_register_skill_hooks", skill.name))


class _RecordingSession:
    """替身 AgentSession：run_turn 仅记录 prompt（绕开真实 context builder / agent.chat）。"""

    def __init__(self, agent, **kw):
        self.agent = agent

    async def run_turn(self, prompt: str):
        self.agent.calls.append(("run_turn", prompt))


class _FakeSkill:
    def __init__(self, name, *, user_invocable=True, context="inline", hooks=None):
        self.name = name
        self.user_invocable = user_invocable
        self.context = context
        self.hooks = hooks


class _FakeSandbox:
    def __init__(self, calls): self.calls = calls
    def get_defaults(self): self.calls.append(("sandbox_get",)); return {"network": "off"}
    def set_default(self, k, v): self.calls.append(("sandbox_set", k, v)); return v


# ─── 驱动器 ──────────────────────────────────────────────────────

def _run_script(monkeypatch, lines, *, skill_lookup=None) -> list:
    """把 `lines` 依次喂给 run_repl，返回 handler 命中记录 `calls`。

    skill_lookup: 可选 name->skill|None，控制 get_skill_by_name 的返回（默认全部落空→chat）。
    """
    calls: list = []
    agent = _FakeAgent(calls)

    # 1) 脚本化输入：依次返回 lines，耗尽后 EOF 终止循环
    it = iter(lines)

    async def _fake_read(prompt="", *, input=None, output=None, persistent=True):
        try:
            return next(it)
        except StopIteration:
            return cli.EOF

    monkeypatch.setattr(cli, "_async_read_line", _fake_read)

    # 2) 替身 session：绕开真实 SessionContextBuilder / agent.chat
    monkeypatch.setattr(cli, "AgentSession", _RecordingSession)

    # 3) 静默欢迎语；info/error 记录但不打印
    monkeypatch.setattr(cli, "print_welcome", lambda *a, **k: None)
    monkeypatch.setattr(cli, "print_info", lambda *a, **k: calls.append(("print_info",)))
    monkeypatch.setattr(cli, "print_error", lambda *a, **k: calls.append(("print_error",)))

    # 4) 领域 helper → 录制 stub。CMD-P0 后这些 helper 由 commands/builtin 调用，故 patch 目标
    #    随代码迁移：list_memories/discover_skills/sandbox_defaults 在各自 source 模块（builtin
    #    用 call-time import），handle_eval_command 在 builtin 模块。（行为断言不变，仅 import 目标移。）
    import nanocode.entrypoints.commands.builtin as _builtin
    monkeypatch.setattr("nanocode.memory.list_memories",
                        lambda: (calls.append(("list_memories",)) or []))
    monkeypatch.setattr("nanocode.skills.discover_skills",
                        lambda: (calls.append(("discover_skills",)) or []))
    monkeypatch.setattr(_builtin, "handle_eval_command",
                        lambda rest: (calls.append(("handle_eval_command", rest)) or "stub"))
    monkeypatch.setattr("nanocode.tools.sandbox_defaults", _FakeSandbox(calls))
    # builtin handler 经 ...ui 打印；静默以免污染输出（断言只看 calls 录制）。
    monkeypatch.setattr(_builtin, "print_info", lambda *a, **k: None)
    monkeypatch.setattr(_builtin, "print_error", lambda *a, **k: None)

    async def _fake_shell(cmd):
        calls.append(("shell", cmd))
        return "shellout"

    monkeypatch.setattr(cli, "_run_user_shell", _fake_shell)

    def _lookup(name):
        calls.append(("get_skill_by_name", name))
        return skill_lookup(name) if skill_lookup else None

    monkeypatch.setattr(cli, "get_skill_by_name", _lookup)
    monkeypatch.setattr(cli, "resolve_skill_prompt", lambda skill, args: f"RESOLVED:{skill.name}:{args}")
    monkeypatch.setattr(cli, "execute_skill",
                        lambda name, args: (calls.append(("execute_skill", name, args)) or "ran"))

    # 5) tasks_tool 延迟 import（cli 在分支内 `from ..tools.tasks_tool import ...`）→ 在源模块打桩
    import nanocode.tools.tasks_tool as tt
    for fn in ("list_tasks_text", "task_output_text", "agents_overview_text",
               "list_agent_definitions_text", "list_subagents_text",
               "agent_definition_detail_text", "subagent_detail_text"):
        monkeypatch.setattr(tt, fn, (lambda f: (lambda *a, **k: (calls.append((f,)) or "stub")))(fn))

    async def _fake_task_stop(*a, **k):
        calls.append(("task_stop",))
        return "stopped"

    monkeypatch.setattr(tt, "task_stop", _fake_task_stop)

    # 6) 跑循环；run_repl 会装 SIGINT handler 但不还原 —— 测试负责快照/恢复
    prev = signal.getsignal(signal.SIGINT)
    try:
        asyncio.run(cli.run_repl(agent))
    finally:
        try:
            signal.signal(signal.SIGINT, prev)
        except (TypeError, ValueError):
            pass
    return calls


def _names(calls) -> list:
    return [c[0] for c in calls]


# ════════════════════════════════════════════════════════════════
# Tier A —— refactor-stable：落点分区 / 顺序 / exit / 全角 / shell
# ════════════════════════════════════════════════════════════════

# 已知命令一律不得落到普通 chat
_KNOWN_COMMANDS = [
    "/clear", "/plan", "/cost", "/compact",
    "/memory", "/memory consolidate", "/memory eval", "/memory eval generate",
    "/memory optimize", "/skills", "/sandbox",
    "/tasks", "/task abc", "/task-stop abc", "/agents", "/agent agent-001",
]


@pytest.mark.parametrize("line", _KNOWN_COMMANDS)
def test_known_command_does_not_fall_through_to_chat(monkeypatch, line):
    calls = _run_script(monkeypatch, [line])
    assert ("run_turn", line) not in calls, f"{line!r} 误落到 chat"


def test_unknown_slash_falls_through_to_chat(monkeypatch):
    """落空→chat：skill 查找未命中时，'/foo' 原样作为普通文本发给模型。"""
    calls = _run_script(monkeypatch, ["/frobnicate now"], skill_lookup=lambda n: None)
    assert ("run_turn", "/frobnicate now") in calls


def test_plain_text_goes_to_chat(monkeypatch):
    calls = _run_script(monkeypatch, ["fix the bug in app.py"])
    assert ("run_turn", "fix the bug in app.py") in calls


def test_memory_eval_generate_not_eaten_by_memory_eval(monkeypatch):
    """most-specific-first：精确分支必须先于 startswith 前缀分支命中。"""
    calls = _run_script(monkeypatch, ["/memory eval generate"])
    n = _names(calls)
    assert "_spawn_memory_eval" in n
    assert "handle_eval_command" not in n


def test_memory_eval_with_args_routes_to_eval_handler(monkeypatch):
    calls = _run_script(monkeypatch, ["/memory eval pending"])
    assert ("handle_eval_command", "pending") in calls
    assert "_spawn_memory_eval" not in _names(calls)


def test_bare_memory_eval_defaults_to_eval_handler(monkeypatch):
    calls = _run_script(monkeypatch, ["/memory eval"])
    assert ("handle_eval_command", "") in calls


def test_fullwidth_slash_is_normalized(monkeypatch):
    """中文输入法的全角 ／ 归一为 /，故 '／memory' 命中 /memory 而非当文本。"""
    calls = _run_script(monkeypatch, ["／memory"])
    assert "list_memories" in _names(calls)
    assert ("run_turn", "／memory") not in calls


@pytest.mark.parametrize("word", ["exit", "quit"])
def test_bare_word_exit_breaks_loop_before_next_line(monkeypatch, word):
    """exit/quit 立即跳出循环：后续脚本行不会被读取/分发。"""
    calls = _run_script(monkeypatch, [word, "should-not-run"])
    assert ("run_turn", "should-not-run") not in calls


def test_bang_runs_user_shell_not_chat(monkeypatch):
    calls = _run_script(monkeypatch, ["!ls -la"])
    assert ("shell", "ls -la") in calls
    assert not any(c[0] == "run_turn" for c in calls)


def test_non_user_invocable_skill_falls_through_to_chat(monkeypatch):
    """skill 存在但非 user_invocable → 不 continue → 落普通 chat（保留现行为）。"""
    skill = _FakeSkill("internal", user_invocable=False)
    calls = _run_script(monkeypatch, ["/internal"], skill_lookup=lambda n: skill)
    assert ("run_turn", "/internal") in calls


def test_user_invocable_skill_invokes_via_run_turn_not_raw_chat(monkeypatch):
    """inline skill：经 resolve_skill_prompt 后 run_turn 解析后的 prompt，而非原始 '/cmd args'。"""
    skill = _FakeSkill("commit", user_invocable=True, context="inline")
    calls = _run_script(monkeypatch, ["/commit fix types"],
                        skill_lookup=lambda n: skill if n == "commit" else None)
    assert ("run_turn", "RESOLVED:commit:fix types") in calls
    assert ("run_turn", "/commit fix types") not in calls


def test_blank_line_is_ignored(monkeypatch):
    calls = _run_script(monkeypatch, ["", "  "])
    assert not any(c[0] == "run_turn" for c in calls)


def test_repl_turn_driven_via_runtime_thread(monkeypatch):
    """docs/15 Phase 7：普通 turn 一律经 RuntimeThread.run（逃生阀 NANOCODE_REPL_VIA_RUNTIME 已删,
    runtime 是唯一 turn 路径）。落到录制 session 的 run_turn。"""
    calls = _run_script(monkeypatch, ["hello world"])
    assert ("run_turn", "hello world") in calls


def test_control_result_routes_to_apply_control_not_chat(monkeypatch):
    """docs/14 P1/P2：dispatch 返回 Control → run_repl 走 _apply_control（过 can_switch 闸后路由），
    不落 chat、不 fall through skill。用未配线的 'switch_runtime' 避免触发真实 rebind。"""
    from nanocode.entrypoints.commands.types import Control

    async def _fake_dispatch(line, registry, ctx):
        return Control("switch_runtime", {}) if line == "/ctl" else cli.NOT_A_COMMAND

    monkeypatch.setattr(cli, "dispatch", _fake_dispatch)
    calls = _run_script(monkeypatch, ["/ctl"])
    assert ("run_turn", "/ctl") not in calls                       # 不落 chat
    assert not any(c[0] == "get_skill_by_name" for c in calls)     # 未 fall through 到 skill
    assert ("print_info",) in calls                               # _apply_control 路由到了（打印 not-wired）


# ════════════════════════════════════════════════════════════════
# Tier B —— 每条命令命中了哪个 handler（搬迁后 import 目标可能要轻改）
# ════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("line, expected_call", [
    ("/clear", "clear_history"),
    ("/plan", "toggle_plan_mode"),
    ("/cost", "show_cost"),
    ("/compact", "compact"),
    ("/memory consolidate", "_spawn_memory_consolidate"),
    ("/memory eval generate", "_spawn_memory_eval"),
    ("/memory optimize", "_spawn_memory_optimize"),
    ("/memory", "list_memories"),
    ("/skills", "discover_skills"),
    ("/sandbox", "sandbox_get"),
])
def test_named_command_hits_expected_handler(monkeypatch, line, expected_call):
    calls = _run_script(monkeypatch, [line])
    assert expected_call in _names(calls)


@pytest.mark.parametrize("line, expected_call", [
    ("/tasks", "list_tasks_text"),
    ("/tasks running", "list_tasks_text"),
    ("/task abc", "task_output_text"),
    ("/task-stop abc", "task_stop"),
    ("/agents", "agents_overview_text"),
    ("/agents available", "list_agent_definitions_text"),
    ("/agents running", "list_subagents_text"),
    ("/agent agent-001", "subagent_detail_text"),
])
def test_tasks_family_hits_expected_handler(monkeypatch, line, expected_call):
    """演示延迟 import 命令族的打桩方式（在 nanocode.tools.tasks_tool 源模块 patch）。"""
    calls = _run_script(monkeypatch, [line])
    assert expected_call in _names(calls)


def test_sandbox_set_three_tokens(monkeypatch):
    calls = _run_script(monkeypatch, ["/sandbox network on"])
    assert ("sandbox_set", "network", "on") in calls


# TODO(CMD-P0)：搬迁完成后，新增一份「跑同样 _KNOWN_COMMANDS 表 + 上述断言，但通过新
# Registry/runner 而非 cli.run_repl」的对照测试，确认两条路径产生**逐条相同**的命中记录。
