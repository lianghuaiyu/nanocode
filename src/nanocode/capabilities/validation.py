"""capabilities/validation.py — public tool input 的严格校验（docs/19 §4.2 / §10.1）。

模型 raw input 在进入 permission **之前**必须先变成 validated public args。Pi 的参考即
validate-before-`beforeToolCall`（packages/agent/src/agent-loop.ts）：工具执行只拿 validated args。

校验规则（fail-closed，绝不 silent strip）：

1. 以 `_` 开头的 key → reject（封死 `_cwd` / `_session_id` 这类隐藏字段从模型侧注入）。
2. 已知工具（spec.TOOLS）schema `additionalProperties: false` 时，unknown key → reject。
3. required key 缺失 / 为 None → reject。
4. 声明了 type 的 key 类型不符 → reject。

MCP / 未登记工具无本地 schema → 跳过 2-4（但 1 仍生效：下划线键一律拒）。

放置：`CapabilityRouter.dispatch()` 顶部（覆盖所有真实执行入口，含流式早执行）+
`engine._authorize_dispatch` 顶部（使 permission 看到 validated args）。两处共用此纯函数。
"""

from __future__ import annotations


_PY_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list}


def _type_ok(value, json_type: str) -> bool:
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    py = _PY_TYPES.get(json_type)
    if py is None:
        return True  # 未知/未声明类型不强校验
    if py is bool:
        return isinstance(value, bool)
    if py is str:
        return isinstance(value, str)
    return isinstance(value, py)


def validate_tool_input(name: str, inp) -> str | None:
    """校验一次工具调用的 public args。通过返回 None；否则返回拒绝文案（不抛）。"""
    # 1. 下划线键一律拒（普适，含 MCP）——封死隐藏字段注入。
    if isinstance(inp, dict):
        for key in inp:
            if isinstance(key, str) and key.startswith("_"):
                return (f"rejected tool input for '{name}': key '{key}' is not allowed "
                        f"(leading underscore keys are reserved for the runtime).")

    from ..tools.spec import TOOLS  # lazy：避免 capabilities ↔ tools 包级 import 环
    spec = TOOLS.get(name)
    if spec is None:
        return None  # MCP / 未登记：无本地 schema，结构校验跳过（下划线键已挡）

    if not isinstance(inp, dict):
        return f"rejected tool input for '{name}': expected an object of arguments."

    schema = spec.schema.get("input_schema", {}) or {}
    props = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []
    closed = schema.get("additionalProperties", True) is False

    if closed:
        for key in inp:
            if key not in props:
                return f"rejected tool input for '{name}': unknown key '{key}'."
    for key in required:
        if key not in inp or inp[key] is None:
            return f"rejected tool input for '{name}': missing required key '{key}'."
    for key, value in inp.items():
        if key in props:
            json_type = props[key].get("type")
            if json_type and not _type_ok(value, json_type):
                return f"rejected tool input for '{name}': key '{key}' must be of type {json_type}."
    return None
