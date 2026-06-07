"""权限判定：5 种权限模式 + .claude/settings.json 声明式 allow/deny 规则。"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

# ─── Permission modes ──────────────────────────────────────

PermissionMode = str  # "default" | "plan" | "acceptEdits" | "bypassPermissions" | "dontAsk"

READ_TOOLS = {"read_file", "list_files", "grep_search", "web_fetch"}
EDIT_TOOLS = {"write_file", "edit_file"}

# Concurrency-safe tools can run in parallel (read-only, no side effects)
CONCURRENCY_SAFE_TOOLS = {"read_file", "list_files", "grep_search", "web_fetch"}


# ─── Permission rules (.claude/settings.json) ───────────────


def _parse_rule(rule: str) -> dict:
    m = re.match(r"^([a-z_]+)\((.+)\)$", rule)
    if m:
        return {"tool": m.group(1), "pattern": m.group(2)}
    return {"tool": rule, "pattern": None}


def _load_settings(file_path: Path) -> dict | None:
    if not file_path.exists():
        return None
    try:
        return json.loads(file_path.read_text())
    except Exception:
        return None


_cached_rules: dict | None = None


def load_permission_rules() -> dict:
    global _cached_rules
    if _cached_rules is not None:
        return _cached_rules

    allow: list[dict] = []
    deny: list[dict] = []

    user_settings = _load_settings(Path.home() / ".claude" / "settings.json")
    project_settings = _load_settings(Path.cwd() / ".claude" / "settings.json")

    for settings in [user_settings, project_settings]:
        if not settings or "permissions" not in settings:
            continue
        perms = settings["permissions"]
        for r in perms.get("allow", []):
            allow.append(_parse_rule(r))
        for r in perms.get("deny", []):
            deny.append(_parse_rule(r))

    _cached_rules = {"allow": allow, "deny": deny}
    return _cached_rules


def _matches_rule(rule: dict, tool_name: str, inp: dict, *, is_allow: bool = False) -> bool:
    if rule["tool"] != tool_name:
        return False
    if rule["pattern"] is None:
        return True

    value = ""
    if tool_name in ("run_shell", "sandbox_shell"):
        value = inp.get("command", "")
    elif "file_path" in inp:
        value = inp["file_path"]
    else:
        return True

    pattern = rule["pattern"]
    if pattern.endswith("*"):
        # 前缀 allow 规则不得跨 shell 组合符延伸匹配：`allow run_shell(git pull*)`
        # 不能放行 `git pull && rm -rf ~`（否则压掉后段的 is_dangerous 确认）。
        # deny 规则保持激进（is_allow=False）；精确匹配 allow 不受影响。
        if (
            is_allow
            and tool_name in ("run_shell", "sandbox_shell")
            and any(c in _SHELL_COMPOSITION_CHARS for c in value)
        ):
            return False
        return value.startswith(pattern[:-1])
    return value == pattern


def _check_permission_rules(tool_name: str, inp: dict) -> str | None:
    rules = load_permission_rules()
    for rule in rules["deny"]:
        if _matches_rule(rule, tool_name, inp):  # deny：激进，不传 is_allow
            return "deny"
    for rule in rules["allow"]:
        if _matches_rule(rule, tool_name, inp, is_allow=True):  # allow：遇组合符 fail-closed
            return "allow"
    return None


# ─── Shell sandbox routing (实验性：NANOCODE_SHELL_SANDBOX=auto 开启) ──────────
#
# 只读且安全、可在宿主快速执行的命令前缀（按 token 前缀匹配，非整串 startswith，
# 故 "git status && curl evil" 不会命中——含 && 的命令不进快速通道）。
READONLY_SHELL_PREFIXES = (
    "ls", "pwd", "cat", "head", "tail", "wc", "echo", "which", "whoami",
    "grep", "rg", "find", "tree", "stat", "file", "date", "env", "printenv",
    "du", "df", "uname", "hostname", "id", "ps", "true",
    "git status", "git log", "git diff", "git show", "git branch",
    "git remote", "git rev-parse", "git describe",
    "python --version", "python3 --version", "pip --version",
    "node --version", "npm --version",
)

# 命令组合 / 重定向 / 替换字符——出现即不进只读快速通道（fail-closed，安全方向）。
_SHELL_COMPOSITION_CHARS = set("|&;<>$`(){}\n")


def shell_sandbox_mode() -> str:
    """'off'（默认）| 'auto'。控制 run_shell 是否经沙盒路由。"""
    val = (os.environ.get("NANOCODE_SHELL_SANDBOX") or "off").strip().lower()
    return val if val in ("off", "auto") else "off"


def is_readonly_command(command: str) -> bool:
    """命令是否为已知只读、可安全在宿主直跑（按 token 前缀白名单匹配）。
    含组合/重定向/替换字符或引号不平衡时一律返回 False（保守，落到沙盒）。"""
    cmd = (command or "").strip()
    if not cmd:
        return False
    if any(c in _SHELL_COMPOSITION_CHARS for c in cmd):
        return False
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False
    if not tokens:
        return False
    prefixes = {tokens[0], " ".join(tokens[:2]), " ".join(tokens[:3])}
    return any(p in prefixes for p in READONLY_SHELL_PREFIXES)


def classify_shell_runtime(command: str) -> str:
    """run_shell 命令的运行时归类：'host' 或 'sandbox'。
    - 路由关闭（off）→ host（保持旧行为）。
    - 危险命令 → host（确认后须作用于真实宿主，如 rm foo）。
    - 只读白名单 → host（快速直跑）。
    - 其余 → sandbox（默认进 microVM）。"""
    from .run_shell import is_dangerous

    if shell_sandbox_mode() != "auto":
        return "host"
    if not (command or "").strip():
        return "host"
    if is_dangerous(command):
        return "host"
    if is_readonly_command(command):
        return "host"
    return "sandbox"


def check_permission(
    tool_name: str,
    inp: dict,
    mode: str = "default",
    plan_file_path: str | None = None,
) -> dict:
    """Returns {"action": "allow"|"deny"|"confirm", "message": ...}"""
    from .run_shell import is_dangerous

    rule_result = _check_permission_rules(tool_name, inp)
    if rule_result == "deny":
        return {"action": "deny", "message": f"Denied by permission rule for {tool_name}"}

    if mode == "bypassPermissions":
        return {"action": "allow"}

    if rule_result == "allow":
        return {"action": "allow"}

    if tool_name in READ_TOOLS:
        return {"action": "allow"}

    if mode == "plan":
        if tool_name in EDIT_TOOLS:
            file_path = inp.get("file_path") or inp.get("path")
            if plan_file_path and file_path == plan_file_path:
                return {"action": "allow"}
            return {"action": "deny", "message": f"Blocked in plan mode: {tool_name}"}
        if tool_name in ("run_shell", "sandbox_shell"):
            return {"action": "deny", "message": "Shell commands blocked in plan mode"}

    if tool_name in ("enter_plan_mode", "exit_plan_mode"):
        return {"action": "allow"}

    if mode == "acceptEdits" and tool_name in EDIT_TOOLS:
        return {"action": "allow"}

    needs_confirm = False
    confirm_message = ""

    if tool_name == "run_shell" and shell_sandbox_mode() == "auto" and inp.get("escalate"):
        needs_confirm = True
        confirm_message = f"escalate to host (bypass sandbox): {inp.get('command', '')}"
    elif tool_name == "run_shell" and is_dangerous(inp.get("command", "")):
        needs_confirm = True
        confirm_message = inp.get("command", "")
    elif tool_name == "sandbox_shell" and inp.get("mount_workspace", False):
        needs_confirm = True
        confirm_message = f"sandbox workspace mount (host dir exposed read-write): {Path.cwd()}"
    elif tool_name == "sandbox_shell" and inp.get("network", "none") != "none":
        needs_confirm = True
        confirm_message = f"sandbox network access: {inp.get('network', '')}"
    elif tool_name == "sandbox_shell" and inp.get("deps", "reuse") == "install":
        needs_confirm = True
        confirm_message = "sandbox dependency install (writes shared deps volume)"
    elif tool_name == "write_file" and not Path(inp.get("file_path", "")).exists():
        needs_confirm = True
        confirm_message = f"write new file: {inp.get('file_path', '')}"
    elif tool_name == "edit_file" and not Path(inp.get("file_path", "")).exists():
        needs_confirm = True
        confirm_message = f"edit non-existent file: {inp.get('file_path', '')}"

    if needs_confirm:
        if mode == "dontAsk":
            return {"action": "deny", "message": f"Auto-denied (dontAsk mode): {confirm_message}"}
        return {"action": "confirm", "message": confirm_message}

    if tool_name == "run_shell" and shell_sandbox_mode() == "auto":
        return {"action": "allow", "runtime": classify_shell_runtime(inp.get("command", ""))}
    return {"action": "allow"}


def reset_permission_cache() -> None:
    global _cached_rules
    _cached_rules = None
