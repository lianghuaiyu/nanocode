# src/nanocode/tools/sandbox_shell.py
"""sandbox_shell 工具：在 microsandbox microVM 内隔离执行 shell 命令。
run_shell 的 opt-in 旁路。所有 msb CLI 行为基于真机实测（msb 0.5.x）。"""

from __future__ import annotations

import itertools
import os
import subprocess
from pathlib import Path

from . import sandbox_defaults

DEFAULT_IMAGE = "python:3.12"
DEFAULT_MEMORY_MIB = 1024
DEFAULT_CPUS = 1
DEFAULT_TIMEOUT_MS = 120000
DEPS_VOLUME = "nanocode-deps"
DEPS_MOUNT = "/deps"
WORKSPACE_MOUNT = "/workspace"
TRACE_MOUNT = "/trace"
_eph_counter = itertools.count(1)
EC_SENTINEL = "__NCEC__="

NETWORK_VALUES = {"none", "public"}
DEPS_VALUES = {"none", "reuse", "install"}

SCHEMA = {
    "name": "sandbox_shell",
    "description": (
        "Execute a shell command inside an isolated microsandbox microVM (hardware-level "
        "isolation, separate Linux kernel). Prefer this over run_shell for untrusted commands, "
        "package installs, or tests that should not touch the host. "
        "Defaults: ephemeral VM, network disabled, host project NOT mounted, shared dependency "
        "volume mounted read-only for reuse. "
        "DEPENDENCY REUSE: to make a pip package persist across calls, run ONE call with "
        "deps='install' AND network='public' and just `pip install <pkg>` (PIP_TARGET is set "
        "automatically, so no --target needed); afterwards any call with deps='reuse' (the "
        "default) can `import <pkg>` even offline. A plain `pip install` with the default "
        "deps='reuse' does NOT persist (read-only) — you must use deps='install'. "
        "Requires the 'msb' CLI and a supported platform (Apple Silicon macOS or Linux+KVM); "
        "if unavailable, fall back to run_shell."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run inside the sandbox (supports &&, pipes, cd)"},
            "image": {"type": "string", "description": f"OCI image (default: {DEFAULT_IMAGE})"},
            "persist": {"type": "boolean", "description": "Reuse a long-lived sandbox across calls (default: session/false)"},
            "network": {"type": "string", "enum": sorted(NETWORK_VALUES), "description": "none=offline (default), public=internet allowed"},
            "mount_workspace": {"type": "boolean", "description": "Mount current dir read-write at /workspace (default: session/false)"},
            "deps": {"type": "string", "enum": sorted(DEPS_VALUES), "description": "reuse=read-only shared deps, importable offline (default); install=writable, set with network='public' to `pip install <pkg>` into the shared volume for later reuse; none=no shared deps"},
            "memory_mib": {"type": "number", "description": f"Memory in MiB (default: {DEFAULT_MEMORY_MIB})"},
            "cpus": {"type": "number", "description": f"vCPUs (default: {DEFAULT_CPUS})"},
            "timeout_ms": {"type": "number", "description": f"Timeout in ms (default: {DEFAULT_TIMEOUT_MS})"},
            "trace": {"type": "boolean", "description": "Collect the in-sandbox agent's trace: bind-mount a host trace dir at /trace and inject NANOCODE_TRACE* env so a nanocode (or any file-writing) agent inside writes its trajectory back to the host, linked as a child of this session's trace (default: session/false)"},
        },
        "required": ["command"],
    },
}


# ─── 参数校验 ───
def _positive_int(value, default: int, field: str) -> int:
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer") from None
    if n <= 0:
        raise ValueError(f"{field} must be greater than 0")
    return n


def _network(value) -> str:
    s = str(value).strip().lower()
    if s not in NETWORK_VALUES:
        raise ValueError(f"network must be one of: {', '.join(sorted(NETWORK_VALUES))}")
    return s


def _deps(value) -> str:
    s = str(value).strip().lower()
    if s not in DEPS_VALUES:
        raise ValueError(f"deps must be one of: {', '.join(sorted(DEPS_VALUES))}")
    return s


def _bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"true", "on", "yes", "1"}:
        return True
    if s in {"false", "off", "no", "0"}:
        return False
    raise ValueError("boolean value expected")


# ─── msb 定位 ───
# msb 二进制只在**严格校验过**的显式 env 或固定可信目录里解析，**完全不走 PATH**
# （镜像 bwrap _resolve_bwrap_bin）。shutil.which 会走 PATH，PATH 含 cwd 时返回 ./msb →
# 劫持 microVM 启动器在宿主跑（与 bwrap 的 A 同类），故弃用。
#
# 启动器劫持收口（round-4）：env(NANOCODE_MSB_BIN/MSB_BIN) 此前接受任意绝对路径，且 repo
# `.env` 在 workspace-trust **之前**加载（cli.py），故恶意 repo `.env` 可设
# `NANOCODE_MSB_BIN=/bin/sh` + 放文件 `run` → `auto` 前台一条沙盒命令拼成
# `[/bin/sh, "run", …]` → 在宿主跑 repo 脚本，不受限（真实 RCE）。
# 修复：env 路径必须通过 _is_trusted_msb——绝对、存在、可执行、**basename 为 msb**
# （拒 /bin/sh 等非 msb 启动器）、且**不在 cwd/workspace 内**（拒指向 repo 文件）；
# 否则忽略 env、落回 _TRUSTED_MSB_DIRS 解析真 msb。
_TRUSTED_MSB_DIRS = (
    os.path.expanduser("~/.microsandbox/bin"),
    "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
)


def _is_trusted_msb(path: str) -> bool:
    """env 提供的 msb 路径是否可信：绝对、存在且可执行、basename==msb、不在 cwd 内。"""
    if not path or not os.path.isabs(path):
        return False
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        return False
    if os.path.basename(path) != "msb":           # 拒非 msb 启动器（/bin/sh 等）
        return False
    rp = os.path.realpath(path)
    cwd = os.path.realpath(os.getcwd())
    if rp == cwd or rp.startswith(cwd + os.sep):   # 拒指向 repo 内文件
        return False
    return True


def _resolve_msb() -> str | None:
    # 显式 env：仅在严格校验通过时采用（拒相对/不存在/不可执行/非 msb/cwd 内）。
    explicit = os.environ.get("NANOCODE_MSB_BIN") or os.environ.get("MSB_BIN")
    if explicit and _is_trusted_msb(explicit):
        return explicit
    # 已知安装位置 + 可信系统目录（不走 PATH，避免 cwd 注入）。
    for d in _TRUSTED_MSB_DIRS:
        p = os.path.join(d, "msb")
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


# ─── session_id 解析：显式 inp["_session_id"]（调用方注入；env 回退已删，docs/16 C-2 随 #3）───
def _session_id_of(p: dict) -> str:
    """取显式注入的 _session_id（router/后台 runner/hook 路径都显式注入；缺 → "default"）。"""
    return p.get("_session_id") or "default"


def _sandbox_name_for(p: dict) -> str:
    return f"nanocode-sbx-{_session_id_of(p)}"


def _trace_dir() -> Path:
    """宿主侧沙箱轨迹根目录（NANOCODE_TRACE_DIR 覆盖，否则 ./.nanocode/traces/）。

    sandbox 的 `/trace` bind-mount 让 in-sandbox 的嵌套 agent 把自己的轨迹写回宿主——这是独立于
    runtime wire/Tracer（已退役）的沙箱产物落点。本地内联，避免依赖已删的 trace 包。"""
    override = os.environ.get("NANOCODE_TRACE_DIR", "").strip()
    d = Path(override) if override else (Path.cwd() / ".nanocode" / "traces")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _trace_host_dir_for(p: dict, trace_tag: str) -> str:
    """宿主侧沙箱子轨迹目录的绝对路径（不创建，仅计算）。session_id 来自 p。"""
    return str(_trace_dir() / _session_id_of(p) / "sandbox" / trace_tag)


# ─── 参数合并：显式 > 会话默认 > 内置默认 ───
def _merge_params(inp: dict) -> dict:
    defaults = sandbox_defaults.get_defaults()

    def pick(key, builtin):
        if key in inp and inp[key] is not None:
            return inp[key]
        if key in defaults:
            return defaults[key]
        return builtin

    result = {
        "command": inp.get("command"),
        "image": inp.get("image") or DEFAULT_IMAGE,
        "persist": _bool(pick("persist", False), False),
        "network": _network(pick("network", "none")),
        "mount_workspace": _bool(pick("mount_workspace", False), False),
        "deps": _deps(pick("deps", "reuse")),
        "memory_mib": _positive_int(inp.get("memory_mib"), DEFAULT_MEMORY_MIB, "memory_mib"),
        "cpus": _positive_int(inp.get("cpus"), DEFAULT_CPUS, "cpus"),
        "timeout_ms": _positive_int(inp.get("timeout_ms"), DEFAULT_TIMEOUT_MS, "timeout_ms"),
        "trace": _bool(pick("trace", False), False),
        "trace_tag": None,  # 下面按 trace+persist 计算
        "_session_id": inp.get("_session_id"),  # 透传：显式 session_id 优先于 env
        "_cwd": inp.get("_cwd"),
    }
    if result["trace"]:
        sid = _session_id_of(result)
        if result["persist"]:
            result["trace_tag"] = f"nanocode-sbx-{sid}"
        else:
            result["trace_tag"] = f"eph-{next(_eph_counter)}"
    return result


def _validate_command(p: dict) -> None:
    if not isinstance(p["command"], str) or not p["command"].strip():
        raise ValueError("command is required")


def _common_resource_flags(p: dict) -> list[str]:
    """内存/cpu/网络/卷/依赖 —— run 与 create 共用。"""
    args = ["--memory", f"{p['memory_mib']}M", "--cpus", str(p["cpus"])]
    if p["network"] == "none":
        args += ["--no-net"]
    if p["mount_workspace"]:
        workspace = str(Path(p.get("_cwd") or Path.cwd()).resolve())
        args += ["--volume", f"{workspace}:{WORKSPACE_MOUNT}:rw"]
    if p["deps"] == "reuse":
        args += ["--volume", f"{DEPS_VOLUME}:{DEPS_MOUNT}:ro", "-e", f"PYTHONPATH={DEPS_MOUNT}"]
    elif p["deps"] == "install":
        args += ["--volume", f"{DEPS_VOLUME}:{DEPS_MOUNT}:rw",
                 "-e", f"PYTHONPATH={DEPS_MOUNT}", "-e", f"PIP_TARGET={DEPS_MOUNT}"]
    if p.get("trace") and p.get("trace_tag"):
        host_trace = _trace_host_dir_for(p, p["trace_tag"])
        parent = _session_id_of(p)
        args += ["--volume", f"{host_trace}:{TRACE_MOUNT}:rw",
                 "-e", "NANOCODE_TRACE=1",
                 "-e", f"NANOCODE_TRACE_DIR={TRACE_MOUNT}",
                 "-e", f"NANOCODE_TRACE_PARENT={parent}"]
    return args


def build_run_command(p: dict, msb: str) -> list[str]:
    args = [msb, "run", "-q"]
    args += _common_resource_flags(p)
    if p["mount_workspace"]:
        args += ["--workdir", WORKSPACE_MOUNT]
    args += ["--timeout", f"{p['timeout_ms'] // 1000}s"]
    args += [p["image"], "--", "/bin/sh", "-c", p["command"]]
    return args


def build_create_command(p: dict, msb: str, sandbox_name: str) -> list[str]:
    """持久沙箱的 create：资源/网络/卷在此固定（实测 exec 不能改这些）。"""
    args = [msb, "create", "--name", sandbox_name]
    args += _common_resource_flags(p)
    args += [p["image"]]
    return args


def _wrap_with_sentinel(command: str) -> str:
    # 内层 sh -c 跑用户命令，外层捕获其退出码并打印哨兵（实测 exec 不透传退出码）
    inner = command.replace("'", "'\"'\"'")
    return f"sh -c '{inner}'; echo \"{EC_SENTINEL}$?\""


def build_exec_command(p: dict, msb: str, sandbox_name: str) -> list[str]:
    args = [msb, "exec"]
    if p["mount_workspace"]:
        args += ["--workdir", WORKSPACE_MOUNT]
    args += ["--timeout", f"{p['timeout_ms'] // 1000}s"]
    args += [sandbox_name, "--", "/bin/sh", "-c", _wrap_with_sentinel(p["command"])]
    return args


def build_msb_command(inp: dict, msb: str | None = None) -> list[str]:
    """纯函数：dict → argv。msb 缺省时用占位符 'msb'，便于脱离环境单测。"""
    msb = msb or _resolve_msb() or "msb"
    p = _merge_params(inp)
    _validate_command(p)
    if p["persist"]:
        return build_exec_command(p, msb, _sandbox_name_for(p))
    return build_run_command(p, msb)


_MSB_MISSING_MSG = (
    "Error: Microsandbox ('msb') not found. Sandbox execution unavailable on this host; "
    "use run_shell instead. To enable: install via 'curl -fsSL https://install.microsandbox.dev | sh' "
    "(Apple Silicon macOS or Linux+KVM only), or set NANOCODE_MSB_BIN to the msb path."
)


def _text(v) -> str:
    if v is None:
        return ""
    return v.decode(errors="replace") if isinstance(v, bytes) else v


def _format_completed(cp, exit_code: int, stdout: str, stderr: str) -> str:
    if exit_code != 0:
        parts = [f"Command failed (exit code {exit_code})"]
        if stdout:
            parts.append(f"Stdout:\n{stdout}")
        if stderr:
            parts.append(f"Stderr:\n{stderr}")
        return "\n".join(parts)
    if stdout and stderr:
        return f"{stdout}\nStderr:\n{stderr}"
    return stdout or stderr or "(no output)"


def _parse_sentinel(stdout: str):
    """从 exec 输出中解析 __NCEC__=N 哨兵，返回 (exit_code, cleaned_stdout)。"""
    lines = stdout.splitlines()
    code = 0
    kept = []
    for ln in lines:
        if ln.startswith(EC_SENTINEL):
            try:
                code = int(ln[len(EC_SENTINEL):].strip())
            except ValueError:
                code = 0
        else:
            kept.append(ln)
    cleaned = "\n".join(kept)
    if stdout.endswith("\n") and cleaned:
        cleaned += "\n"
    return code, cleaned


def _persist_fingerprint(p: dict) -> str:
    """决定 persist 沙箱身份的配置（命令除外）。变了就需重建。"""
    return "|".join([
        p["image"], str(p["memory_mib"]), str(p["cpus"]),
        p["network"], str(p["mount_workspace"]), p["deps"],
    ])


# 记录每个 persist 沙箱当前的配置指纹（进程内）
_persist_fingerprints: dict = {}


def _ensure_persist_sandbox(sandbox_name: str, p: dict, msb: str):
    """确保 persist 沙箱存在且配置匹配。配置变更则 stop+rm 重建（实测 exec 不能改网络/卷）。
    返回 (err, notice)：err 非 None 表示失败；notice 非 None 表示发生了重建（状态已丢，需提示用户）。"""
    fp = _persist_fingerprint(p)
    check = subprocess.run([msb, "list"], capture_output=True, text=True, timeout=30)
    exists = sandbox_name in (check.stdout or "")

    if exists and _persist_fingerprints.get(sandbox_name) == fp:
        return None, None  # 已存在且配置一致，直接复用

    rebuilt = False
    if exists:
        # 配置变了：拆掉重建
        subprocess.run([msb, "stop", sandbox_name], capture_output=True, text=True, timeout=60)
        subprocess.run([msb, "rm", sandbox_name], capture_output=True, text=True, timeout=60)
        rebuilt = True

    created = subprocess.run(build_create_command(p, msb, sandbox_name),
                             capture_output=True, text=True, timeout=300)
    if created.returncode != 0:
        return f"Error: failed to create sandbox: {_text(created.stderr) or _text(created.stdout)}", None
    _persist_fingerprints[sandbox_name] = fp
    notice = ("⚠ persist sandbox rebuilt (image/memory/cpus/network/mount/deps changed); "
              "prior in-sandbox state was discarded") if rebuilt else None
    return None, notice


def cleanup_persist_sandbox(session_id: str | None = None) -> None:
    """会话结束时清理本会话的 persist 沙箱（共享依赖卷不动）。由 cli.py 调用。"""
    msb = _resolve_msb()
    if not msb:
        return
    sid = session_id or os.environ.get("NANOCODE_SESSION_ID", "default")
    name = f"nanocode-sbx-{sid}"
    try:
        subprocess.run([msb, "stop", name], capture_output=True, text=True, timeout=60)
        subprocess.run([msb, "rm", name], capture_output=True, text=True, timeout=60)
    except Exception:
        pass
    _persist_fingerprints.pop(name, None)


def run(inp: dict) -> str:
    msb = _resolve_msb()
    if not msb:
        return _MSB_MISSING_MSG
    try:
        p = _merge_params(inp)
        _validate_command(p)
        timeout_s = p["timeout_ms"] / 1000
        if p["deps"] in ("reuse", "install"):
            subprocess.run([msb, "volume", "create", DEPS_VOLUME],
                           capture_output=True, text=True, timeout=60)  # 幂等，已存在则忽略；确保挂载点存在
        if p.get("trace") and p.get("trace_tag"):
            os.makedirs(_trace_host_dir_for(p, p["trace_tag"]), exist_ok=True)
        rebuild_notice = None
        if p["persist"]:
            name = _sandbox_name_for(p)
            err, rebuild_notice = _ensure_persist_sandbox(name, p, msb)
            if err:
                return err
            argv = build_exec_command(p, msb, name)
        else:
            argv = build_run_command(p, msb)
        cp = subprocess.run(argv, shell=False, capture_output=True, text=True,
                            timeout=timeout_s + 15)  # 宿主兜底略大于沙箱 timeout
        stdout, stderr = _text(cp.stdout), _text(cp.stderr)
        if p["persist"]:
            code, stdout = _parse_sentinel(stdout)
        else:
            code = cp.returncode
        result = _format_completed(cp, code, stdout, stderr)
        if rebuild_notice:
            result = f"{rebuild_notice}\n{result}"
        if p.get("trace") and p.get("trace_tag"):
            host_trace = _trace_host_dir_for(p, p["trace_tag"])
            parent = _session_id_of(p)
            result = f"[sandbox-trace] dir={host_trace} parent={parent}\n{result}"
        return result
    except FileNotFoundError:
        return _MSB_MISSING_MSG
    except subprocess.TimeoutExpired as e:
        extra = ""
        if getattr(e, "stdout", None):
            extra = f"\nStdout:\n{_text(e.stdout)}"
        return f"Command timed out after {inp.get('timeout_ms', DEFAULT_TIMEOUT_MS)}ms{extra}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"
