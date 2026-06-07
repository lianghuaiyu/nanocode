import asyncio
import sys
from nanocode.mcp import McpManager, McpConnection


def test_is_mcp_tool():
    m = McpManager()
    assert m.is_mcp_tool("mcp__srv__do") is True
    assert m.is_mcp_tool("read_file") is False


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
