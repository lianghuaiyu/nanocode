"""run_shell 工具：执行 shell 命令；附带危险命令的正则检测。"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

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
                    "FAILED inside the isolated sandbox because it needs network access, host "
                    "tools (e.g. git, node), or host filesystem access. This runs the command on "
                    "the HOST and requires user approval. Never set this on a first attempt."
                ),
            },
        },
        "required": ["command"],
    },
}


def run_structured(inp: dict) -> dict:
    """执行命令并返回结构化结果（exit code/stdout/stderr/超时/异常）；支持 stdin 与 timeout(ms)。"""
    out = {"exit_code": None, "stdout": "", "stderr": "", "timed_out": False, "error": None}
    try:
        timeout_s = inp.get("timeout", 30000) / 1000
        result = subprocess.run(
            inp["command"],
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            input=inp.get("stdin"),
        )
        out["exit_code"] = result.returncode
        out["stdout"] = result.stdout or ""
        out["stderr"] = result.stderr or ""
    except subprocess.TimeoutExpired:
        out["timed_out"] = True
    except Exception as e:
        out["error"] = str(e)
    return out


def run(inp: dict) -> str:
    """文本输出包装：基于 run_structured 格式化（字节级等价于旧实现）。"""
    r = run_structured(inp)
    if r["timed_out"]:
        return f"Command timed out after {inp.get('timeout', 30000)}ms"
    if r["error"] is not None:
        return f"Error: {r['error']}"
    if r["exit_code"] != 0:
        stderr = f"\nStderr: {r['stderr']}" if r["stderr"] else ""
        stdout = f"\nStdout: {r['stdout']}" if r["stdout"] else ""
        return f"Command failed (exit code {r['exit_code']}){stdout}{stderr}"
    return r["stdout"] or "(no output)"


async def run_background(inp: dict, *, stdout_path: str, stderr_path: str) -> dict:
    """异步执行命令；stdout/stderr 流式写入文件；结束返回结构化结果。
    被取消时 terminate→grace→kill 并 re-raise。permission 不在此做。"""
    out = {"exit_code": None, "timed_out": False, "cancelled": False, "error": None}
    timeout = inp.get("timeout")
    timeout_s = (timeout / 1000) if timeout else None
    Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
    Path(stderr_path).parent.mkdir(parents=True, exist_ok=True)
    proc = None
    try:
        with open(stdout_path, "wb") as fo, open(stderr_path, "wb") as fe:
            proc = await asyncio.create_subprocess_shell(inp["command"], stdout=fo, stderr=fe)
            try:
                if timeout_s is not None:
                    await asyncio.wait_for(proc.wait(), timeout=timeout_s)
                else:
                    await proc.wait()
                out["exit_code"] = proc.returncode
            except asyncio.TimeoutError:
                out["timed_out"] = True
                await _terminate_then_kill(proc)
    except asyncio.CancelledError:
        out["cancelled"] = True
        if proc is not None:
            await _terminate_then_kill(proc)
        raise
    except Exception as e:
        out["error"] = str(e)
    return out


async def _terminate_then_kill(proc, grace_s: float = 3.0) -> None:
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_s)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


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
