"""MCP 管理器：加载配置、连接所有服务器、聚合工具并按前缀路由调用。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .connection import McpConnection


# ─── MCP Manager ─────────────────────────────────────────────


class McpManager:
    """Manages all MCP server connections. Call load_and_connect() once, then
    use get_tool_definitions() and call_tool() to integrate with the agent."""

    def __init__(self):
        self._connections: dict[str, McpConnection] = {}
        self._tools: list[dict] = []
        self._connected = False

    async def load_and_connect(self) -> None:
        """Read settings, connect to all configured MCP servers, discover tools."""
        if self._connected:
            return
        self._connected = True

        configs = self._load_configs()
        if not configs:
            return

        timeout = 15.0

        for name, cfg in configs.items():
            conn = McpConnection(
                name,
                cfg["command"],
                cfg.get("args"),
                cfg.get("env"),
            )
            try:
                await conn.connect()
                await asyncio.wait_for(conn.initialize(), timeout=timeout)
                server_tools = await asyncio.wait_for(conn.list_tools(), timeout=timeout)
                self._connections[name] = conn
                self._tools.extend(server_tools)
                from ..ui import is_verbose
                if is_verbose():
                    print(f"[mcp] Connected to '{name}' — {len(server_tools)} tools", flush=True)
            except Exception as e:
                print(f"[mcp] Failed to connect to '{name}': {e}", flush=True)
                conn.close()

    def get_tool_definitions(self) -> list[dict]:
        """Return tool definitions in Anthropic API format with mcp__ prefix."""
        return [
            {
                "name": f"mcp__{t['serverName']}__{t['name']}",
                "description": t.get("description") or f"MCP tool {t['name']} from {t['serverName']}",
                "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
            }
            for t in self._tools
        ]

    def is_mcp_tool(self, name: str) -> bool:
        """Check if a tool name is an MCP-prefixed tool."""
        return name.startswith("mcp__")

    async def call_tool(self, prefixed_name: str, args: dict) -> str:
        """Route a prefixed tool call to the correct server."""
        parts = prefixed_name.split("__")
        if len(parts) < 3:
            raise ValueError(f"Invalid MCP tool name: {prefixed_name}")
        server_name = parts[1]
        tool_name = "__".join(parts[2:])  # tool name might contain __
        conn = self._connections.get(server_name)
        if not conn:
            raise RuntimeError(f"MCP server '{server_name}' not connected")
        return await conn.call_tool(tool_name, args)

    async def disconnect_all(self) -> None:
        """Disconnect all servers."""
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()
        self._tools.clear()
        self._connected = False

    # ─── Config loading ──────────────────────────────────────

    def _load_configs(self) -> dict[str, dict]:
        merged: dict[str, dict] = {}

        # 1. Global: ~/.claude/settings.json
        global_path = Path.home() / ".claude" / "settings.json"
        self._merge_config_file(global_path, merged)

        # 2. Project: .claude/settings.json (cwd)
        project_path = Path.cwd() / ".claude" / "settings.json"
        self._merge_config_file(project_path, merged)

        # 3. Also check .mcp.json (Claude Code convention)
        mcp_json_path = Path.cwd() / ".mcp.json"
        self._merge_config_file(mcp_json_path, merged)

        return merged

    def _merge_config_file(self, path: Path, target: dict[str, dict]) -> None:
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text())
            servers = raw.get("mcpServers", raw)
            for name, config in servers.items():
                if isinstance(config, dict) and "command" in config:
                    target[name] = config
        except Exception:
            pass  # skip malformed config
