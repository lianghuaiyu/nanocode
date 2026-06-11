"""P2 语义等价闸门（docs/13 §9-P2 / 评审 B1）。

证明双写忠实：capture(live provider list) → message entries → build_context → render
在**归一化投影**下 ≡ 原 live list。归一化按 B1 规整：tool-call arguments 解析为对象、
is_error/content 缺省统一、剥 system（render 侧 Context）。限无压缩/无注入 turn。
"""

import json

from nanocode.session import capture, tree
from nanocode.session.manager import SessionManager
from nanocode.session.render import ModelCtx, render


# ─── 归一化投影 ──────────────────────────────────────────────────────────────
def _norm_block(b: dict):
    t = b.get("type")
    if t == "text":
        return ("text", b.get("text", ""))
    if t == "tool_use":
        return ("tool_use", b.get("id"), b.get("name"), b.get("input"))
    if t == "image":
        src = b.get("source") or {}
        return ("image", src.get("media_type"), src.get("data"))
    return ("other", json.dumps(b, sort_keys=True))


def norm_anthropic(msgs):
    out = []
    for m in msgs:
        role = m["role"]
        c = m.get("content")
        if role == "assistant":
            out.append(("assistant", [_norm_block(b) for b in c]))
        elif isinstance(c, list) and c and isinstance(c[0], dict) and c[0].get("type") == "tool_result":
            out.append(("toolresults", [(b.get("tool_use_id"), b.get("content"), bool(b.get("is_error", False)))
                                         for b in c]))
        else:
            out.append(("user", c if isinstance(c, str) else [_norm_block(b) for b in c]))
    return out


def norm_openai(msgs):
    out = []
    for m in msgs:
        role = m["role"]
        if role == "system":
            continue
        if role == "assistant":
            tcs = [(tc["id"], tc["function"]["name"], json.loads(tc["function"]["arguments"]))
                   for tc in (m.get("tool_calls") or [])]
            out.append(("assistant", m.get("content") or "", tcs))
        elif role == "user":
            out.append(("user", m.get("content") or ""))
        elif role == "tool":
            out.append(("tool", m.get("tool_call_id"), m.get("content")))
    return out


def _roundtrip(provider_list, provider, ctx):
    """capture → 树 → build_context → render，返回 render 出的 provider list。"""
    mgr = SessionManager.create(cwd="/tmp")
    for neutral in capture.capture_provider_messages(provider_list, provider, model=ctx.model_id):
        mgr.append_message(neutral)
    built = mgr.build_context()
    return render(built.messages, ctx)["messages"]


ANTH = ModelCtx(provider="anthropic", api="anthropic", model_id="claude-x")
OAI = ModelCtx(provider="openai", api="openai-completions", model_id="gpt-x")


def test_anthropic_round_trip_equiv():
    live = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"p": "a"}},
            {"type": "tool_use", "id": "t2", "name": "grep_search", "input": {"q": "x"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "file a"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "match"},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    got = _roundtrip(live, "anthropic", ANTH)
    assert norm_anthropic(got) == norm_anthropic(live)


def test_openai_round_trip_equiv():
    live = [
        {"role": "system", "content": "SYS PROMPT"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "ok",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "run", "arguments": '{"x":1}'}}]},  # 紧凑 JSON（无空格）
        {"role": "tool", "tool_call_id": "t1", "content": "done"},
        {"role": "assistant", "content": "final"},
    ]
    got = _roundtrip(live, "openai", OAI)
    # arguments JSON 空格差异由 norm（json.loads）抹平
    assert norm_openai(got) == norm_openai(live)


def test_anthropic_multiturn_plain_text():
    live = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": [{"type": "text", "text": "a2"}]},
    ]
    got = _roundtrip(live, "anthropic", ANTH)
    assert norm_anthropic(got) == norm_anthropic(live)


def test_persists_to_tree_and_reopens_equal():
    live = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "yo"}]},
    ]
    mgr = SessionManager.create("p2sess", cwd="/tmp")
    for neutral in capture.capture_provider_messages(live, "anthropic", model="claude-x"):
        mgr.append_message(neutral)
    # reopen → build_context → render 仍等价（盘上 JSONL 重建）
    reopened = SessionManager.open("p2sess")
    got = render(reopened.build_context().messages, ANTH)["messages"]
    assert norm_anthropic(got) == norm_anthropic(live)


# ─── 边缘形状往返矩阵（workflow 复核改为永久测试） ──────────────────────────────
def test_anthropic_assistant_only_tool_use_with_result():
    live = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "R"}]},
    ]
    got = _roundtrip(live, "anthropic", ANTH)
    assert norm_anthropic(got) == norm_anthropic(live)


def test_anthropic_tool_result_is_error_preserved():
    live = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "boom", "is_error": True}]},
    ]
    got = _roundtrip(live, "anthropic", ANTH)
    assert norm_anthropic(got) == norm_anthropic(live)
    tr = [b for m in got if m["role"] == "user" and isinstance(m["content"], list) for b in m["content"]]
    assert tr[0]["is_error"] is True


def test_anthropic_image_block_in_user_round_trips():
    live = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "B64"}},
        {"type": "text", "text": "see"},
    ]}]
    got = _roundtrip(live, "anthropic", ANTH)
    assert norm_anthropic(got) == norm_anthropic(live)


def test_openai_assistant_content_none_with_tool_calls():
    live = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "run", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
    ]
    got = _roundtrip(live, "openai", OAI)
    assert norm_openai(got) == norm_openai(live)
