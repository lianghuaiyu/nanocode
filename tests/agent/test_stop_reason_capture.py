"""docs/14 §4.3 bug#2：provider-faithful stopReason —— capture 采用 backend 真实 stop/finish
reason（不再纯内容推断），使 render 的 drop-aborted/error 门真正可达。"""

from nanocode.session import capture, tree
from nanocode.session.render import ModelCtx, render


def test_neutral_stop_reason_mapping():
    assert capture.neutral_stop_reason("anthropic", "tool_use") == "toolUse"
    assert capture.neutral_stop_reason("anthropic", "max_tokens") == "maxTokens"
    assert capture.neutral_stop_reason("anthropic", "end_turn") == "stop"
    assert capture.neutral_stop_reason("openai", "tool_calls") == "toolUse"
    assert capture.neutral_stop_reason("openai", "length") == "maxTokens"
    assert capture.neutral_stop_reason("anthropic", "error") == "error"   # 未知值 verbatim 透传
    assert capture.neutral_stop_reason("anthropic", None) is None


def test_capture_uses_explicit_stop_reason_else_infers():
    text_msg = {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}
    assert capture.capture_anthropic(text_msg, model="m", stop_reason="maxTokens")[0]["stopReason"] == "maxTokens"
    assert capture.capture_anthropic(text_msg, model="m")[0]["stopReason"] == "stop"   # 无显式 → 推断
    tool_msg = {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "run", "input": {}}]}
    assert capture.capture_anthropic(tool_msg, model="m")[0]["stopReason"] == "toolUse"


def test_capture_openai_explicit_stop_reason():
    msg = {"role": "assistant", "content": "ok", "tool_calls": []}
    assert capture.capture_openai(msg, model="m", stop_reason="maxTokens")[0]["stopReason"] == "maxTokens"


def test_capture_openai_bad_tool_args_audited_not_swallowed():
    # docs/14 §5.3：OpenAI tool args JSON 解析失败 → arguments={} 但原始串保留在 argumentsRaw 供审计。
    msg = {"role": "assistant", "content": None,
           "tool_calls": [{"id": "t1", "function": {"name": "run", "arguments": "{not json"}}]}
    out = capture.capture_openai(msg, model="m")
    tc = next(b for b in out[0]["content"] if b["type"] == "toolCall")
    assert tc["arguments"] == {} and tc["argumentsRaw"] == "{not json"


def test_per_tool_output_cap_exists():
    # §10#5 prerequisite：单条工具输出有硬 cap（防大输出在 summary-compaction 前撑爆窗口）。
    from nanocode.tools.shared import MAX_RESULT_CHARS, _truncate_result
    big = "x" * (MAX_RESULT_CHARS + 10_000)
    capped = _truncate_result(big)
    assert len(capped) < len(big) and "truncated" in capped


def test_render_drops_aborted_assistant_now_reachable():
    # 真实 stopReason 能进树后，render 的 drop-aborted 门才有意义（之前内容推断永远给 stop/toolUse）。
    msgs = [
        tree.user_message("q"),
        tree.assistant_message([tree.text_block("partial...")], provider="anthropic",
                               api="anthropic", model="claude-x", stop_reason="aborted"),
        tree.user_message("q2"),
    ]
    out = render(msgs, ModelCtx("anthropic", "anthropic", "claude-x"))["messages"]
    joined = str(out)
    assert "partial" not in joined          # aborted assistant 被丢弃
    assert "q2" in joined
