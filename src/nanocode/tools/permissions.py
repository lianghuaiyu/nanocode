"""权限判定：5 种权限模式 + .nanocode/settings.json 声明式 allow/deny 规则。"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

from .sandbox_backends.base import DEFAULT_PROTECTED_ROOTS
from ..paths import data_dir, project_config_dir

# ─── Permission modes ──────────────────────────────────────

PermissionMode = str  # "default" | "plan" | "acceptEdits" | "bypassPermissions" | "dontAsk"

READ_TOOLS = {"read_file", "list_files", "grep_search", "web_fetch"}
EDIT_TOOLS = {"write_file", "edit_file"}

# Concurrency-safe tools can run in parallel (read-only, no side effects)
CONCURRENCY_SAFE_TOOLS = {"read_file", "list_files", "grep_search", "web_fetch"}


# ─── Permission rules (.nanocode/settings.json) ───────────────


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


# ─── Sub-agent fleet config (.nanocode/settings.json "agents" section) ──
#
# 控制子 agent 舰队的并发与超时上限（call-time enforcement 由 engine 消费）。
# 用户级 + 项目级 settings.json 合并（项目覆盖用户的逐键），与权限规则同源同缓存语义。
#   max_threads          : 同时运行的后台子 agent 上限（前台子 agent 阻塞父、天然串行，
#                          故 cap 只施于后台 spawn）。
#   max_depth            : 子 agent 代际深度上限（主=0，每下一层 +1）。今天子不能 spawn 孙
#                          （agent 工具被剥），故 live depth 结构上恒为 1；此为前瞻性纵深防御。
#   default_timeout_ms   : 前台子 agent 的回退 wall-clock 超时（工具入参 / manifest 都缺省时）。
#   background_timeout_ms: 后台子 agent 的回退 wall-clock 超时（同上）。
AGENTS_CONFIG_DEFAULTS: dict = {
    "max_threads": 4,
    "max_depth": 2,
    "default_timeout_ms": None,
    "background_timeout_ms": None,
}

_cached_agents_config: dict | None = None


def _coerce_int(value, default):
    """settings 整数字段归一：非法/缺省 -> default（绝不抛）。None 透传（=无超时）。"""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def load_agents_config() -> dict:
    """读取 .nanocode/settings.json 的 "agents" 段（用户 + 项目合并，缓存）。

    缺省时回退 AGENTS_CONFIG_DEFAULTS。项目级逐键覆盖用户级。仅识别已知键
    （max_threads / max_depth / default_timeout_ms / background_timeout_ms），
    其余忽略。整数字段非法 -> 回退默认；timeout 字段缺省 -> None（无超时）。
    """
    global _cached_agents_config
    if _cached_agents_config is not None:
        return _cached_agents_config

    merged = dict(AGENTS_CONFIG_DEFAULTS)
    user_settings = _load_settings(data_dir() / "settings.json")
    project_settings = _load_settings(project_config_dir() / "settings.json")
    for settings in [user_settings, project_settings]:
        if not settings or not isinstance(settings.get("agents"), dict):
            continue
        section = settings["agents"]
        for key in AGENTS_CONFIG_DEFAULTS:
            if key in section:
                merged[key] = section[key]

    cfg = {
        "max_threads": _coerce_int(merged.get("max_threads"),
                                   AGENTS_CONFIG_DEFAULTS["max_threads"]),
        "max_depth": _coerce_int(merged.get("max_depth"),
                                 AGENTS_CONFIG_DEFAULTS["max_depth"]),
        "default_timeout_ms": _coerce_int(merged.get("default_timeout_ms"), None),
        "background_timeout_ms": _coerce_int(merged.get("background_timeout_ms"), None),
    }
    _cached_agents_config = cfg
    return cfg


def load_permission_rules() -> dict:
    global _cached_rules
    if _cached_rules is not None:
        return _cached_rules

    allow: list[dict] = []
    deny: list[dict] = []

    user_settings = _load_settings(data_dir() / "settings.json")
    project_settings = _load_settings(project_config_dir() / "settings.json")

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
    "grep", "rg", "tree", "stat", "file", "date", "printenv",
    "du", "df", "uname", "hostname", "id", "ps", "true",
    "git status", "git log", "git diff", "git show",
    "git rev-parse", "git describe",
    "python --version", "python3 --version", "pip --version",
    "node --version", "npm --version",
)

# 命令组合 / 重定向 / 替换字符——出现即不进只读快速通道（fail-closed，安全方向）。
_SHELL_COMPOSITION_CHARS = set("|&;<>$`(){}\n")


def shell_sandbox_mode() -> str:
    """'off'（默认）| 'auto'（microVM 路由）| 'seatbelt'（原生 OS 沙盒）。
    控制 run_shell 是否经沙盒路由及走哪种后端。"""
    val = (os.environ.get("NANOCODE_SHELL_SANDBOX") or "off").strip().lower()
    return val if val in ("off", "auto", "seatbelt") else "off"


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
    - 只读白名单 → host（快速直跑）。
    - 其余（含危险命令）→ sandbox（auto→microVM；seatbelt→原生 OS 沙盒）。

    危险命令归类为 sandbox：default 下经 check_permission confirm（确认→进沙盒，受限），
    bypass 下无确认仍进沙盒（受限），真要上宿主须走 escalate=true。闭合 seatbelt+bypass
    下 `rm -rf .git` 在宿主裸跑的洞。"""
    if shell_sandbox_mode() == "off":
        return "host"
    if not (command or "").strip():
        return "host"
    if is_readonly_command(command):
        return "host"
    return "sandbox"


def _project_root(start: str | None = None) -> str:
    """向上walk找 .git 定位 git 项目根；没找到 .git → 回退 cwd。

    从 repo/src 启动时，cwd 锚不住 repo/.git/...；锚到项目根才挡得住 `../.git/hooks/x`。
    `.git` 既可能是目录，也可能是 gitfile（worktree/submodule 的 `.git` 是文件，内含
    `gitdir: ...`）→ 用 os.path.exists 而非 isdir，否则这类 repo 锚点回退、`../.git` 不受保护。

    已知限制（round-4，不修）：本锚点模型只把 `.git`（含 gitfile）所在目录当项目根，
    **不解析 gitfile 里 `gitdir:` 指向的真实元数据目录**。worktree/submodule 的真实
    `.git` 元数据落在 worktree 之外（如父仓 `.git/worktrees/<name>/...`、
    `.git/modules/<sub>/...`），那些外部目录不在 _is_protected_path 的保护范围内——
    bypass 下写它们不受阻。属边角、低实战风险，记录于 round-4 devlog，暂不展开跟随。
    """
    d = os.path.realpath(start or os.getcwd())
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.realpath(os.getcwd())  # 没找到 .git → 回退 cwd
        d = parent


def _is_protected_path(file_path: str) -> bool:
    """file_path 是否落在受保护元数据目录（.git/.nanocode/.claude/.codex/.agents）内。

    锚点为 git 项目根（向上找 .git）**和** cwd 两处，故从 repo/src 启动也能保护 repo/.git/…。
    """
    if not file_path:
        return False
    try:
        abs_p = os.path.realpath(file_path)
    except Exception:
        return False
    anchors = {_project_root(), os.path.realpath(os.getcwd())}
    for root in anchors:
        for pr in DEFAULT_PROTECTED_ROOTS:
            p = os.path.join(root, pr)
            if abs_p == p or abs_p.startswith(p + os.sep):
                return True
    return False


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

    # 受保护项目元数据目录写入：边界规则，先于 bypass/acceptEdits 裁决（与 deny 同属
    # 「bypass 越不过」的硬边界），否则 bypass 下可写穿 .git/hooks/... 等持久化路径。
    if tool_name in ("write_file", "edit_file") and _is_protected_path(inp.get("file_path", "")):
        if mode == "dontAsk":
            return {"action": "deny", "message": f"Auto-denied (dontAsk): protected path {inp.get('file_path', '')}"}
        return {"action": "confirm", "message": f"write into protected project dir: {inp.get('file_path', '')}"}

    # escalate=true 是沙盒逃逸到宿主的边界跨越：先于 bypass 裁决（与 deny/protected 同属
    # 「bypass 越不过」的硬边界），否则 bypass 下 escalate=true 会无确认直接上宿主。
    if tool_name == "run_shell" and shell_sandbox_mode() != "off" and inp.get("escalate"):
        if mode == "dontAsk":
            return {"action": "deny", "message": f"Auto-denied (dontAsk): escalate {inp.get('command', '')}"}
        return {"action": "confirm", "message": f"escalate to host (bypass sandbox): {inp.get('command', '')}"}

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

    if tool_name == "run_shell" and is_dangerous(inp.get("command", "")):
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

    return {"action": "allow"}


def reset_permission_cache() -> None:
    global _cached_rules, _cached_agents_config
    _cached_rules = None
    _cached_agents_config = None
