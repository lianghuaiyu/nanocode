"""子 agent 结构化结果：宿主派生事实 + 模型自述判断。

设计要点（host derives facts; model authors judgement）：
- summary / findings：模型自述（可选结构块），缺省时宿主回退（首 ~500 字符 / []）。
- files_read / files_modified：宿主**观测**派生，绝不信任模型。
- 解析器永不抛、且对任意大输入有界（只扫描尾部窗口，绝不在巨串上跑灾难性正则）。

模型可在最终消息末尾**可选**附一个结构块，二选一：
  1) 一个 fenced 代码块，info string 含 ``agent-result``，内容为 JSON：
     {"summary": "...", "findings": ["...", "..."]}
  2) 一个 markdown ``## Summary`` / ``## Findings`` 段（findings 为 - / * / 数字列表）。
两者都没有 → 回退（summary=首 ~500 字符，findings=[]）。绝非强制。
"""
from __future__ import annotations

import json
import re

# 摘要回退长度（首 ~500 字符）。
SUMMARY_FALLBACK_CHARS = 500
# 模型自述 summary 的硬上限：信封 body 必须有界，与模型是否配合无关
# （否则一个 12KB 的模型 summary 会原样撑进父上下文，挫败"有界"目标）。
SUMMARY_MAX_CHARS = 2000
# 解析器只在文本尾部这个窗口内找结构块——保证对巨型 transcript 有界（无灾难性回溯）。
_PARSE_TAIL_WINDOW = 16000
# findings 单条上限（防止单个超长行撑爆注入）。
_FINDING_MAX_CHARS = 500


def _cap_summary(summary: str) -> str:
    """模型/markdown summary 硬截断到 SUMMARY_MAX_CHARS（带省略号指针）。"""
    s = (summary or "").strip()
    if len(s) <= SUMMARY_MAX_CHARS:
        return s
    return s[:SUMMARY_MAX_CHARS].rstrip() + " …[summary truncated]"


def _fallback_summary(text: str) -> str:
    return (text or "").strip()[:SUMMARY_FALLBACK_CHARS]


def _coerce_findings(raw) -> list[str]:
    """把模型给的 findings 规整成 list[str]：丢弃非字符串、去空白、截断、cap 总数在调用处做。"""
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, str):
            # 容忍 {text: "..."} 之类的轻微变体，否则跳过。
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                item = item["text"]
            else:
                continue
        s = item.strip()
        if s:
            out.append(s[:_FINDING_MAX_CHARS])
    return out


def _parse_fenced_agent_result(tail: str) -> dict | None:
    """在尾部窗口内找一个 info string 含 ``agent-result`` 的 fenced 块，解析其 JSON。

    匹配 ``` 或 ~~~ 围栏；info 行只要包含 'agent-result' token 即可（容忍
    ```agent-result / ```json agent-result 等）。解析失败/非 dict → None。
    """
    # 非贪婪、行锚定的围栏匹配；DOTALL 让 . 跨行吃 body。
    # 只在 tail 上跑 → 输入有界，无灾难性回溯风险。
    pattern = re.compile(
        r"(?:^|\n)[ \t]*(`{3,}|~{3,})[ \t]*([^\n]*agent-result[^\n]*)\n(.*?)\n[ \t]*\1",
        re.DOTALL,
    )
    last = None
    for m in pattern.finditer(tail):
        last = m  # 取最后一个（最贴近文末的结构块）
    if last is None:
        return None
    body = last.group(3)
    try:
        data = json.loads(body)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    summary = data.get("summary")
    return {
        "summary": summary.strip() if isinstance(summary, str) else None,
        "findings": _coerce_findings(data.get("findings")),
    }


def _parse_markdown_sections(tail: str) -> dict | None:
    """解析 ``## Summary`` / ``## Findings`` markdown 段（任一存在即视为结构化）。

    - Summary：标题下直到下一个 ## 标题或文末的纯文本。
    - Findings：标题下的列表项（- / * / 数字.）。
    缺省字段 → None / []。两段都缺 → None（让调用方回退）。
    """
    def _section(name: str) -> str | None:
        m = re.search(
            rf"(?:^|\n)#{{1,6}}[ \t]*{name}[ \t]*\n(.*?)(?=\n#{{1,6}}[ \t]|\Z)",
            tail, re.IGNORECASE | re.DOTALL,
        )
        return m.group(1) if m else None

    summary_block = _section("Summary")
    findings_block = _section("Findings")
    if summary_block is None and findings_block is None:
        return None

    summary = summary_block.strip() if summary_block else None

    findings: list[str] = []
    if findings_block:
        for line in findings_block.splitlines():
            s = line.strip()
            if not s:
                continue
            # 剥列表标记：- / * / + / "1." / "1)"
            m = re.match(r"^(?:[-*+]|\d+[.)])[ \t]+(.*)$", s)
            text = m.group(1).strip() if m else s
            if text:
                findings.append(text[:_FINDING_MAX_CHARS])

    return {"summary": summary, "findings": findings}


def parse_structured_result(text: str) -> dict:
    """解析可选结构块，返回 {"summary": str, "findings": list[str]}。

    顺序：fenced agent-result JSON → markdown ## Summary/## Findings → 回退。
    回退：summary=首 ~500 字符，findings=[]。永不抛；对任意大输入有界。
    """
    text = text or ""
    tail = text[-_PARSE_TAIL_WINDOW:] if len(text) > _PARSE_TAIL_WINDOW else text

    parsed = None
    try:
        parsed = _parse_fenced_agent_result(tail)
        if parsed is None:
            parsed = _parse_markdown_sections(tail)
    except Exception:
        parsed = None  # 解析器绝不影响主流程

    if parsed is None:
        return {"summary": _fallback_summary(text), "findings": []}

    summary = parsed.get("summary")
    if not summary:
        summary = _fallback_summary(text)
    else:
        summary = _cap_summary(summary)   # 模型自述也必须有界
    return {"summary": summary, "findings": list(parsed.get("findings") or [])}
