"""工具级 hooks 的纯逻辑：frontmatter 规范化、matcher 匹配、事件 JSON 构造。"""
from __future__ import annotations

VALID_HOOK_EVENTS = ("pre-tool-use", "post-tool-use")


def normalize_hooks(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, list] = {}
    for event, entries in raw.items():
        if event not in VALID_HOOK_EVENTS or not isinstance(entries, list):
            continue
        norm = []
        for e in entries:
            if not isinstance(e, dict) or not e.get("command"):
                continue
            m = e.get("matcher", "*")
            if isinstance(m, str):
                matcher = [m]
            elif isinstance(m, list):
                matcher = [str(x) for x in m]
            else:
                matcher = ["*"]
            try:
                timeout_ms = int(e.get("timeout", 30000))
            except (TypeError, ValueError):
                timeout_ms = 30000
            norm.append({"matcher": matcher, "command": str(e["command"]), "timeout_ms": timeout_ms})
        if norm:
            out[event] = norm
    return out or None


def hook_matches(matcher: list[str], tool_name: str) -> bool:
    return "*" in matcher or tool_name in matcher


def build_hook_event(event, skill, tool_name, inp, result, cwd, session_id) -> dict:
    return {"event": event, "skill": skill, "tool": tool_name,
            "input": inp, "result": result, "cwd": cwd, "session_id": session_id}
