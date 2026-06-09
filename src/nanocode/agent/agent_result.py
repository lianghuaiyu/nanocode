"""子 agent 结果信封——纯函数（无 self / 无 IO / 无模型循环），自 engine 抽出（CAP-P1 STEP 1）。

- build_agent_result：宿主派生文件事实 + 解析的 summary/findings（**不信任模型自述文件**）。
- render_agent_result_envelope：有界、定形的父上下文信封（小文本直通 / 大文本截断 + 指针）。

Agent 保留 `_build_agent_result` / `_render_agent_result_envelope` 薄 shim（tests 与内部调用方
按方法名调用）。本模块**不 import engine**；parse_structured_result 在调用时 import（同原实现）。
"""

from __future__ import annotations

# 小结果直通阈值：raw text <= 此字节数时整段作为 summary 直通父上下文
# （concise explore/plan deliverable 不应被截断丢失）；超出则截断 + 指针。
ENVELOPE_PASSTHROUGH_BYTES = 4096
ENVELOPE_MAX_FINDINGS = 10
ENVELOPE_MAX_FILES = 10


def build_agent_result(sub_agent, text: str, tokens: dict, result_path: str | None) -> dict:
    """装配 AgentResult：宿主派生文件事实 + 模型自述 summary/findings（可选结构块解析，回退兜底）。

    files_read / files_modified 取自 SUB-AGENT 实例的观测集合（宿主派生，不信任模型）。
    summary / findings 由 parse_structured_result 解析子 agent 最终文本；无结构块则
    summary=首 ~500 字符、findings=[]。tokens 已折叠进父，仅展示。
    """
    from ..subagents.result import parse_structured_result
    parsed = parse_structured_result(text or "")
    files_read = sorted(getattr(sub_agent, "_files_read", None) or set())
    files_modified = sorted(getattr(sub_agent, "_files_modified", None) or set())
    return {
        "summary": parsed["summary"],
        "findings": parsed["findings"],
        "files_read": files_read,
        "files_modified": files_modified,
        "tokens": {"input": tokens.get("input", 0), "output": tokens.get("output", 0)},
        "result_path": result_path,
    }


def render_agent_result_envelope(result: dict, raw_text: str) -> str:
    """渲染**有界、定形**的信封——父上下文看到的就是这个（不再是整段 transcript）。

    规则：
    - raw_text 小（<= ~4KB）→ 整段直通作为 summary（concise deliverable 不丢失）；
      否则用模型 summary，并附 "... [truncated — full result at <path>, use read_file]" 指针。
    - 始终追加：top findings（cap ~10）、files_modified（cap ~10 名 + 溢出计数）、
      files_read 计数、tokens、result_path。
    - 无论 findings/files 多少都有界。
    """
    raw_text = raw_text or ""
    result_path = result.get("result_path")
    explicit_summary = (result.get("summary") or "").strip()
    small = len(raw_text.encode("utf-8")) <= ENVELOPE_PASSTHROUGH_BYTES
    if not raw_text.strip():
        # 空 transcript：若调用方已显式给了 summary（如超时/错误终态的原因），用它；
        # 否则给"无输出"提示。两种都仍带 result_path 指针。
        if explicit_summary:
            body = explicit_summary + (f"\nFull result at {result_path}, use read_file"
                                       if result_path else "")
        else:
            body = (f"(sub-agent produced no output; see {result_path})"
                    if result_path else "(sub-agent produced no output)")
    elif small:
        body = raw_text.strip()
    else:
        summary = explicit_summary or "(no summary)"
        pointer = (f"\n... [truncated — full result at {result_path}, use read_file]"
                   if result_path else
                   "\n... [truncated — full result not persisted]")
        body = summary + pointer

    lines = [body]

    findings = result.get("findings") or []
    if findings:
        shown = findings[:ENVELOPE_MAX_FINDINGS]
        lines.append("\nFindings:")
        lines.extend(f"  - {f}" for f in shown)
        if len(findings) > len(shown):
            lines.append(f"  - (+{len(findings) - len(shown)} more)")

    modified = result.get("files_modified") or []
    if modified:
        shown = modified[:ENVELOPE_MAX_FILES]
        lines.append("\nFiles modified:")
        lines.extend(f"  - {p}" for p in shown)
        if len(modified) > len(shown):
            lines.append(f"  - (+{len(modified) - len(shown)} more)")

    read_count = len(result.get("files_read") or [])
    tok = result.get("tokens") or {}
    lines.append(f"\nFiles read: {read_count}")
    lines.append(f"Tokens: {tok.get('input', 0)} in / {tok.get('output', 0)} out")
    lines.append(f"Result: {result_path or '(not persisted)'}")
    return "\n".join(lines)
