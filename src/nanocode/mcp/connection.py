"""单个 MCP 连接：管理一个 stdio 子进程并通过 JSON-RPC 收发消息。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from typing import Any


# ─── Single MCP connection (one per server) ──────────────────


class McpConnection:
    """Manages a single MCP server process and JSON-RPC communication."""

    def __init__(self, server_name: str, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None):
        self.server_name = server_name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None

    async def connect(self) -> None:
        """Spawn the server process."""
        merged_env = {**os.environ, **self.env}
        self._process = await asyncio.create_subprocess_exec(
            self.command, *self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged_env,
        )
        # Start reading stdout lines in background
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """Read newline-delimited JSON-RPC responses from stdout."""
        assert self._process and self._process.stdout
        while True:
            line = await self._process.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_id = msg.get("id")
            if msg_id is not None and msg_id in self._pending:
                fut = self._pending.pop(msg_id)
                if "error" in msg:
                    e = msg["error"]
                    fut.set_exception(
                        RuntimeError(f"MCP error {e.get('code')}: {e.get('message')}")
                    )
                else:
                    fut.set_result(msg.get("result"))

    async def _send_request(self, method: str, params: dict | None = None) -> Any:
        """Send a JSON-RPC request and wait for response."""
        assert self._process and self._process.stdin
        req_id = self._next_id
        self._next_id += 1
        msg = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        self._process.stdin.write((msg + "\n").encode())
        await self._process.stdin.drain()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        return await fut

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if not self._process or not self._process.stdin:
            return
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}})
        self._process.stdin.write((msg + "\n").encode())

    async def initialize(self) -> None:
        """Perform MCP initialize handshake."""
        await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "nanocode", "version": "1.0.0"},
        })
        self._send_notification("notifications/initialized")

    async def list_tools(self) -> list[dict]:
        """Discover available tools from this server."""
        result = await self._send_request("tools/list")
        if not result or not isinstance(result.get("tools"), list):
            return []
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema"),
                "serverName": self.server_name,
            }
            for t in result["tools"]
        ]

    async def call_tool(self, name: str, args: dict) -> str:
        """Call a tool and return the text result."""
        result = await self._send_request("tools/call", {"name": name, "arguments": args})
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            return "\n".join(
                c["text"] for c in result["content"] if c.get("type") == "text"
            )
        return json.dumps(result)

    def close(self) -> None:
        """Kill the server process."""
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._process:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
            self._process = None
        # Reject pending requests
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(f"MCP server '{self.server_name}' closed"))
        self._pending.clear()
