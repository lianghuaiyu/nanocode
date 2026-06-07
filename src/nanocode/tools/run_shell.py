"""run_shell 工具：执行 shell 命令；附带危险命令的正则检测。"""

from __future__ import annotations

import asyncio
import os
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


def _no_backend_msg(context: str) -> str:
    """无原生后端 / auto 无 msb 时的 fail-closed 文案（带 escalate=true 指引）。

    前台/hook：提示重试 escalate=true 上宿主；background：提示前台或 escalate=true。
    """
    if context == "background":
        return (
            "native OS sandbox unavailable on this host; "
            "background sandbox command refused — run it in the foreground (confined) "
            "or add escalate=true to run on the host"
        )
    return (
        "native OS sandbox unavailable on this host. "
        "Retry the SAME command with escalate=true to run it on the host "
        "(you will be asked to approve)."
    )


def _auto_bg_msg() -> str:
    """auto（microVM）档后台命令的 fail-closed 文案：microVM 无法异步后台包裹。"""
    return (
        "background command is not available under the microVM (auto) sandbox — "
        "run it in the foreground or add escalate=true to run on the host"
    )


def plan_shell(inp: dict, *, context: str = "foreground") -> tuple[str, object]:
    """所有 shell 入口（前台/后台/hook）共用的唯一路由决策（纯函数，deferred import 避周期）。

    context ∈ {"foreground","background","hook"}。
    返回 (kind, info)：
      ('host', None)        宿主裸跑（off / 只读 / escalate）。
      ('sandbox', backend)  原生 OS 沙盒（caller 用 backend.run_structured 或 backend.build_argv）。
      ('microvm', None)     microVM（仅 foreground；background→blocked）。
      ('blocked', reason)   不能受限且不该裸跑 → 拒绝（fail-closed）。

    Codex 模型：confinement 是统一的 routing 决策，所有 shell 入口共用同一个 planner。
    无法把命令关进沙盒时 fail-closed，绝不裸跑宿主。
    """
    from . import permissions, sandbox_shell
    from .sandbox_backends import resolve_native_backend

    cmd = inp.get("command", "")
    if inp.get("escalate"):
        return ("host", None)  # 显式提权（已在权限层 confirm）
    if permissions.classify_shell_runtime(cmd) != "sandbox":
        return ("host", None)  # off / 只读
    # hook 一律用原生 OS 沙盒受限（宿主工具链在，不破坏 hook，又满足约束）；无原生后端则
    # blocked。hook 绝不进 microVM、绝不裸跑宿主——故置于 mode 分支之前（任何沙盒档都生效）。
    # （off 档已被上面的 classify!=sandbox→host 拦下，off 档 hook 仍宿主，正确：off=不沙盒。）
    if context == "hook":
        backend = resolve_native_backend()
        if backend is None:
            return ("blocked", _no_backend_msg("hook"))  # 无原生后端 → fail-closed
        return ("sandbox", backend)
    mode = permissions.shell_sandbox_mode()
    if mode == "seatbelt":
        backend = resolve_native_backend()
        if backend is None:
            return ("blocked", _no_backend_msg(context))  # H①：fail-closed
        return ("sandbox", backend)
    # mode == "auto"（microVM）
    if sandbox_shell._resolve_msb() is None:
        return ("blocked", _no_backend_msg(context))  # C 残留：auto+无 msb 不再静默裸跑
    if context == "background":
        return ("blocked", _auto_bg_msg())  # microVM 无法异步后台
    return ("microvm", None)  # foreground


def plan_background(inp: dict) -> tuple[str, object]:
    """后台命令的路由决策——`plan_shell(inp, context="background")` 的薄包装。

    background 永不返回 'microvm'（planner 已转 blocked）；'sandbox' 时 info 为后端模块，
    caller（run_background）自行 build_argv。保留此名兼容现有调用与测试。
    """
    return plan_shell(inp, context="background")


async def run_background(inp: dict, *, stdout_path: str, stderr_path: str) -> dict:
    """异步执行命令；stdout/stderr 流式写入文件；结束返回结构化结果。
    被取消时 terminate→grace→kill 并 re-raise。permission 不在此做。

    路由（plan_shell, context="background"）：沙盒归类 → backend.build_argv 关进 seatbelt/bwrap
    （create_subprocess_exec）；无法受限（无后端 / auto microVM）→ blocked（返回 dict 多 'blocked'
    键，不 spawn 任何子进程）；其余（off / 只读 / escalate）→ 原样裸跑宿主（create_subprocess_shell）。"""
    kind, info = plan_shell(inp, context="background")
    if kind == "blocked":
        # fail-closed：不创建子进程，回报 blocked 由 tasks/runner 落库为 status="blocked"。
        return {
            "exit_code": None,
            "timed_out": False,
            "cancelled": False,
            "error": None,
            "blocked": info,
        }
    out = {"exit_code": None, "timed_out": False, "cancelled": False, "error": None}
    timeout = inp.get("timeout")
    timeout_s = (timeout / 1000) if timeout else None
    Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
    Path(stderr_path).parent.mkdir(parents=True, exist_ok=True)
    proc = None
    try:
        with open(stdout_path, "wb") as fo, open(stderr_path, "wb") as fe:
            if kind == "sandbox":
                # info 是后端模块：build_argv 拼受限 argv（杀外层 sandbox-exec/bwrap 会带走
                # 子进程；bwrap 已 --die-with-parent）。
                argv = info.build_argv(inp["command"], posture="workspace-write", cwd=os.getcwd())
                proc = await asyncio.create_subprocess_exec(*argv, stdout=fo, stderr=fe)
            else:  # kind == "host"
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
