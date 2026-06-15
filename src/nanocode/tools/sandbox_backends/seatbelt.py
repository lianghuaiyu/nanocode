"""Seatbelt（macOS sandbox-exec）后端：纯函数 profile builder + 受控 runner。

实跑验证（macOS 26.5 / arm64）：读全盘、写 workspace 内（含 .git/嵌套/原子 rename）、
写 workspace 外被拒、联网被拒、宿主 git/python3 可跑。本模块**不接任何路由**，仅供
PR-2 调用与本 PR 的 skipif-darwin 冒烟测试。

关键坑（均在此处理）：
1. realpath：/tmp→/private/tmp，subpath 必须是 realpath 解析后的真实路径。
2. TMPDIR：cwd 之外把系统临时目录也加入 writable roots（cwd + $TMPDIR + /tmp）。
3. SBPL 转义：路径插进 (subpath "...") 前转义反斜杠与双引号。
4. fail-closed：cwd 为空/非绝对/为 "/" → raise，绝不生成放行全盘的 profile。
5. 网络：省略 (allow network*) 即被 (deny default) 拒；Seatbelt 无法做网络 allowlist。
"""

from __future__ import annotations

import os
import subprocess

from .base import (
    DANGER_FULL_ACCESS,
    DEFAULT_PROTECTED_ROOTS,
    READ_ONLY,
    WORKSPACE_WRITE,
)

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

_BASE = """\
(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow sysctl-read)
(allow mach-lookup)
(allow file-read*)
(allow file-write-data (require-all (path "/dev/null") (vnode-type CHARACTER-DEVICE)))"""


def is_available() -> bool:
    return os.path.exists(SANDBOX_EXEC)


def _sbpl_string(s: str) -> str:
    # SBPL 双引号字符串字面量：转义反斜杠与双引号
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _real_abs(path: str) -> str:
    # fail-closed：空/非绝对的输入在 realpath 解析（会拼接当前 cwd）之前就拒绝，
    # 否则 "" / 相对路径会被悄悄解析成 os.getcwd()，放行非预期目录。
    if not path or not os.path.isabs(path):
        raise ValueError(f"unsafe sandbox workspace path: {path!r}")
    rp = os.path.realpath(path)
    if not rp or not os.path.isabs(rp) or rp == "/":
        raise ValueError(f"unsafe sandbox workspace path: {path!r} -> {rp!r}")
    return rp


def _writable_roots(cwd_real: str) -> list[str]:
    # 照 Codex：cwd + $TMPDIR + /tmp（取存在的目录，realpath 去重）
    roots = [cwd_real]
    for cand in (os.environ.get("TMPDIR"), "/tmp"):
        if cand and os.path.isdir(cand):
            r = os.path.realpath(cand)
            if r not in roots:
                roots.append(r)
    return roots


def build_seatbelt_profile(
    posture: str,
    cwd: str,
    *,
    writable_roots: tuple[str, ...] | None = None,
    protected_roots: tuple[str, ...] = DEFAULT_PROTECTED_ROOTS,
    allow_network: bool = False,
) -> str:
    """纯函数：根据姿态生成 SBPL profile 文本。

    danger-full-access 不生成 profile（宿主直跑）；read-only / workspace-write 生成。
    """
    if posture == DANGER_FULL_ACCESS:
        raise ValueError(
            "danger-full-access does not use a seatbelt profile (run on host)"
        )
    if posture not in (READ_ONLY, WORKSPACE_WRITE):
        raise ValueError(f"unknown posture: {posture}")

    lines = [_BASE]
    if posture == WORKSPACE_WRITE:
        cwd_real = _real_abs(cwd)
        roots = (
            list(writable_roots)
            if writable_roots is not None
            else _writable_roots(cwd_real)
        )
        roots = [_real_abs(r) for r in roots]
        # 跨 root 交叉 carve：先算出所有 root 下所有 protected 绝对路径，再为每个 root 的
        # allow 把「落在该 root 下（== 或子路径）」的所有 protected 路径都 carve 掉。
        # 否则 cwd 在 /tmp 下时，宽 root /private/tmp 的 allow 会放行 cwd/.git。
        all_protected = [
            os.path.join(root, pr) for root in roots for pr in protected_roots
        ]
        for root in roots:
            carves = []
            for p in all_protected:
                if p == root or p.startswith(root + os.sep):
                    carves.append(f"(require-not (subpath {_sbpl_string(p)}))")
                    # 防 mkdir 首次创建绕过（Codex 注释）：subpath 不覆盖目录本身这个 literal
                    carves.append(f"(require-not (literal {_sbpl_string(p)}))")
            if carves:
                inner = "\n    ".join(
                    [f"(subpath {_sbpl_string(root)})"] + carves
                )
                lines.append(f"(allow file-write*\n  (require-all\n    {inner}))")
            else:
                lines.append(f"(allow file-write* (subpath {_sbpl_string(root)}))")
    # read-only：base 已是只读（仅 /dev/null 可写）
    if allow_network:
        lines.append("(allow network-outbound)")
        lines.append("(allow network-inbound)")
    # 否则网络被 (deny default) 拒
    return "\n".join(lines) + "\n"


# ─── SandboxPlan 消费（docs/19 §4/§5：adapter 只吃 plan，不吃 raw dict）─────────
#
# 经 duck-typing 读 plan 属性（不 import capabilities.sandbox 的类型，避免 L0→L1 import 环）。
# NetworkMode 是 str-Enum，与字符串字面量比较成立（mode == "full"）。

def _plan_posture(plan):
    writable = tuple(str(p) for p in plan.filesystem.writable_roots)
    protected = tuple(str(p) for p in plan.filesystem.protected_roots)
    allow_net = (plan.network.mode == "full")
    posture = WORKSPACE_WRITE if writable else READ_ONLY
    return posture, writable, protected, allow_net


def build_argv_from_plan(plan) -> list[str]:
    """SandboxPlan → seatbelt 受限 argv。writable/protected/network 全部来自 plan。"""
    cwd = _real_abs(str(plan.cwd))
    posture, writable, protected, allow_net = _plan_posture(plan)
    if posture == WORKSPACE_WRITE:
        profile = build_seatbelt_profile(
            WORKSPACE_WRITE, cwd, writable_roots=writable,
            protected_roots=protected, allow_network=allow_net)
    else:
        profile = build_seatbelt_profile(READ_ONLY, cwd, allow_network=allow_net)
    return [SANDBOX_EXEC, "-p", profile, "/bin/sh", "-c", plan.command]


def run_structured_plan(plan) -> dict:
    """在 seatbelt 沙盒内执行 plan.command；返回结构化 dict（exit_code/stdout/stderr/timed_out/error）。"""
    out = {"exit_code": None, "stdout": "", "stderr": "", "timed_out": False, "error": None}
    try:
        workdir = _real_abs(str(plan.cwd))
        argv = build_argv_from_plan(plan)
        env = dict(os.environ)
        env["TMPDIR"] = os.environ.get("TMPDIR") or "/tmp"
        r = subprocess.run(
            argv, shell=False, capture_output=True, text=True,
            timeout=plan.timeout_ms / 1000, input=plan.stdin, cwd=workdir, env=env)
        out["exit_code"], out["stdout"], out["stderr"] = (
            r.returncode, r.stdout or "", r.stderr or "")
    except subprocess.TimeoutExpired:
        out["timed_out"] = True
    except Exception as e:
        out["error"] = str(e)
    return out
