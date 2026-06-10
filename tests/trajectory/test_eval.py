"""trajectory.eval — P4 在线启发式 / reward / failure attribution 的派生标签测试。

合成 SessionEvent（error tool_result / permission deny / budget_exceeded / 干净 final
answer / pytest 输出 / 写文件 / compaction / legacy / summary-mode），断言：
  - online_evals 产出正确的 signal 集；
  - attach_rewards 对 error step 给负 reward、对干净 step 保持 None；
  - failure_attribution 指向 error。

边界自检：本测试不触碰 wire、不调任何 tracer/sink；eval/reward 纯 plain data。
"""
from __future__ import annotations

from nanocode.events.models import SessionEvent
from nanocode.trajectory.eval import (
    SIG_BUDGET_EXCEEDED,
    SIG_COMPACTION_OCCURRED,
    SIG_CONTEXT_OVERFLOW,
    SIG_PERMISSION_DENIED,
    SIG_REACHED_FINAL_ANSWER,
    SIG_SHELL_EXIT_CODE,
    SIG_TESTS_FAIL,
    SIG_TESTS_PASS,
    SIG_TOOL_ERROR,
    SIG_TOUCHED_FILE,
    attach_rewards,
    failure_attribution,
    online_evals,
)
from nanocode.trajectory.schema import Step


# ─── 合成 helper ─────────────────────────────────────────────────────────────

def _ev(etype, *, agent_id="main", seq=0, turn_id="turn_main_0", **payload) -> SessionEvent:
    """合成一条 new-style SessionEvent（payload 落顶层 → 归集为 data）。"""
    d = {
        "v": 1,
        "id": f"evt_{agent_id}_{seq}",
        "ts": f"2026-06-09T00:00:{seq:02d}+00:00",
        "session_id": "sess_test",
        "agent_id": agent_id,
        "branch_id": "main",
        "type": etype,
        "seq": seq,
        "turn_id": turn_id,
        **payload,
    }
    return SessionEvent.from_wire(d, agent_id=agent_id)


def _signals(evals):
    return [e["signal"] for e in evals]


def _by_signal(evals, signal):
    return [e for e in evals if e["signal"] == signal]


# ─── online_evals：逐信号 ────────────────────────────────────────────────────

def test_tool_error_from_error_result():
    evs = [_ev("tool_result", tool="grep_search", chars=20, result="Error: bad pattern")]
    out = online_evals(evs)
    assert _signals(out) == [SIG_TOOL_ERROR]
    rec = out[0]
    assert rec["agent_id"] == "main"
    assert rec["turn_id"] == "turn_main_0"
    assert rec["value"] is True
    assert "Error" in rec["detail"]


def test_tool_error_warning_prefix():
    evs = [_ev("tool_result", tool="x", result="Warning: something")]
    assert SIG_TOOL_ERROR in _signals(online_evals(evs))


def test_tool_blocked_is_tool_error():
    evs = [_ev("tool_blocked", tool="write_file", reason="not_in_allowlist")]
    out = online_evals(evs)
    assert _signals(out) == [SIG_TOOL_ERROR]
    assert "write_file" in out[0]["detail"]


def test_permission_denied():
    evs = [_ev("permission_decision", tool="run_shell", action="deny", message="dangerous rm -rf")]
    out = online_evals(evs)
    assert _signals(out) == [SIG_PERMISSION_DENIED]
    assert out[0]["value"] == "run_shell"
    assert "dangerous" in out[0]["detail"]


def test_permission_allow_emits_nothing():
    evs = [_ev("permission_decision", tool="run_shell", action="allow", message="ok")]
    assert online_evals(evs) == []


def test_shell_exit_code_nonzero():
    evs = [_ev("tool_result", tool="run_shell", result="boom\n(exit 2)")]
    out = online_evals(evs)
    assert SIG_SHELL_EXIT_CODE in _signals(out)
    rec = _by_signal(out, SIG_SHELL_EXIT_CODE)[0]
    assert rec["value"] == 2


def test_shell_exit_code_zero_no_signal():
    evs = [_ev("tool_result", tool="run_shell", result="done\n(exit 0)")]
    assert SIG_SHELL_EXIT_CODE not in _signals(online_evals(evs))


def test_tests_fail_heuristic():
    out = online_evals([_ev("tool_result", tool="run_shell",
                            result="=== 3 failed, 5 passed in 1.2s ===")])
    sigs = _signals(out)
    assert SIG_TESTS_FAIL in sigs
    assert SIG_TESTS_PASS not in sigs  # 有 failed 时不再记 pass
    assert _by_signal(out, SIG_TESTS_FAIL)[0]["value"] == 3


def test_tests_pass_heuristic():
    out = online_evals([_ev("tool_result", tool="run_shell",
                            result="=== 12 passed in 0.4s ===")])
    sigs = _signals(out)
    assert SIG_TESTS_PASS in sigs
    assert SIG_TESTS_FAIL not in sigs
    assert _by_signal(out, SIG_TESTS_PASS)[0]["value"] == 12


def test_tests_errors_count_as_fail():
    out = online_evals([_ev("tool_result", tool="run_shell",
                            result="=== 2 errors in 0.4s ===")])
    assert SIG_TESTS_FAIL in _signals(out)


def test_reached_final_answer_no_tool_uses():
    evs = [_ev("assistant_message", text="All done.", thinking="", tool_uses=[])]
    out = online_evals(evs)
    assert _signals(out) == [SIG_REACHED_FINAL_ANSWER]
    assert out[0]["value"] is True
    assert "All done" in out[0]["detail"]


def test_assistant_message_with_tool_uses_not_final():
    evs = [_ev("assistant_message", text="calling tool",
               tool_uses=[{"id": "t1", "name": "run_shell", "input": {}}])]
    assert online_evals(evs) == []


def test_touched_file_from_write_tool_call():
    evs = [_ev("tool_call", tool="edit_file", input={"file_path": "/tmp/a.py"}, tool_use_id="t1")]
    out = online_evals(evs)
    assert _signals(out) == [SIG_TOUCHED_FILE]
    assert out[0]["value"] == "/tmp/a.py"


def test_non_write_tool_call_no_touched_file():
    evs = [_ev("tool_call", tool="grep_search", input={"pattern": "x"})]
    assert online_evals(evs) == []


def test_compaction_occurred():
    evs = [_ev("compaction", kind="auto", message_count_before=40, message_count_after=12)]
    out = online_evals(evs)
    assert _signals(out) == [SIG_COMPACTION_OCCURRED]
    assert out[0]["value"] == "auto"
    assert "40" in out[0]["detail"] and "12" in out[0]["detail"]


def test_budget_exceeded():
    evs = [_ev("budget_exceeded", reason="max turns reached")]
    out = online_evals(evs)
    assert _signals(out) == [SIG_BUDGET_EXCEEDED]


def test_context_overflow_classification():
    evs = [_ev("budget_exceeded", reason="context window overflow")]
    out = online_evals(evs)
    assert _signals(out) == [SIG_CONTEXT_OVERFLOW]


# ─── 容错：legacy / summary-mode / malformed 绝不崩 ──────────────────────────

def test_summary_mode_tool_result_uses_result_summary():
    # SUMMARY 级别：无 full result，只有 result_summary + hash + chars。
    d = {
        "v": 1, "id": "evt_main_0", "ts": "t", "session_id": "s",
        "agent_id": "main", "branch_id": "main", "type": "tool_result", "seq": 0,
        "turn_id": "turn_main_0", "tool": "run_shell", "chars": 30,
        "result_summary": "Error: file not found", "result_hash": "sha256:abc",
    }
    ev = SessionEvent.from_wire(d, agent_id="main")
    out = online_evals([ev])
    assert SIG_TOOL_ERROR in _signals(out)


def test_legacy_flat_row_does_not_crash():
    # legacy 行：无 id / parent，payload 在顶层。
    d = {"v": 1, "ts": "t", "session_id": "s", "seq": 3, "type": "tool_result",
         "tool": "run_shell", "result": "(exit 1)"}
    ev = SessionEvent.from_wire(d, agent_id="main")
    assert ev.legacy is True
    out = online_evals([ev])
    assert SIG_SHELL_EXIT_CODE in _signals(out)


def test_malformed_and_missing_fields_no_crash():
    evs = [
        _ev("tool_result"),  # 无 result / result_summary
        _ev("permission_decision"),  # 无 action
        _ev("tool_call", tool="edit_file"),  # 无 input
        _ev("assistant_message"),  # 无 tool_uses → 视为 final
        _ev("budget_exceeded"),  # 无 reason
        _ev("unknown_type", foo="bar"),  # 未知 type
    ]
    out = online_evals(evs)  # 不抛即通过
    assert isinstance(out, list)
    # 缺字段的 final answer 仍被识别
    assert SIG_REACHED_FINAL_ANSWER in _signals(out)


def test_empty_events():
    assert online_evals([]) == []
    assert online_evals(None) == []


# ─── attach_rewards：error step 负 reward，干净 step 留 None ──────────────────

def _step(step_id, agent_id="main", turn_id="turn_main_0"):
    return Step(
        trajectory_id="traj_test", episode_id="sess_test", step_id=step_id,
        parent_step_id=None, turn_id=turn_id, agent_id=agent_id, step_type="tool_action",
    )


def test_attach_rewards_negative_on_error_step():
    steps = [_step("step_main_0"), _step("step_main_1")]
    evals = [
        {"step_id": "step_main_0", "turn_id": "turn_main_0", "agent_id": "main",
         "signal": SIG_TOOL_ERROR, "value": True, "detail": "Error: x"},
    ]
    out = attach_rewards(steps, evals)
    assert out[0].reward == -1.0          # error step → 负 reward
    assert out[1].reward is None          # 干净 step → 保持 None


def test_attach_rewards_is_non_mutating():
    steps = [_step("step_main_0")]
    evals = [{"step_id": "step_main_0", "turn_id": None, "agent_id": "main",
              "signal": SIG_PERMISSION_DENIED, "value": "run_shell", "detail": ""}]
    out = attach_rewards(steps, evals)
    assert out[0].reward == -1.0
    assert steps[0].reward is None        # 原列表未被就地改


def test_attach_rewards_shell_exit_nonzero_penalized():
    steps = [_step("step_main_0")]
    evals = [{"step_id": "step_main_0", "turn_id": None, "agent_id": "main",
              "signal": SIG_SHELL_EXIT_CODE, "value": 2, "detail": "(exit 2)"}]
    out = attach_rewards(steps, evals)
    assert out[0].reward == -1.0


def test_attach_rewards_shell_exit_zero_not_penalized():
    steps = [_step("step_main_0")]
    evals = [{"step_id": "step_main_0", "turn_id": None, "agent_id": "main",
              "signal": SIG_SHELL_EXIT_CODE, "value": 0, "detail": "(exit 0)"}]
    out = attach_rewards(steps, evals)
    assert out[0].reward is None


def test_attach_rewards_clean_run_all_none():
    steps = [_step("step_main_0"), _step("step_main_1")]
    evals = [{"step_id": "step_main_0", "turn_id": None, "agent_id": "main",
              "signal": SIG_REACHED_FINAL_ANSWER, "value": True, "detail": "done"}]
    out = attach_rewards(steps, evals)
    assert all(s.reward is None for s in out)


def test_attach_rewards_no_step_id_eval_ignored():
    steps = [_step("step_main_0")]
    evals = [{"step_id": None, "turn_id": "turn_main_0", "agent_id": "main",
              "signal": SIG_TOOL_ERROR, "value": True, "detail": ""}]
    out = attach_rewards(steps, evals)
    assert out[0].reward is None          # 无 step_id 的 eval 不回填


def test_attach_rewards_empty():
    assert attach_rewards([], []) == []
    assert attach_rewards(None, []) == []


# ─── failure_attribution：指向首个失败 ───────────────────────────────────────

def test_failure_attribution_points_at_error():
    evs = [
        _ev("tool_call", seq=0, tool="run_shell", input={"command": "pytest"}),
        _ev("tool_result", seq=1, tool="run_shell", result="Error: boom"),
    ]
    evals = online_evals(evs)
    attr = failure_attribution(evs, evals)
    assert attr is not None
    assert attr["signal"] == SIG_TOOL_ERROR
    assert attr["agent_id"] == "main"
    assert "boom" in attr["detail"]


def test_failure_attribution_permission_denied_tool():
    evs = [_ev("permission_decision", tool="run_shell", action="deny", message="no")]
    evals = online_evals(evs)
    attr = failure_attribution(evs, evals)
    assert attr["signal"] == SIG_PERMISSION_DENIED
    assert attr["tool"] == "run_shell"


def test_failure_attribution_tool_blocked_extracts_tool():
    evs = [_ev("tool_blocked", tool="write_file", reason="not_in_allowlist")]
    evals = online_evals(evs)
    attr = failure_attribution(evs, evals)
    assert attr["signal"] == SIG_TOOL_ERROR
    assert attr["tool"] == "write_file"


def test_failure_attribution_first_of_many():
    evs = [
        _ev("budget_exceeded", seq=0, reason="max turns"),
        _ev("tool_result", seq=1, tool="run_shell", result="Error: later"),
    ]
    evals = online_evals(evs)
    attr = failure_attribution(evs, evals)
    # 第一条失败信号即时间序最早（budget_exceeded）。
    assert attr["signal"] == SIG_BUDGET_EXCEEDED


def test_failure_attribution_clean_run_returns_none():
    evs = [
        _ev("tool_result", seq=0, tool="run_shell", result="ok\n(exit 0)"),
        _ev("assistant_message", seq=1, text="done", tool_uses=[]),
    ]
    evals = online_evals(evs)
    assert failure_attribution(evs, evals) is None


def test_failure_attribution_empty_evals_none():
    assert failure_attribution([], []) is None


# ─── 端到端：错误 + 拒绝 + budget + 干净 final，一次性断言 ────────────────────

def test_end_to_end_mixed_run():
    evs = [
        _ev("tool_call", seq=0, turn_id="turn_main_0", tool="run_shell",
            input={"command": "pytest"}),
        _ev("tool_result", seq=1, turn_id="turn_main_0", tool="run_shell",
            result="=== 1 failed in 0.1s ===\n(exit 1)"),
        _ev("permission_decision", seq=2, turn_id="turn_main_1", tool="run_shell",
            action="deny", message="rm -rf denied"),
        _ev("budget_exceeded", seq=3, turn_id="turn_main_1", reason="context overflow"),
        _ev("assistant_message", seq=4, turn_id="turn_main_2", text="finished", tool_uses=[]),
    ]
    evals = online_evals(evs)
    sigs = set(_signals(evals))
    assert SIG_TESTS_FAIL in sigs
    assert SIG_SHELL_EXIT_CODE in sigs
    assert SIG_PERMISSION_DENIED in sigs
    assert SIG_CONTEXT_OVERFLOW in sigs
    assert SIG_REACHED_FINAL_ANSWER in sigs

    # failure_attribution 指向最早失败（tests_fail，seq=1 的结果）。
    attr = failure_attribution(evs, evals)
    assert attr is not None
    assert attr["signal"] in {SIG_TESTS_FAIL, SIG_SHELL_EXIT_CODE}


def test_module_never_touches_wire():
    # 纯静态边界自检：eval 模块不得 import 任何 runtime 模块、不得调用 emit/写盘。
    # 仅扫描真实代码行（剥离注释/docstring），避免误伤描述边界的中文注释。
    import ast

    import nanocode.trajectory.eval as evalmod

    text = open(evalmod.__file__, encoding="utf-8").read()
    tree = ast.parse(text)

    # 1) 静态扫 import：禁止任何 runtime 模块。
    runtime_mods = ("engine", "anthropic_backend", "openai_backend",
                    "context_builder", "session", "subagent_manager")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert "agent" not in mod.split("."), f"must not import runtime: {mod}"
            assert "tracer" not in mod, f"must not import tracer: {mod}"
            for part in runtime_mods:
                assert part not in mod, f"must not import runtime: {mod}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert "nanocode.agent" not in alias.name
                assert "trace.tracer" not in alias.name

    # 2) 剥离注释/字符串后，确认没有 emit/write 等写侧调用。
    code_only = "\n".join(
        line.split("#", 1)[0] for line in text.splitlines()
    )
    for tok in (".emit(", ".write(", "tracer.", "make_tracer"):
        assert tok not in code_only, f"eval.py must not call runtime sink: {tok}"


# ─── step 归属正确性（修审阅 HIGH）+ 去重（修审阅 LOW）────────────────────────


def test_reward_attributed_to_failing_tool_step_not_final():
    """HIGH 回归：失败 tool_action 的 tool_error 必须归到该 tool step，而非该 turn 的 final step。

    端到端跑 build_steps -> online_evals(events, steps) -> attach_rewards，断言负 reward 落在
    失败的 tool_action（锚在 tool_call seq）而**不是**成功的最终答复 final step。
    """
    from nanocode.trajectory.project import build_steps

    events = [
        _ev("llm_request", seq=0, message_count=1),
        _ev("assistant_message", seq=1, text="run it",
            tool_uses=[{"id": "t0", "name": "run_shell", "input": {}}]),
        _ev("llm_response", seq=2, input_tokens=5, output_tokens=2),
        _ev("tool_call", seq=3, tool="run_shell", input={"command": "boom"}, tool_use_id="t0"),
        _ev("tool_result", seq=4, tool="run_shell", tool_use_id="t0", result="Error: boom"),
        _ev("llm_request", seq=5, message_count=3),
        _ev("assistant_message", seq=6, text="all done", tool_uses=[]),
        _ev("llm_response", seq=7, input_tokens=3, output_tokens=1),
    ]
    steps = build_steps(events)
    evals = online_evals(events, steps)
    rewarded = attach_rewards(steps, evals)

    # tool_error eval 归到失败 tool_action（step_main_3），不是 final。
    err = [e for e in evals if e["signal"] == SIG_TOOL_ERROR]
    assert len(err) == 1
    assert err[0]["step_id"] == "step_main_3"

    tool_step = next(s for s in rewarded if s.step_type == "tool_action")
    final_steps = [s for s in rewarded if s.step_type == "final"]
    assert tool_step.reward == -1.0                         # 负 reward 落在失败那一步
    assert final_steps and all(s.reward is None for s in final_steps)  # 成功最终答复不被污染


def test_blocked_tool_triple_emits_single_tool_error():
    """LOW 回归：tool_blocked + 其配对 "Error: tool ... not permitted" tool_result 只产 1 条 tool_error。"""
    evs = [
        _ev("tool_call", seq=0, tool="run_shell", input={"command": "ls"}, tool_use_id="t0"),
        _ev("tool_blocked", seq=1, tool="run_shell", reason="not_in_allowlist"),
        _ev("tool_result", seq=2, tool="run_shell", tool_use_id="t0",
            result="Error: tool 'run_shell' is not permitted for this sub-agent."),
    ]
    out = online_evals(evs)
    assert _signals(out).count(SIG_TOOL_ERROR) == 1


def test_non_block_error_result_still_emits_tool_error():
    """去重只针对 "not permitted" 块文本；普通 Error 结果仍照常产 tool_error。"""
    evs = [_ev("tool_result", tool="run_shell", result="Error: command failed")]
    assert SIG_TOOL_ERROR in _signals(online_evals(evs))
