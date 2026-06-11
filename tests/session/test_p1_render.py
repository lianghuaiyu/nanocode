"""P1 render 验收矩阵：中立 Message[] → provider-合法 payload（docs/13 §4/§9-P1）。

覆盖：孤儿合成、aborted+inverse-orphan 删除、并行 tool_call(Anthropic 合并)、OpenAI tool-role、
跨 provider id 归一、image placeholder、thinking gate。
"""

from nanocode.session import tree
from nanocode.session.render import ModelCtx, render

ANTH = ModelCtx(provider="anthropic", api="anthropic", model_id="claude-x")
OAI = ModelCtx(provider="openai", api="openai-completions", model_id="gpt-x")


def _assistant(blocks, *, provider="anthropic", api="anthropic", model="claude-x", stop="toolUse"):
    return tree.assistant_message(blocks, provider=provider, api=api, model=model, stop_reason=stop)


def test_forward_orphan_synthesized():
    msgs = [_assistant([tree.tool_call_block("t1", "read_file", {"p": "a"})])]
    out = render(msgs, ANTH)["messages"]
    assert out[0]["role"] == "assistant"
    assert out[1]["role"] == "user"
    tr = out[1]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "t1"
    assert tr["is_error"] is True and tr["content"] == "No result provided"


def test_aborted_assistant_and_inverse_orphan_dropped():
    # aborted turn 已写入 t1 的 result → 丢 assistant 后 t1 result 成 inverse-orphan，必须一并删。
    msgs = [
        tree.user_message("hi"),
        _assistant([tree.tool_call_block("t1", "x", {}), tree.tool_call_block("t2", "y", {})], stop="aborted"),
        tree.tool_result_message(tool_call_id="t1", tool_name="x", content="partial"),
    ]
    out = render(msgs, ANTH)["messages"]
    # 只剩 user；无悬空 tool_use / 无孤儿 tool_result（provider-合法）。
    assert [m["role"] for m in out] == ["user"]
    flat = str(out)
    assert "tool_use" not in flat and "tool_result" not in flat


def test_parallel_tool_calls_group_into_one_anthropic_user_message():
    msgs = [
        _assistant([tree.tool_call_block("a", "x", {}), tree.tool_call_block("b", "y", {})]),
        tree.tool_result_message(tool_call_id="a", tool_name="x", content="ra"),
        tree.tool_result_message(tool_call_id="b", tool_name="y", content="rb"),
    ]
    out = render(msgs, ANTH)["messages"]
    assert len(out) == 2
    assert out[0]["role"] == "assistant"
    assert out[1]["role"] == "user"
    blocks = out[1]["content"]
    assert [b["tool_use_id"] for b in blocks] == ["a", "b"]  # 合并进同一条 user 消息


def test_openai_tool_role_and_arguments_json_string():
    msgs = [
        tree.user_message("hi"),
        _assistant([tree.text_block("ok"), tree.tool_call_block("t1", "run", {"x": 1})],
                   provider="openai", api="openai-completions", model="gpt-x"),
        tree.tool_result_message(tool_call_id="t1", tool_name="run", content="done"),
    ]
    out = render(msgs, OAI, system_prompt="SYS")["messages"]
    assert out[0] == {"role": "system", "content": "SYS"}
    assert out[1] == {"role": "user", "content": "hi"}
    asst = out[2]
    assert asst["role"] == "assistant" and asst["content"] == "ok"
    tc = asst["tool_calls"][0]
    assert tc["id"] == "t1" and tc["type"] == "function"
    assert tc["function"] == {"name": "run", "arguments": '{"x": 1}'}  # arguments 是 JSON 串
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": "done"}


def test_openai_id_normalization_pipe_and_length():
    long_id = "abc|" + "z" * 500
    msgs = [
        _assistant([tree.tool_call_block(long_id, "run", {})], provider="openai",
                   api="openai-completions", model="gpt-x"),
        tree.tool_result_message(tool_call_id=long_id, tool_name="run", content="r"),
    ]
    out = render(msgs, OAI)["messages"]
    nid = out[0]["tool_calls"][0]["id"]
    assert nid == "abc" and len(nid) <= 40
    assert out[1]["tool_call_id"] == nid  # toolResult 的 id 一并 remap


def test_anthropic_id_sanitized():
    bad = "tool/with:bad*chars"
    msgs = [_assistant([tree.tool_call_block(bad, "x", {})])]
    out = render(msgs, ANTH)["messages"]
    used = out[0]["content"][0]["id"]
    assert used == "tool_with_bad_chars"
    # 合成的孤儿 result 也用归一后的 id
    assert out[1]["content"][0]["tool_use_id"] == used


def test_image_placeholder_when_unsupported():
    ctx = ModelCtx(provider="anthropic", api="anthropic", model_id="claude-x", supports_images=False)
    msgs = [tree.user_message([tree.image_block("BASE64", "image/png"), tree.text_block("see")])]
    out = render(msgs, ctx)["messages"]
    blocks = out[0]["content"]
    assert all(b["type"] != "image" for b in blocks)
    assert any(b.get("text", "").startswith("(image omitted") for b in blocks)


def test_thinking_gate_same_model_keeps_signed():
    msgs = [_assistant([tree.thinking_block("reason", signature="sig"), tree.text_block("ans")], stop="stop")]
    out = render(msgs, ANTH)["messages"]
    blocks = out[0]["content"]
    th = [b for b in blocks if b["type"] == "thinking"]
    assert th and th[0]["signature"] == "sig" and th[0]["thinking"] == "reason"


def test_thinking_gate_cross_model_downgrades_to_text():
    # message recorded on claude-x; render target claude-y → not same model → thinking→text.
    msgs = [tree.assistant_message(
        [tree.thinking_block("reason", signature="sig"), tree.text_block("ans")],
        provider="anthropic", api="anthropic", model="claude-x", stop_reason="stop")]
    ctx = ModelCtx(provider="anthropic", api="anthropic", model_id="claude-y")
    out = render(msgs, ctx)["messages"]
    blocks = out[0]["content"]
    assert all(b["type"] != "thinking" for b in blocks)
    assert any(b["type"] == "text" and b["text"] == "reason" for b in blocks)


def test_redacted_thinking_dropped_cross_model_kept_same():
    redacted = tree.thinking_block("", signature="ENC", redacted=True)
    msg = lambda: tree.assistant_message([redacted, tree.text_block("a")],
                                          provider="anthropic", api="anthropic", model="claude-x", stop_reason="stop")
    same = render([msg()], ANTH)["messages"][0]["content"]
    assert any(b["type"] == "redacted_thinking" for b in same)
    cross = render([msg()], ModelCtx("anthropic", "anthropic", "claude-y"))["messages"][0]["content"]
    assert all(b["type"] not in ("thinking", "redacted_thinking") for b in cross)


def test_user_interrupt_mid_tool_batch_yields_legal_payload():
    # context-clear-mid-batch（评审命中）：assistant 出 t1/t2，仅 t1 有 result，user 插话打断。
    msgs = [
        _assistant([tree.tool_call_block("t1", "x", {}), tree.tool_call_block("t2", "y", {})]),
        tree.tool_result_message(tool_call_id="t1", tool_name="x", content="r1"),
        tree.user_message("stop, do this instead"),
    ]
    out = render(msgs, ANTH)["messages"]
    uses = [b["id"] for m in out if m["role"] == "assistant"
            for b in m["content"] if b["type"] == "tool_use"]
    results = [b["tool_use_id"] for m in out if m["role"] == "user" and isinstance(m["content"], list)
               for b in m["content"] if b.get("type") == "tool_result"]
    assert set(uses) == set(results)  # 每个 tool_use 在下个 user turn 前都被应答（Anthropic 合法）
    synth = [b for m in out if m["role"] == "user" and isinstance(m["content"], list)
             for b in m["content"] if b.get("tool_use_id") == "t2"]
    assert synth and synth[0]["is_error"] is True
