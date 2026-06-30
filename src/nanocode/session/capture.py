"""session/capture.py — live provider 消息 dict → 中立 Message（render.py 的逆向）。

P2 双写脚手架用：把 engine 现有的 provider-shaped 列表（_anthropic_messages / _openai_messages）
反向捕获成中立 Message，落进 canonical 树。与 render.py 构成往返：
    capture(provider list) → 中立 Message[] → build_context → render → provider list'（归一后等价）

注（docs/13）：① live list 不存 stopReason → 按内容推断（有 tool_use/tool_calls → "toolUse" 否则
"stop"）；真 abort 的 stopReason 是 P2b backend 捕获，非此处。② Anthropic 现状剥 thinking（backend），
故 live list 无 thinking 块——capture 也看不到。③ OpenAI system 消息不入树（render 侧 Context）。
纯函数。
"""

from __future__ import annotations

import json

from . import tree

ANTHROPIC = "anthropic"
OPENAI = "openai"


def _infer_stop(has_tools: bool) -> str:
    return "toolUse" if has_tools else "stop"


# provider 原生 stop/finish reason → 中立 stopReason（docs/14 §4.3 bug#2：忠实捕获，不再纯内容推断）。
# Anthropic StopReason 全集 = {end_turn,max_tokens,stop_sequence,tool_use,pause_turn,refusal}（SDK Literal）：
# 正常终止族 → stop，tool_use → toolUse，max_tokens → maxTokens。pause_turn（服务端要求续跑）当 stop；
# refusal（策略拒答，仍是完成的 assistant turn）当 stop（render 不丢，positional de-orphan 仍兜底）。
# error/aborted/content_filter 等未知值 verbatim 透传（render 据 "error"/"aborted" 丢弃半成品 turn）。
_ANTHROPIC_STOP = {"tool_use": "toolUse", "max_tokens": "maxTokens",
                   "end_turn": "stop", "stop_sequence": "stop",
                   "pause_turn": "stop", "refusal": "stop"}
_OPENAI_FINISH = {"tool_calls": "toolUse", "length": "maxTokens", "stop": "stop"}


def neutral_stop_reason(provider: str, raw: "str | None") -> "str | None":
    if not raw:
        return None
    table = _OPENAI_FINISH if provider == OPENAI else _ANTHROPIC_STOP
    return table.get(raw, raw)


def _anthropic_block_to_neutral(b: dict) -> dict | None:
    t = b.get("type")
    if t == "text":
        return tree.text_block(b.get("text", ""))
    if t == "tool_use":
        return tree.tool_call_block(b.get("id", ""), b.get("name", ""),
                                    dict(b.get("input") or {}))
    if t == "thinking":
        return tree.thinking_block(b.get("thinking", ""), signature=b.get("signature"))
    if t == "redacted_thinking":
        return tree.thinking_block("", signature=b.get("data", ""), redacted=True)
    if t == "image":
        src = b.get("source") or {}
        return tree.image_block(src.get("data", ""), src.get("media_type", ""))
    return None


def _is_tool_result_user(msg: dict) -> bool:
    """Anthropic 把一轮的 tool_result 们装进一条 {role:user, content:[{type:tool_result}...]}。"""
    c = msg.get("content")
    return (msg.get("role") == "user" and isinstance(c, list) and bool(c)
            and isinstance(c[0], dict) and c[0].get("type") == "tool_result")


def capture_anthropic(msg: dict, *, model: str, api: str = ANTHROPIC, stop_reason: "str | None" = None,
                      usage: dict | None = None, latency_ms: "int | None" = None) -> list[dict]:
    """一条 anthropic live 消息 → 0+ 条中立 Message（tool_result-user 拆成多条 toolResult）。
    stop_reason 给定（已是中立值）时忠实采用；否则按内容推断（toolUse/stop）。
    usage/latency_ms（docs/14 Milestone B）：assistant 消息携带 per-call token 用量 + 延迟；
    latency_ms 也透传给 toolResult（per-tool 延迟，对 LLM 不可见，trajectory 派生用）。"""
    role = msg.get("role")
    if role == "assistant":
        content = msg.get("content")
        if isinstance(content, str):                     # string content（OpenAI 形态/旧快照）→ 单 text 块
            blocks = [tree.text_block(content)] if content else []
        else:
            blocks = [nb for b in (content or []) if (nb := _anthropic_block_to_neutral(b))]
        has_tools = any(b.get("type") == "toolCall" for b in blocks)
        return [tree.assistant_message(blocks, provider=ANTHROPIC, api=api, model=model,
                                       stop_reason=stop_reason or _infer_stop(has_tools),
                                       usage=usage, latency_ms=latency_ms)]
    if _is_tool_result_user(msg):
        out = []
        for blk in msg["content"]:
            if isinstance(blk, dict) and blk.get("type") == "tool_result":
                out.append(tree.tool_result_message(
                    tool_call_id=blk.get("tool_use_id", ""), tool_name=blk.get("toolName", ""),
                    content=blk.get("content", ""), is_error=bool(blk.get("is_error", False)),
                    latency_ms=blk.get("toolLatencyMs", latency_ms)))   # per-block 延迟优先（B1）
        return out
    if role == "user":
        content = msg.get("content")
        if isinstance(content, list):
            content = [nb for b in content if (nb := _anthropic_block_to_neutral(b))]
        return [tree.user_message(content)]
    return []


def capture_openai(msg: dict, *, model: str, api: str = "openai-completions", stop_reason: "str | None" = None,
                   usage: dict | None = None, latency_ms: "int | None" = None) -> list[dict]:
    role = msg.get("role")
    if role == "system":
        return []  # system 不入树（render 侧 Context，docs/13 §3.2 M5）
    if role == "user":
        return [tree.user_message(msg.get("content") or "")]
    if role == "assistant":
        blocks: list[dict] = []
        text = msg.get("content")
        if text:
            blocks.append(tree.text_block(text))
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            raw_bad = None
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    raw_bad = args          # 解析失败：保留原始串供审计（docs/14 §5.3），不静默吞成 {}
                    args = {}
            blk = tree.tool_call_block(tc.get("id", ""), fn.get("name", ""), args or {})
            if raw_bad is not None:
                blk["argumentsRaw"] = raw_bad   # render 只读 "arguments"，此审计字段被忽略、不影响回放
            blocks.append(blk)
        has_tools = bool(msg.get("tool_calls"))
        return [tree.assistant_message(blocks, provider=OPENAI, api=api, model=model,
                                       stop_reason=stop_reason or _infer_stop(has_tools),
                                       usage=usage, latency_ms=latency_ms)]
    if role == "tool":
        return [tree.tool_result_message(tool_call_id=msg.get("tool_call_id", ""),
                                         tool_name="", content=msg.get("content", ""),
                                         latency_ms=latency_ms)]
    return []


def capture_provider_messages(messages: list[dict], provider: str, *, model: str = "") -> list[dict]:
    """整列 provider 消息 → 中立 Message[]（顺序保持；anthropic tool_result-user 会展开成多条）。"""
    cap = capture_openai if provider == OPENAI else capture_anthropic
    out: list[dict] = []
    for m in messages or []:
        if isinstance(m, dict):
            out.extend(cap(m, model=model))
    return out
