"""redaction：trajectory summary 级的盘上 payload 整形 / 脱敏 helper。

纯叶子模块，**不**依赖 nanocode 任何子系统（可被 trace.tracer import）。所有函数
defensive：绝不抛（instrumentation 绝不影响 agent loop）。

边界（docs/10 + WIRE 契约）：
- FULL 级别 / trajectory 关闭：payload 原样不动（与今天 byte-identical）。
- SUMMARY 级别：丢弃重型 payload（messages / 大 result），只保留摘要 + hash + 长度，
  使 wire 仍可审计/投影，但 event-tree rebuild 会退化为 snapshot（已由 SessionContextBuilder 处理）。

**整形范围（刻意收窄）**：当前只整形 ``llm_request.messages`` 与 ``tool_result.result``
这两个最大的重型 payload。``tool_call.input``（如 write_file 的 content、run_shell 的命令）
与 ``assistant_message.text``/``tool_uses`` 在 SUMMARY 级别**仍保留全量**——投影层（metrics
的 files_touched/tests、project 的 args_summary）依赖这些字段，且默认 always-on wire 本就
按全量落盘。结论：SUMMARY 是相对默认 wire 的**隐私收敛**（去掉了最大的 prompt/输出 payload），
但并非完全脱敏；勿把密钥作为工具参数传入。完全输入脱敏留作后续工作。
"""
from __future__ import annotations

import json

# SUMMARY 级 tool_result 摘要的字符上限。刻意**远小于** truncate 的默认 1000：result_summary
# 是会落进 durable wire 的工具输出片段，1000 字头部足以裹挟密钥/敏感输出（codex 复审 MED）。
# 取一个短头部——既能保留错误信息可读性（多数 "Error: ..." 开头在前 200 字内，供 eval 的
# tool_error 启发式与人审），又把泄漏面收到最小。完整内容只在 FULL 级（payload 原样）出现。
SUMMARY_RESULT_SNIPPET_CHARS = 200


def payload_hash(obj) -> str:
    """对任意可序列化对象算稳定 sha256：``"sha256:<hex>"``。失败返回 ""（绝不抛）。"""
    try:
        raw = json.dumps(obj, ensure_ascii=False, default=str, sort_keys=True)
    except Exception:
        return ""
    try:
        import hashlib

        return "sha256:" + hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()
    except Exception:
        return ""


def truncate(text: str, n: int = 1000) -> str:
    """把文本截断到至多 n 字符（超出加省略号标记）。非字符串先 str()。绝不抛。"""
    try:
        s = text if isinstance(text, str) else str(text)
    except Exception:
        return ""
    if len(s) <= n:
        return s
    return s[:n] + f"... [+{len(s) - n} chars]"


def apply_summary_shaping(event: dict) -> None:
    """SUMMARY 级别在 emit 时对事件 dict 做**就地**整形：丢弃重型 payload，补摘要 + hash。

    按 event["type"] 分派；只整形已知重型字段，其余字段保持不变。绝不抛。

    - ``llm_request`` 且含 ``messages``：pop ``messages``；补 ``messages_chars``（messages 的
      json 串长度）、``messages_hash``；保留既有 ``message_count``。
    - ``tool_result`` 且含 ``result``：pop ``result``；补 ``result_summary``（str(result) 截断到
      ``SUMMARY_RESULT_SNIPPET_CHARS``，限制密钥泄漏面）、``result_hash``；保留既有 ``chars``。
    """
    try:
        etype = event.get("type")
        if etype == "llm_request" and "messages" in event:
            messages = event.pop("messages")
            try:
                serialized = json.dumps(messages, ensure_ascii=False, default=str)
            except Exception:
                serialized = ""
            event["messages_chars"] = len(serialized)
            event["messages_hash"] = payload_hash(messages)
        elif etype == "tool_result" and "result" in event:
            result = event.pop("result")
            # truncate() 自身做 str 强转 + defensive 兜底，无需在此重复
            event["result_summary"] = truncate(result, SUMMARY_RESULT_SNIPPET_CHARS)
            event["result_hash"] = payload_hash(result)
    except Exception:
        return
