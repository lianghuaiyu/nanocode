import asyncio
import sys
from nanocode.mcp import McpManager, McpConnection


def test_tool_definitions_prefix():
    m = McpManager()
    m._tools = [{"name": "echo", "description": "d",
                 "inputSchema": {"type": "object"}, "serverName": "srv"}]
    defs = m.get_tool_definitions()
    assert defs[0]["name"] == "mcp__srv__echo"
    assert defs[0]["input_schema"]["type"] == "object"


def test_echo_server_integration():
    async def go():
        conn = McpConnection("echo", sys.executable, ["examples/mcp_echo_server.py"])
        await conn.connect()
        await conn.initialize()
        tools = await conn.list_tools()
        names = [t["name"] for t in tools]
        out = await conn.call_tool("echo", {"text": "hi"})
        conn.close()
        return names, out

    names, out = asyncio.run(go())
    assert "echo" in names
    assert out == "hi"


# ── docs/26 G6: extension-declared MCP servers (out-of-process tier) ─────────

def test_add_extension_servers_merges_before_connect():
    m = McpManager()
    m.add_extension_servers({"acme-tools-files": {"command": "echo", "args": ["x"]}})
    cfgs = m._load_configs()
    assert cfgs.get("acme-tools-files") == {"command": "echo", "args": ["x"]}


def test_add_extension_servers_after_connect_raises():
    import pytest
    m = McpManager()
    m._connected = True  # simulate post-first-turn connect
    with pytest.raises(RuntimeError):
        m.add_extension_servers({"x": {"command": "echo"}})
