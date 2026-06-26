"""run_shell 工具：public schema + 危险命令正则检测。

执行**不在此**——run_shell 由 engine 经唯一规划点 `SandboxManager`（native-first / VM-on-demand）
执行（docs/19）。本模块只提供 (a) 发给模型的 schema，(b) PermissionEngine / hook 用的危险命令检测。
"""

from __future__ import annotations

import re

SCHEMA = {
    "name": "run_shell",
    "description": "Execute a shell command and return its output. Use this for running tests, installing packages, git operations, etc.",
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to execute"},
            "timeout": {"type": "number", "description": "Timeout in milliseconds (default: 30000)"},
            "run_in_background": {"type": "boolean", "description": "Run the command as a detached background task instead of blocking."},
            "escalate": {
                "type": "boolean",
                "description": (
                    "Retry-only sandbox escalation. Set true ONLY to re-run a command that "
                    "FAILED inside the sandbox because it needs network access, host "
                    "tools (e.g. git, node), or host filesystem access. This runs the command on "
                    "the HOST and requires user approval. Never set this on a first attempt."
                ),
            },
        },
        "required": ["command"],
    },
}


# ─── Dangerous command patterns ─────────────────────────────

DANGEROUS_PATTERNS = [
    re.compile(r"\brm(?=[\s$;&|]|$)"),
    re.compile(r"\bgit\s+(push|reset|clean|checkout\s+\.)"),
    re.compile(r"\bsudo\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd(?=[\s$;&|]|$)"),
    re.compile(r"\$\{?IFS"),
    re.compile(r"\|\s*(ba|z|k|c)?sh\b"),
    re.compile(r">\s*/dev/"),
    re.compile(r"\bkill\b"),
    re.compile(r"\bpkill\b"),
    re.compile(r"\breboot\b"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\bdel\s", re.IGNORECASE),
    re.compile(r"\brmdir\s", re.IGNORECASE),
    re.compile(r"\bformat\s", re.IGNORECASE),
    re.compile(r"\btaskkill\s", re.IGNORECASE),
    re.compile(r"\bRemove-Item\s", re.IGNORECASE),
    re.compile(r"\bStop-Process\s", re.IGNORECASE),
]


def is_dangerous(command: str) -> bool:
    return any(p.search(command) for p in DANGEROUS_PATTERNS)


# ─── host-routed executor（docs/24 Phase 3）────────────────────────
#
# run_shell 经 curated ToolContext 能力把手自包含执行：
# - run_in_background → ctx.tasks.spawn_shell（后台任务面板，与今天 router 后台分支等价）；
# - 前台 → ctx.exec.run（唯一规划点 SandboxManager，与今天 engine._run_real_tool 前台特例等价）。
# cwd/session 来自 HostContext（把手内取），非 tool input（模型无法 spoof）。


async def run(ctx, inp: dict) -> str:
    if inp.get("run_in_background"):
        tid = await ctx.tasks.spawn_shell(inp.get("command", ""), inp.get("timeout"))
        return (f"Started background shell task {tid}. It will report completion later. "
                f"Use task_output with task_id={tid} to inspect progress.")
    from ..capabilities.sandbox import ShellRequest
    request = ShellRequest.from_tool_input(inp)
    return await ctx.exec.run(request)
