"""session/render.py — 中立 Message[] → provider-合法 payload（docs/13 §4 第三段）。

双移植（评审 B3）：
  Pass A  cross-provider normalize（Pi transform-messages.ts 端口）：
          thinking gate / image placeholder / id 归一 / **丢 aborted assistant** /
          **删 inverse-orphan tool_result（nanocode 新增，Pi 不做）** / 合成 forward-orphan tool_result。
  Pass B  per-provider shaping（Pi 各 provider convertMessages 端口）：
          Anthropic 多 tool_result 并进一条 user 消息 + 空块丢弃；OpenAI tool-role + system。

「存全事实、render 严格 gate」：存储层忠实存（含 thinking/签名/aborted），这里决定能否安全发回。
统一输出：{"messages": [...provider dicts...], "system": str | None}。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_IMG_PLACEHOLDER = "(image omitted: model does not support images)"
_NO_RESULT = "No result provided"


@dataclass
class ModelCtx:
    provider: str
    api: str
    model_id: str
    supports_images: bool = True
    allow_empty_signature: bool = False


def _same_model(msg: dict, ctx: ModelCtx) -> bool:
    return (msg.get("provider") == ctx.provider and msg.get("api") == ctx.api
            and msg.get("model") == ctx.model_id)


def _normalize_id(tool_id: str, ctx: ModelCtx) -> str:
    """provider-specific tool-call id 归一（评审 B3：Anthropic 64-char ^[A-Za-z0-9_-]$；OpenAI 40-char pipe-split）。"""
    if ctx.provider == "openai" or ctx.api in ("openai", "openai-completions", "openai-responses"):
        return tool_id.split("|", 1)[0][:40]
    return re.sub(r"[^a-zA-Z0-9_-]", "_", tool_id)[:64]


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif b.get("type") == "image":
                parts.append(_IMG_PLACEHOLDER)
        return "".join(parts)
    return ""


# ─── Pass A: cross-provider normalize ────────────────────────────────────────
def _gate_assistant_content(msg: dict, ctx: ModelCtx) -> list[dict]:
    """thinking gate（§4⑦ / transform-messages.ts:90-131）+ image 不在此（assistant 无 image）。"""
    same = _same_model(msg, ctx)
    out: list[dict] = []
    for b in msg.get("content", []):
        t = b.get("type")
        if t == "thinking":
            if b.get("redacted"):
                if same:
                    out.append(b)  # redacted 仅同模型保留
                continue
            sig = b.get("thinkingSignature")
            thinking = b.get("thinking", "")
            if same and sig:
                out.append(b)  # 同模型 + 有签名 → 原样保留（可重放）
            elif same and ctx.allow_empty_signature and thinking.strip():
                out.append(b)
            elif thinking.strip():
                out.append({"type": "text", "text": thinking})  # 否则降级 text
            # 空 thinking → 丢
        elif t == "toolCall":
            nb = dict(b)
            if not same and "thoughtSignature" in nb:
                nb.pop("thoughtSignature", None)  # 跨模型删 thoughtSignature
            out.append(nb)
        else:
            out.append(b)
    return out


def normalize(messages: list[dict], ctx: ModelCtx) -> list[dict]:
    """Pass A：产出仍为中立 Message[]，但已 provider-合法可渲染。

    顺序：① 逐条 gate（thinking/image/id 归一，丢 aborted assistant）→ ② 删 inverse-orphan
    tool_result → ③ 合成 forward-orphan tool_result（positional，在 user/assistant 边界与末尾）。
    """
    id_map: dict[str, str] = {}

    # ① 逐条 transform；丢 aborted/errored assistant。
    staged: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            if m.get("stopReason") in ("error", "aborted"):
                continue  # 丢半成品 turn（Pi transform-messages.ts:186-194）
            content = _gate_assistant_content(m, ctx)
            new_content = []
            for b in content:
                if b.get("type") == "toolCall":
                    nid = _normalize_id(b["id"], ctx)
                    if nid != b["id"]:
                        id_map[b["id"]] = nid
                        b = {**b, "id": nid}
                new_content.append(b)
            staged.append({**m, "content": new_content})
        elif role == "user":
            content = m.get("content")
            if not ctx.supports_images and isinstance(content, list):
                content = _downgrade_images(content)
            staged.append({**m, "content": content})
        elif role == "toolResult":
            tcid = m.get("toolCallId")
            tcid = id_map.get(tcid, tcid)
            content = m.get("content")
            if not ctx.supports_images and isinstance(content, list):
                content = _downgrade_images(content)
            staged.append({**m, "toolCallId": tcid, "content": content})
        else:
            staged.append(m)

    # ② 存活 tool_call id 集合 → 删 inverse-orphan（评审 blocker：丢 aborted 后留下的孤儿 result）。
    surviving = {b["id"] for m in staged if m.get("role") == "assistant"
                 for b in m.get("content", []) if b.get("type") == "toolCall"}
    deorphaned = [m for m in staged
                  if not (m.get("role") == "toolResult" and m.get("toolCallId") not in surviving)]

    # ③ 合成 forward-orphan（tool_call 无对应 result）：positional 在边界与末尾补 error result。
    out: list[dict] = []
    pending: list[tuple[str, str]] = []   # (toolCallId, toolName)
    seen_results: set[str] = set()

    def flush() -> None:
        for cid, name in pending:
            if cid not in seen_results:
                out.append({"role": "toolResult", "toolCallId": cid, "toolName": name,
                            "content": _NO_RESULT, "isError": True})
        pending.clear()
        seen_results.clear()

    for m in deorphaned:
        role = m.get("role")
        if role == "assistant":
            flush()
            out.append(m)
            pending.extend((b["id"], b.get("name", "")) for b in m.get("content", [])
                           if b.get("type") == "toolCall")
        elif role == "toolResult":
            seen_results.add(m.get("toolCallId"))
            out.append(m)
        elif role == "user":
            flush()
            out.append(m)
        else:
            out.append(m)
    flush()
    return out


def _downgrade_images(content: list[dict]) -> list[dict]:
    out: list[dict] = []
    prev_ph = False
    for b in content:
        if isinstance(b, dict) and b.get("type") == "image":
            if not prev_ph:
                out.append({"type": "text", "text": _IMG_PLACEHOLDER})
            prev_ph = True
            continue
        out.append(b)
        prev_ph = isinstance(b, dict) and b.get("type") == "text" and b.get("text") == _IMG_PLACEHOLDER
    return out


# ─── Pass B: per-provider shaping ────────────────────────────────────────────
def _anthropic_blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    out: list[dict] = []
    for b in content or []:
        t = b.get("type")
        if t == "text":
            out.append({"type": "text", "text": b.get("text", "")})
        elif t == "thinking":
            if b.get("redacted"):
                out.append({"type": "redacted_thinking", "data": b.get("thinkingSignature", "")})
            else:
                out.append({"type": "thinking", "thinking": b.get("thinking", ""),
                            "signature": b.get("thinkingSignature", "")})
        elif t == "image":
            out.append({"type": "image", "source": {"type": "base64",
                        "media_type": b.get("mimeType", ""), "data": b.get("data", "")}})
        elif t == "toolCall":
            out.append({"type": "tool_use", "id": b["id"], "name": b.get("name", ""),
                        "input": b.get("arguments", {})})
    return out


def _render_anthropic(messages: list[dict], system: str | None) -> dict:
    out: list[dict] = []
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        role = m.get("role")
        if role == "toolResult":  # 合并连续 toolResult → 一条 user 消息（anthropic.ts:1113-1146）
            block_group = []
            while i < n and messages[i].get("role") == "toolResult":
                tr = messages[i]
                content = tr.get("content")
                rc = content if isinstance(content, str) else _anthropic_blocks(content)
                block_group.append({"type": "tool_result", "tool_use_id": tr.get("toolCallId"),
                                    "content": rc, "is_error": bool(tr.get("isError"))})
                i += 1
            out.append({"role": "user", "content": block_group})
            continue
        if role == "user":
            content = m.get("content")
            out.append({"role": "user", "content": content if isinstance(content, str)
                        else _anthropic_blocks(content)})
        elif role == "assistant":
            blocks = _anthropic_blocks(m.get("content"))
            if blocks:  # 空 assistant（无内容无 tool_use）丢弃
                out.append({"role": "assistant", "content": blocks})
        i += 1
    return {"messages": out, "system": system}


def _render_openai(messages: list[dict], system: str | None) -> dict:
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": _content_to_text(m.get("content"))})
        elif role == "toolResult":
            out.append({"role": "tool", "tool_call_id": m.get("toolCallId"),
                        "content": _content_to_text(m.get("content"))})
        elif role == "assistant":
            text = "".join(b.get("text", "") for b in m.get("content", []) if b.get("type") == "text")
            tool_calls = [{"id": b["id"], "type": "function",
                           "function": {"name": b.get("name", ""),
                                        "arguments": json.dumps(b.get("arguments", {}))}}
                          for b in m.get("content", []) if b.get("type") == "toolCall"]
            msg: dict = {"role": "assistant", "content": text or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            if text or tool_calls:  # 空 assistant 丢弃
                out.append(msg)
    return {"messages": out, "system": None}


def _merge_consecutive_users(msgs: list[dict], *, is_openai: bool) -> list[dict]:
    """合并相邻 user 消息（S2/P5：custom_message 折成独立 user 消息后，与前一条 user 相邻——
    Anthropic 要求 user/assistant 交替，合并即复刻原「append-to-last-user」注入定位）。"""
    out: list[dict] = []
    for m in msgs:
        if m.get("role") == "user" and out and out[-1].get("role") == "user":
            prev = out[-1]
            if is_openai:
                prev["content"] = (str(prev.get("content") or "") + "\n\n"
                                   + str(m.get("content") or "")).strip()
            else:
                def _blocks(c):
                    if isinstance(c, list):
                        return list(c)
                    return [{"type": "text", "text": c}] if c else []
                prev["content"] = _blocks(prev.get("content")) + _blocks(m.get("content"))
        else:
            out.append(dict(m))
    return out


def render(messages: list[dict], ctx: ModelCtx, *, system_prompt: str | None = None) -> dict:
    """中立 Message[] → provider payload。返回 {"messages":[...], "system": str|None}。

    Anthropic：system 走 out-of-band（返回 system 字段）；OpenAI：system 为 messages[0]。
    """
    normalized = normalize(messages, ctx)
    is_openai = ctx.provider == "openai" or ctx.api in ("openai", "openai-completions", "openai-responses")
    payload = _render_openai(normalized, system_prompt) if is_openai else _render_anthropic(normalized, system_prompt)
    payload["messages"] = _merge_consecutive_users(payload["messages"], is_openai=is_openai)
    return payload
