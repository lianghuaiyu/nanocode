"""docs/19 Phase 1：public tool input 严格校验 + schema 闭合。

模型 raw input 在进入 permission/executor 之前变成 validated public args：
下划线键 / unknown key / 缺 required / 类型不符一律 reject（不 silent strip）。
"""

from nanocode.capabilities.validation import validate_tool_input
from nanocode.tools.spec import TOOLS


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


# ─── schema 闭合性：所有 public tool 都 additionalProperties:false ──

def test_all_public_schemas_closed():
    for name, spec in TOOLS.items():
        insch = spec.schema.get("input_schema", {})
        assert insch.get("additionalProperties") is False, f"{name} schema not closed"


def test_sandbox_shell_not_public():
    assert "sandbox_shell" not in TOOLS
