"""权限判定：5 种权限模式 + .nanocode/settings.json 声明式 allow/deny 规则。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

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

# ─── Context engineering config (.nanocode/settings.json "context" section) ──
# map_tokens：Aider 同款预算开关；未指定则按模型默认决定，0 表示禁用。
# map_refresh：repo map 结果缓存档（aider 同名）——auto（默认,贵才缓存）/
# files（文件集不变即缓存）/always（每次重算）/manual（首算后固定）。
# map_multiplier_no_files：无已读/已改文件时的预算倍率；Aider CLI 当前默认 2。
CONTEXT_CONFIG_DEFAULTS: dict = {
    "map_tokens": None,
    "map_refresh": "auto",
    "map_multiplier_no_files": 2,
}

_REPO_MAP_REFRESH_MODES = frozenset({"auto", "files", "always", "manual"})
_cached_context_config: dict | None = None


def load_context_config() -> dict:
    """读取 settings.json 的 "context" 段（用户 + 项目合并，缓存；同 load_agents_config 语义）。"""
    global _cached_context_config
    if _cached_context_config is not None:
        return _cached_context_config
    merged = dict(CONTEXT_CONFIG_DEFAULTS)
    for settings in [_load_settings(data_dir() / "settings.json"),
                     _load_settings(project_config_dir() / "settings.json")]:
        if not settings or not isinstance(settings.get("context"), dict):
            continue
        section = settings["context"]
        for key in CONTEXT_CONFIG_DEFAULTS:
            if key in section:
                merged[key] = section[key]
    if os.environ.get("NANOCODE_MAP_TOKENS") is not None:
        merged["map_tokens"] = os.environ.get("NANOCODE_MAP_TOKENS")
    if os.environ.get("NANOCODE_MAP_REFRESH") is not None:
        merged["map_refresh"] = os.environ.get("NANOCODE_MAP_REFRESH")
    if os.environ.get("NANOCODE_MAP_MULTIPLIER_NO_FILES") is not None:
        merged["map_multiplier_no_files"] = os.environ.get("NANOCODE_MAP_MULTIPLIER_NO_FILES")
    refresh = merged.get("map_refresh", "auto")
    tokens = _coerce_optional_int(merged.get("map_tokens"))
    multiplier = _coerce_positive_float(merged.get("map_multiplier_no_files"), 2.0)
    cfg = {
        "map_tokens": tokens,
        "map_refresh": refresh if refresh in _REPO_MAP_REFRESH_MODES else "auto",
        "map_multiplier_no_files": multiplier,
    }
    _cached_context_config = cfg
    return cfg


def _coerce_nonnegative_int(value, default):
    try:
        out = int(value)
    except (ValueError, TypeError):
        return default
    return out if out >= 0 else default


def _coerce_optional_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _coerce_positive_float(value, default):
    try:
        out = float(value)
    except (ValueError, TypeError):
        return default
    return out if out > 0 else default


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
    if tool_name == "run_shell":
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
            and tool_name == "run_shell"
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


# 命令组合 / 重定向 / 替换字符——allow 前缀规则遇之 fail-closed（不跨组合符延伸放行）。
_SHELL_COMPOSITION_CHARS = set("|&;<>$`(){}\n")

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
    # docs/19：受保护根的解析（含 .git gitdir pointer target → worktree/submodule 真实元数据目录）
    # 与 SandboxManager 共用单一逻辑（capabilities.sandbox.protected_roots_for_workspace），闭合
    # write_file/edit_file 对 gitfile 指向的外部 .git/worktrees|modules 的保护缺口（review HIGH）。
    # lazy import：capabilities 包 __init__ 会 import tools.permissions，顶层 import 成环。
    from ..capabilities.sandbox import protected_roots_for_workspace
    anchors = {_project_root(), os.path.realpath(os.getcwd())}
    for root in anchors:
        for p in protected_roots_for_workspace(Path(root)):
            ps = str(p)
            if abs_p == ps or abs_p.startswith(ps + os.sep):
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
    # docs/19：sandbox 默认常开（profile 投影），escalate 永远需要明确审批；非交互 → deny。
    if tool_name == "run_shell" and inp.get("escalate"):
        # 后台命令不支持宿主提权（spawn 后无法交互审批 + 无前台流）→ fail-closed deny，不静默降级受限。
        if inp.get("run_in_background"):
            return {"action": "deny", "message": (
                "host escalation is not supported for background commands; "
                "run it in the foreground (confined or escalated) instead")}
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
        if tool_name in ("run_shell",):
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
    global _cached_rules, _cached_agents_config, _cached_context_config
    _cached_rules = None
    _cached_agents_config = None
    _cached_context_config = None


# ─── Sub-agent call-time allowlist (P4) ──────────────────────────
# 纯宿主 meta 工具（无持久副作用、不经 execute_tool）→ allowlist 永不约束。
# 注意：'memory'（save/update/delete 落真实 memory_tool）、'agent'（子不能 spawn 孙）、
# 'skill'（fork/hook 可触达 shell）都【不在】此集——必须受 allowlist 约束。
ALWAYS_ALLOWED_META = frozenset({
    "task_list", "task_output", "task_stop",
    "enter_plan_mode", "exit_plan_mode",
    "get_subagent_result", "run_list", "run_status", "run_output", "run_cancel", "run_send",
})
AGENT_META_TOOL = "agent"  # 'agent' 元工具：子 agent 一律不可调用（独立 fail-closed 后备）


def allowlist_blocks(name: str, allowed_tool_names: "set[str] | None") -> bool:
    """子 agent call-time allowlist 判定（纯函数；从 engine._tool_blocked_by_allowlist 上移）。

    - allowed_tool_names 为 None（主 agent / 未约束）→ 永不拦截。
    - 'agent' → 子 agent 一律拦截（独立后备，不依赖工具表是否剥了它）。
    - 纯宿主 meta（task_*/plan_mode）→ 放行。
    - 其余（含 memory/skill/run_shell/真实工具）→ 不在有效集内即拦截。
    """
    if allowed_tool_names is None:
        return False
    if name == AGENT_META_TOOL:
        return True
    if name in ALWAYS_ALLOWED_META:
        return False
    return name not in allowed_tool_names


# ─── PermissionEngine：工具派发的单一可测决策点 ───────────────────

@dataclass
class Decision:
    """一次工具派发授权的统一决策。

    action: ``"allow" | "deny" | "confirm"``（``confirm`` == request-approval；沿用现有线值
    以不改用户语义）。allowlist_blocked: 子 agent call-time allowlist 是否拦截——供 callgate
    (_execute_tool_call) 据此 fail-closed。
    """
    action: str
    message: str = ""
    allowlist_blocked: bool = False


class PermissionEngine:
    """工具派发的单一决策点：合并权限策略(check_permission) + 子 agent allowlist。

    纯决策——不弹审批 UI、不 emit 事件、不执行工具。审批交互由调用方据 ``action=="confirm"``
    处理（保持 dedupe / 子 agent 身份装饰语义不变）；allowlist 的 fail-closed 兜底由 callgate
    据 ``allowlist_blocked`` 施加。两后端 + callgate + 未来 App Server/SDK 都经此入口，policy
    不再各处散落。

    读取 agent 的 live 权限上下文（``permission_mode`` / ``_plan_file_path`` /
    ``_allowed_tool_names``），故 mode/plan-file 随会话变化自动生效；单测可传任意带这三个
    属性的对象。
    """

    def __init__(self, agent) -> None:
        self._agent = agent

    def allowlist_blocks(self, name: str) -> bool:
        return allowlist_blocks(name, self._agent._allowed_tool_names)

    def check(self, name: str, inp: dict) -> Decision:
        """单一入口：返回 policy action + allowlist 标记（纯决策，无副作用）。

        docs/16 #7b：内部改走 capabilities.permissions 的不可变 PermissionContext + decide()
        ——live 属性每次 check 快照成 ctx（mode/plan-file 随会话变化仍实时生效），裁决逻辑
        单点在 decide（与 profile 驱动的 spawn / 未来 SDK 宿主共用同一基底）。"""
        from ..capabilities.permissions import PermissionContext, decide  # lazy：避免包级 import 环
        a = self._agent
        ctx = PermissionContext(
            mode=a.permission_mode, plan_file_path=a._plan_file_path,
            allowed_tool_names=(frozenset(a._allowed_tool_names)
                                if a._allowed_tool_names is not None else None))
        return decide(ctx, name, inp)
