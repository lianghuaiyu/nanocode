"""docs/19 Phase 1：public tool input 严格校验 + schema 闭合。

模型 raw input 在进入 permission/executor 之前变成 validated public args：
下划线键 / unknown key / 缺 required / 类型不符一律 reject（不 silent strip）。
"""

from nanocode.capabilities.validation import validate_tool_input
from nanocode.tools import REGISTRY


# ─── 隐藏字段（下划线键）一律拒 ─────────────────────────────────

def test_rejects_cwd_spoof():
    err = validate_tool_input("run_shell", {"command": "pwd", "_cwd": "/"})
    assert err is not None and "_cwd" in err


def test_rejects_session_id_spoof():
    err = validate_tool_input("run_shell", {"command": "pwd", "_session_id": "evil"})
    assert err is not None and "_session_id" in err


def test_rejects_underscore_key_even_for_unknown_tool():
    # MCP/未登记工具无本地 schema，但下划线键仍普适拒绝。
    assert validate_tool_input("mcp__server__tool", {"_cwd": "/"}) is not None


# ─── unknown / 已删参数拒（additionalProperties:false）────────────

def test_rejects_unknown_key():
    assert validate_tool_input("run_shell", {"command": "pwd", "mount_workspace": True}) is not None
    assert validate_tool_input("run_shell", {"command": "pwd", "network": "public"}) is not None


def test_rejects_missing_required():
    assert validate_tool_input("run_shell", {}) is not None
    assert validate_tool_input("read_file", {}) is not None


def test_rejects_type_mismatch():
    assert validate_tool_input("run_shell", {"command": 123}) is not None
    assert validate_tool_input("run_shell", {"command": "pwd", "run_in_background": "yes"}) is not None


# ─── 合法输入通过 ────────────────────────────────────────────────

def test_accepts_valid_run_shell():
    assert validate_tool_input("run_shell", {"command": "pwd"}) is None
    assert validate_tool_input(
        "run_shell", {"command": "pwd", "timeout": 5000, "run_in_background": False,
                      "escalate": True}) is None


def test_accepts_valid_read_file():
    assert validate_tool_input("read_file", {"file_path": "/x", "offset": 1, "limit": 5}) is None


def test_unknown_tool_without_underscore_passes():
    # 无本地 schema 的 MCP 工具，普通键放行（其 schema 由 MCP server 把关）。
    assert validate_tool_input("mcp__server__tool", {"anything": 1}) is None


# ─── MCP parity：source=MCP 工具登记进 overlay 后，结构校验仍不收紧（docs/24 Phase 4b）──
# MCP 工具自 Phase 4b 起带 server 原始 inputSchema 进 per-agent overlay。即便 server 声明
# required + additionalProperties:false + typed properties，本地校验也**不**二次否决（保旧
# pass-through parity）——仅下划线键守卫保留。否则 server 的合法 required/closed 调用会被本地误拒。

def _mcp_overlay_registry():
    from nanocode.tools.registry import REGISTRY
    from nanocode.tools.spec import Tool
    from nanocode.tools.types import ToolSource, Trust
    d = {
        "name": "mcp__server__tool",
        "description": "x",
        "input_schema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
            "additionalProperties": False,
        },
    }
    return REGISTRY.overlay(
        [Tool(schema=d, run=None, source=ToolSource.MCP, trust=Trust.UNTRUSTED, needs=frozenset())]
    )


def test_mcp_required_not_enforced():
    reg = _mcp_overlay_registry()
    # server 声明 required=['q']，但本地不否决缺失。
    assert validate_tool_input("mcp__server__tool", {}, registry=reg) is None


def test_mcp_closed_schema_not_enforced():
    reg = _mcp_overlay_registry()
    # server additionalProperties:false，但本地不否决未知键。
    assert validate_tool_input("mcp__server__tool", {"q": "a", "extra": 1}, registry=reg) is None


def test_mcp_type_not_enforced():
    reg = _mcp_overlay_registry()
    # server 声明 q:string，但本地不否决类型不符。
    assert validate_tool_input("mcp__server__tool", {"q": 123}, registry=reg) is None


def test_mcp_valid_passes():
    reg = _mcp_overlay_registry()
    assert validate_tool_input("mcp__server__tool", {"q": "a"}, registry=reg) is None


def test_mcp_underscore_key_still_rejected():
    reg = _mcp_overlay_registry()
    # 下划线键守卫普适——即便 source=MCP 也封死隐藏字段注入。
    assert validate_tool_input("mcp__server__tool", {"q": "a", "_cwd": "/"}, registry=reg) is not None


def test_builtins_still_strict_with_mcp_overlay():
    reg = _mcp_overlay_registry()
    # MCP 跳过不波及内置工具：同一 overlay registry 下内置仍严格。
    assert validate_tool_input("run_shell", {}, registry=reg) is not None


# ─── schema 闭合性：所有 public tool 都 additionalProperties:false ──

def test_all_public_schemas_closed():
    for name in REGISTRY.names():
        insch = REGISTRY.get(name).schema.get("input_schema", {})
        assert insch.get("additionalProperties") is False, f"{name} schema not closed"


def test_sandbox_shell_not_public():
    assert REGISTRY.get("sandbox_shell") is None
