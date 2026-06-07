# MCP 集成

> 源码位置：`src/nanocode/mcp/`
> 关键文件：`connection.py`（单连接）、`manager.py`（多服务器管理与路由）；示例 `examples/mcp_echo_server.py`

MCP（Model Context Protocol）集成让 nanocode 把外部工具服务器接入自己的工具系统。它通过 JSON-RPC over stdio 启动并连接服务器进程，完成握手、发现远端工具，把这些工具以 `mcp__server__tool` 前缀混入工具表，并在模型调用时路由回正确的服务器。

## 它解决什么

内置工具是有限的，而很多能力（访问数据库、调用内部服务、操作特定系统）更适合由独立的工具服务器提供。MCP 是一个让 agent 与外部工具服务器对话的协议。nanocode 需要：按配置启动这些服务器、用标准协议发现它们暴露的工具、把工具透明地呈现给模型、并在调用时找回对应进程——整个过程对 Agent 循环尽量无侵入。

## 它如何工作

**单连接**（`connection.py` 的 `McpConnection`）：每个 MCP 服务器对应一个连接对象，管理一个 stdio 子进程。
- `connect()` 用 `asyncio.create_subprocess_exec` 启动服务器（合并当前环境与配置的 `env`），并起一个后台读循环 `_read_loop` 持续读取 stdout 的按行 JSON-RPC 响应，按 `id` 把结果投递给对应的 future。
- `_send_request` 发请求并 await 一个 future；`_send_notification` 发无需响应的通知。
- `initialize()` 执行握手：发 `initialize`（带 `protocolVersion`、`clientInfo: {name: "nanocode", ...}`），再发 `notifications/initialized` 通知。
- `list_tools()` 发 `tools/list`，把每个工具规整为 `{name, description, inputSchema, serverName}`。
- `call_tool(name, args)` 发 `tools/call`，从结果的 `content` 数组里抽出文本拼接返回。
- `close()` 取消读循环、杀子进程，并让所有挂起请求以异常结束。

**多服务器管理**（`manager.py` 的 `McpManager`）：
- `load_and_connect()` 读取配置、对每个服务器依次 `connect` → `initialize` → `list_tools`，成功则登记连接并收集其工具，单个服务器失败只打印日志不影响其余（握手与发现各有 15 秒超时）。
- `get_tool_definitions()` 把收集到的远端工具转成 Anthropic 工具 schema，名称加 `mcp__<server>__<tool>` 前缀，避免与内置工具或不同服务器的同名工具冲突。
- `is_mcp_tool(name)` 判断工具名是否以 `mcp__` 开头；`call_tool(prefixed_name, args)` 按前缀拆出服务器名与工具名（工具名本身可含 `__`），找到对应连接转发调用。

**与 Agent 的接入**：`Agent.chat()` 在主 Agent 首次对话时惰性调用 `load_and_connect()`，把 `get_tool_definitions()` 返回的远端工具追加到 `self.tools`。此后模型若调用 `mcp__*` 工具，`engine._execute_tool_call` 经 `is_mcp_tool` 识别并交给 `McpManager.call_tool` 路由。

**配置**（`manager._load_configs`）：按顺序合并三处配置——`~/.claude/settings.json`、`./.claude/settings.json`、`./.mcp.json`，取其 `mcpServers`（或顶层对象）中带 `command` 的条目。仓库自带可运行示例 `examples/mcp_echo_server.py`，配套 `.mcp.json`：

```json
{
  "mcpServers": {
    "echo": {
      "command": "python",
      "args": ["examples/mcp_echo_server.py"]
    }
  }
}
```

该 echo server 是一个最小 stdio MCP 服务器，实现 `initialize` / `tools/list` / `tools/call`，暴露一个把输入文本原样返回的 `echo` 工具，可用来端到端验证连接、发现与调用链路。

## 关键数据流 / 取舍

```
首次 chat:
  load_and_connect → 对每个配置:
     spawn 进程 → initialize 握手 → tools/list 发现
  get_tool_definitions → mcp__server__tool 前缀 → 混入 self.tools

模型调用 mcp__echo__echo(args):
  engine.is_mcp_tool → McpManager.call_tool
     拆前缀 → 找连接 → tools/call → 抽 content 文本返回
```

取舍：传输限定为 stdio + 按行 JSON-RPC，实现简单、依赖少；连接在首次对话时惰性建立，单服务器失败被隔离为日志而不致命；工具名前缀路由用 `__` 分隔且允许工具名内含 `__`，以服务器名做命名空间隔离冲突。
