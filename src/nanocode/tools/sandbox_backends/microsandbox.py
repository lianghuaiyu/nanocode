"""MicrosandboxAdapter（microVM）：在 microsandbox microVM 内隔离执行 `SandboxPlan`（docs/19 §5）。

VM-on-demand 后端：仅当策略要求更强隔离（strict / vm profile）或 native 不可用且策略允许 VM 时，
由 SandboxManager 选中。**不是模型可见工具**——模型只请求 shell，runtime 决定后端。

与旧 `tools/sandbox_shell.py` 的根本区别（docs/19 §5 删除清单）：

- 无 `_merge_params` / `sandbox_defaults` 模块默认值。
- 无 `_session_id` / `_cwd` 从 dict 读隐藏字段——一切来自 `SandboxPlan`（runtime 注入）。
- 无 persist（第一版 ephemeral VM，docs/19 §10.5）：无 `msb list` substring 判定、无指纹复用。
- network 来自 `plan.network`（none → `--no-net`），workspace mount 来自 `plan.cwd`，不是模型 bool。
- deps/trace 不再是模型参数（attack surface 直接消除）。
- `msb` 缺失 → 结构化 error，不提示 "use run_shell"。

`msb` 二进制只在严格校验过的显式 env 或固定可信目录里解析（**完全不走 PATH**，避免 cwd 下
`./msb` 劫持 microVM 启动器在宿主跑）。
"""

from __future__ import annotations

import os
import subprocess

DEFAULT_IMAGE = "python:3.12"
DEFAULT_MEMORY_MIB = 1024
DEFAULT_CPUS = 1
WORKSPACE_MOUNT = "/workspace"

_MSB_MISSING = (
    "microsandbox ('msb') not found: VM backend unavailable on this host "
    "(install via https://install.microsandbox.dev, Apple Silicon macOS or Linux+KVM only)"
)

# msb 二进制只在这些固定可信目录里解析（不走 PATH）。env(NANOCODE_MSB_BIN/MSB_BIN) 仅在严格
# 校验通过时采用：绝对、存在可执行、basename==msb、不在 cwd 内（拒 /bin/sh 等启动器、拒 repo 内文件）。
_TRUSTED_MSB_DIRS = (
    os.path.expanduser("~/.microsandbox/bin"),
    "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
)


def _is_trusted_msb(path: str) -> bool:
    if not path or not os.path.isabs(path):
        return False
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        return False
    if os.path.basename(path) != "msb":
        return False
    rp = os.path.realpath(path)
    cwd = os.path.realpath(os.getcwd())
    if rp == cwd or rp.startswith(cwd + os.sep):
        return False
    return True


def _resolve_msb() -> str | None:
    explicit = os.environ.get("NANOCODE_MSB_BIN") or os.environ.get("MSB_BIN")
    if explicit and _is_trusted_msb(explicit):
        return explicit
    for d in _TRUSTED_MSB_DIRS:
        p = os.path.join(d, "msb")
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def is_available() -> bool:
    return _resolve_msb() is not None


def build_run_argv(plan, msb: str) -> list[str]:
    """SandboxPlan → ephemeral `msb run` argv。资源/网络/挂载全部来自 plan，绝不读模型 dict。

    受保护元数据目录（.git 等，落在 workspace 内）在 rw workspace 之上重新以 `:ro` 子挂载覆盖回只读，
    镜像 native 后端的 carve（review HIGH：否则 VM 内可写 .git/hooks/* 并持久化回宿主）。
    """
    image = plan.vm_image or DEFAULT_IMAGE
    args = [msb, "run", "-q", "--memory", f"{DEFAULT_MEMORY_MIB}M", "--cpus", str(DEFAULT_CPUS)]
    if plan.network.mode == "none":
        args += ["--no-net"]
    # workspace 挂载：plan.cwd realpath → /workspace。可写根非空 → rw，否则 ro（read-only profile）。
    workspace = os.path.realpath(str(plan.cwd))
    mount_mode = "rw" if plan.filesystem.writable_roots else "ro"
    args += ["--volume", f"{workspace}:{WORKSPACE_MOUNT}:{mount_mode}"]
    # 受保护根：仅当 workspace 可写时才需要把它们重新 ro 覆盖（ro workspace 已整体只读）。
    if mount_mode == "rw":
        for p in plan.filesystem.protected_roots:
            hp = os.path.realpath(str(p))
            rel = os.path.relpath(hp, workspace)
            if rel == os.curdir or rel.startswith(os.pardir):
                continue  # 不在 workspace 内（如 .git gitdir pointer target）→ 未挂载即不可写
            if os.path.exists(hp):
                guest = WORKSPACE_MOUNT + "/" + rel.replace(os.sep, "/")
                args += ["--volume", f"{hp}:{guest}:ro"]
    args += ["--workdir", WORKSPACE_MOUNT]
    if plan.timeout_ms:
        args += ["--timeout", f"{plan.timeout_ms // 1000}s"]
    args += [image, "--", "/bin/sh", "-c", plan.command]
    return args


def run_plan(plan) -> dict:
    """在 ephemeral microVM 内执行 plan.command；返回结构化 dict（与 native adapter 同形）。"""
    out = {"exit_code": None, "stdout": "", "stderr": "", "timed_out": False, "error": None}
    msb = _resolve_msb()
    if not msb:
        out["error"] = _MSB_MISSING
        return out
    try:
        argv = build_run_argv(plan, msb)
        timeout_s = (plan.timeout_ms / 1000) if plan.timeout_ms else None
        cp = subprocess.run(
            argv, shell=False, capture_output=True, text=True,
            timeout=(timeout_s + 15) if timeout_s else None, input=plan.stdin)
        out["exit_code"], out["stdout"], out["stderr"] = (
            cp.returncode, cp.stdout or "", cp.stderr or "")
    except FileNotFoundError:
        out["error"] = _MSB_MISSING
    except subprocess.TimeoutExpired:
        out["timed_out"] = True
    except Exception as e:
        out["error"] = str(e)
    return out
