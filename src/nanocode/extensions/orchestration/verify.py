"""orchestration/verify.py — acceptance-gate 的两类验证器（docs/26 §0.6 策略库）。

- ``parse_verdict``：解析 reviewer agent 的裁决 JSON ``{"accept":bool,"feedback":str}``。
- ``validate_schema``：把 worker 输出当 JSON 校验其结构（轻量、无依赖；非完整 JSON Schema）。

二者都**绝不抛**：解析/校验失败归一为「不接受 + 可读反馈」，由 _accept 的 max_rounds 收敛。
JSON 抽取复用 ``memory/maintenance.extract_json_object``（与 memory_evolution diagnostician 同先例）。
"""
from __future__ import annotations

import json

_OPEN = {"{": "}", "[": "]"}


def _extract_json(text: str):
    """text → Python 对象（JSON）。抓第一个 ``{`` 或 ``[`` 到其平衡闭合（计入字符串/转义，
    跳过另一种括号），交给 ``json.loads``。无括号/坏 JSON → 抛（由调用方归一）。

    对象-only 的 ``memory.maintenance.extract_json_object`` 不认顶层数组（planner 常输出
    ``[{...},{...}]``），故此处自带 bracket-aware 抽取。"""
    text = text or ""
    start, opener = -1, ""
    for i, c in enumerate(text):
        if c in _OPEN:
            start, opener = i, c
            break
    if start == -1:
        return json.loads(text)
    close = _OPEN[opener]
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == opener:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    return json.loads(text[start:])


def _snippet(text: str, limit: int = 200) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[:limit].rstrip() + "..."


def parse_verdict(text: str) -> "tuple[bool, str]":
    """reviewer 裁决 → ``(accept, feedback)``。坏裁决 → ``(False, '… unparseable …')``（不接受，续轮）。"""
    try:
        data = _extract_json(text)
    except Exception:
        return False, f"reviewer verdict unparseable (expected JSON {{accept, feedback}}): {_snippet(text)}"
    if not isinstance(data, dict) or "accept" not in data:
        return False, f"reviewer verdict missing 'accept' boolean: {_snippet(text)}"
    return bool(data.get("accept")), str(data.get("feedback") or "")


# ─── 轻量结构 schema 校验（type/required/properties/items 子集，非完整 JSON Schema）─────

_TYPE_CHECK = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    # bool 是 int 子类——number/integer 须排除 bool。
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
}


def _typename(v) -> str:
    return type(v).__name__


def _check(obj, schema, *, path: str = "") -> "list[str]":
    if not isinstance(schema, dict):
        return []
    errs: list[str] = []
    t = schema.get("type")
    if t is not None:
        checker = _TYPE_CHECK.get(t)
        if checker is None:
            return errs  # 不支持的 type 关键字：忽略（不报错，文档化为子集）
        if not checker(obj):
            return [f"{path or 'output'}: expected type {t}, got {_typename(obj)}"]
    if t == "object" and isinstance(obj, dict):
        for req in schema.get("required", []) or []:
            if req not in obj:
                errs.append(f"{path or 'output'}: missing required key '{req}'")
        for key, sub in (schema.get("properties") or {}).items():
            if key in obj:
                errs.extend(_check(obj[key], sub, path=f"{path}.{key}" if path else key))
    if t == "array" and isinstance(obj, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, it in enumerate(obj):
                errs.extend(_check(it, items, path=f"{path}[{i}]"))
    return errs


def validate_schema(text: str, schema: dict) -> "list[str]":
    """把 worker 输出当 JSON 校验 ``schema``；返回错误列表（空=通过）。坏 JSON → 单条错。"""
    try:
        obj = _extract_json(text)
    except Exception:
        return [f"output is not valid JSON: {_snippet(text)}"]
    return _check(obj, schema)
