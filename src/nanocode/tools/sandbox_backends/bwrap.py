"""Bubblewrap（Linux `bwrap`）后端：纯函数 argv builder + 受控 runner。

Linux 默认原生层。与 seatbelt 镜像同一接口（`is_available` / 纯函数 builder /
`run_structured` / 文本 `run`），planner（run_shell.plan_shell→resolve_native_backend）
按平台二选一，签名一致。

workspace-write 映射：整盘只读（`--ro-bind / /`）+ cwd 读写（`--bind cwd cwd`）+
受保护目录重新 `--ro-bind` 覆盖回只读 + 无网络（`--unshare-net`）。read-only 只
`--ro-bind / /` + tmpfs /tmp，不挂 cwd 读写。

本模块**不接任何路由**，仅供 planner（run_shell.plan_shell）选中后调用与 skipif-linux 集成测试。

关键坑（均在此处理）：
1. realpath：subpath 必须用 realpath 解析后的真实路径（symlink cwd）。
2. fail-closed：cwd 为空/非绝对/为 "/" → raise，绝不生成放行全盘可写的 argv；
   `bwrap` 二进制只在固定可信目录里解析（`_resolve_bwrap_bin`，**完全不走 PATH**，
   镜像 seatbelt 写死 `/usr/bin/sandbox-exec` 的做法），找不到则 fail-closed。
   注意：`shutil.which` 照样走 PATH——PATH 含 `.`/cwd 时返回 `./bwrap`，劫持成立，故弃用。
3. 受保护目录：存在 → `--ro-bind` 覆盖回只读（可读、写被拒，不用 `--tmpfs` 以免遮蔽
   真实内容，导致沙盒内 `git status` 看不到 .git）；**不存在 → `--tmpfs`**（空 tmpfs，
   写入落 throwaway，不持久到宿主，挡住 `mkdir .git && echo > .git/config` 逃逸；
   tmpfs 仅用于不存在的受保护目录，不会遮蔽真实内容）。
4. 网络：`--unshare-net` 把命令丢进无网络的 net namespace，实现 network=none。
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

# bwrap 二进制只在这些固定可信目录里解析（完全不走 PATH，镜像 seatbelt 写死
# /usr/bin/sandbox-exec）。shutil.which 会走 PATH，PATH 含 cwd 时返回 ./bwrap → 劫持。
_TRUSTED_BWRAP = (
    "/usr/bin/bwrap",
    "/bin/bwrap",
    "/usr/local/bin/bwrap",
    "/opt/homebrew/bin/bwrap",
)


def _resolve_bwrap_bin() -> str | None:
    """在固定可信目录里找一个可执行的 bwrap 绝对路径（不走 PATH）；找不到 → None。"""
    for p in _TRUSTED_BWRAP:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def is_available() -> bool:
    return _resolve_bwrap_bin() is not None


def _real_abs(path: str) -> str:
    # fail-closed：空/非绝对的输入在 realpath 解析（会拼接当前 cwd）之前就拒绝，
    # 否则 "" / 相对路径会被悄悄解析成 os.getcwd()，放行非预期目录。
    if not path or not os.path.isabs(path):
        raise ValueError(f"unsafe sandbox workspace path: {path!r}")
    rp = os.path.realpath(path)
    if not rp or not os.path.isabs(rp) or rp == "/":
        raise ValueError(f"unsafe sandbox workspace path: {path!r} -> {rp!r}")
    return rp


def build_bwrap_argv(
    posture: str,
    cwd: str,
    *,
    protected_roots: tuple[str, ...] = DEFAULT_PROTECTED_ROOTS,
    allow_network: bool = False,
    command: str = "",
    bwrap_bin: str = "bwrap",
) -> list[str]:
    """纯函数：根据姿态生成 bwrap argv（最终命令的解释器固定为 /bin/sh -c）。

    danger-full-access 不生成 argv（宿主直跑）；read-only / workspace-write 生成。
    `bwrap_bin` 为 argv[0]：runner 解析为 `_resolve_bwrap_bin()` 的可信绝对路径后传入，
    避免 exec 走 PATH 被 cwd 下的 `./bwrap` 劫持（镜像 seatbelt 写死绝对路径的做法）。
    """
    if posture == DANGER_FULL_ACCESS:
        raise ValueError(
            "danger-full-access does not use a bwrap sandbox (run on host)"
        )
    if posture not in (READ_ONLY, WORKSPACE_WRITE):
        raise ValueError(f"unknown posture: {posture}")
    cwd_real = _real_abs(cwd)
    argv = [
        bwrap_bin,
        "--ro-bind", "/", "/",  # 整盘只读可读
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",  # 全新可写 /tmp
    ]
    if posture == WORKSPACE_WRITE:
        argv += ["--bind", cwd_real, cwd_real]  # cwd 读写
        for pr in protected_roots:  # 受保护目录覆盖回不可写
            p = os.path.join(cwd_real, pr)
            if os.path.exists(p):
                argv += ["--ro-bind", p, p]  # 存在：ro-bind（可读、写被拒）
            else:
                argv += ["--tmpfs", p]  # 不存在：空 tmpfs（写入落 throwaway，不持久到宿主）
    # read-only：不加 --bind cwd（仅 --ro-bind / + tmpfs /tmp）
    if not allow_network:
        argv += ["--unshare-net"]  # 无网络命名空间
    argv += [
        "--unshare-pid",
        "--die-with-parent",
        "--chdir", cwd_real,
        "--setenv", "TMPDIR", "/tmp",
        "/bin/sh", "-c", command,
    ]
    return argv


def build_argv(
    command: str, *, posture: str = WORKSPACE_WRITE, cwd: str | None = None
) -> list[str]:
    """纯函数：拼出 bwrap 受限执行的 argv（前台/后台复用）。

    `cwd` 缺省取 os.getcwd()；`bwrap` 二进制用 `_resolve_bwrap_bin()` 在固定可信目录里解析的
    绝对路径（完全不走 PATH），找不到则 fail-closed（raise FileNotFoundError），绝不裸跑宿主。
    """
    workdir = _real_abs(cwd or os.getcwd())
    bin_path = _resolve_bwrap_bin()
    if not bin_path:
        raise FileNotFoundError("bwrap not found in trusted paths")
    return build_bwrap_argv(posture, workdir, command=command, bwrap_bin=bin_path)


def run_structured(
    inp: dict, *, posture: str = WORKSPACE_WRITE, cwd: str | None = None
) -> dict:
    """在 bwrap 沙盒内执行 inp['command']；返回与 run_shell.run_structured 同形 dict
    （exit_code/stdout/stderr/timed_out/error）。danger-full-access 不应走这里。"""
    out = {"exit_code": None, "stdout": "", "stderr": "", "timed_out": False, "error": None}
    try:
        workdir = _real_abs(cwd or os.getcwd())
        try:
            argv = build_argv(inp["command"], posture=posture, cwd=workdir)
        except FileNotFoundError:
            # fail-closed：找不到 bwrap 绝不裸跑宿主，返回机制失败由路由层提示 escalate。
            out["error"] = "bwrap not found in trusted paths"
            return out
        timeout_s = inp.get("timeout", 30000) / 1000
        r = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            input=inp.get("stdin"),
            cwd=workdir,
        )
        out["exit_code"], out["stdout"], out["stderr"] = (
            r.returncode,
            r.stdout or "",
            r.stderr or "",
        )
    except subprocess.TimeoutExpired:
        out["timed_out"] = True
    except Exception as e:
        out["error"] = str(e)
    return out


def run(inp: dict, *, posture: str = WORKSPACE_WRITE, cwd: str | None = None) -> str:
    """文本输出包装：调 run_structured 后按与 run_shell.run 完全一致的格式返回文本。"""
    r = run_structured(inp, posture=posture, cwd=cwd)
    if r["timed_out"]:
        return f"Command timed out after {inp.get('timeout', 30000)}ms"
    if r["error"] is not None:
        return f"Error: {r['error']}"
    if r["exit_code"] != 0:
        stderr = f"\nStderr: {r['stderr']}" if r["stderr"] else ""
        stdout = f"\nStdout: {r['stdout']}" if r["stdout"] else ""
        return f"Command failed (exit code {r['exit_code']}){stdout}{stderr}"
    return r["stdout"] or "(no output)"
