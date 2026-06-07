#!/usr/bin/env python3
"""最小 stdio MCP server，暴露一个 echo 工具——nanocode 的 MCP 集成示例。"""
import json
import sys


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = req.get("id")
        method = req.get("method")
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "serverInfo": {"name": "echo", "version": "1.0.0"}}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{
                "name": "echo", "description": "Echo the given text",
                "inputSchema": {"type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"]}}]}})
        elif method == "tools/call":
            args = (req.get("params") or {}).get("arguments") or {}
            _send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": args.get("text", "")}]}})
        elif mid is not None:
            _send({"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    main()
