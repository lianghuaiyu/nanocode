# Agent 主循环

> 源码位置：`src/nanocode/agent/`
> 关键文件：`engine.py`、`anthropic_backend.py`、`openai_backend.py`、`models.py`

Agent 主循环是 nanocode 的心脏：它把对话历史送入大模型，接收文本与工具调用，执行工具，把结果追加回历史，再次调用模型——如此往复，直到模型不再请求工具为止。`engine.py` 负责编排，两个后端文件负责各自 API 形态的具体流式实现。

## 它解决什么

一个 coding agent 的本质是"让模型在多轮中自主使用工具完成任务"。要做到这一点，需要一个循环来：维护消息历史、决定每轮发什么、把模型产生的工具调用真正执行、并在上下文将满时压缩。nanocode 把这些职责拆成"编排"（`Agent` 类）与"后端"（两个 mixin），让主流程保持可读，同时支持两种差异很大的 API。

## 它如何工作

`Agent` 类（`engine.py`）通过多继承组合三个 mixin：`AnthropicBackendMixin`、`OpenAIBackendMixin`、`PlanModeMixin`。构造时根据是否传入 `api_base` 决定走 OpenAI 兼容路径还是 Anthropic 原生路径，并初始化对应客户端、消息历史与系统提示词。

`chat()` 是主入口。它在首次调用时惰性连接 MCP 服务器，然后分派到 `_chat_anthropic()` 或 `_chat_openai()`。两个后端的循环结构一致：

1. 把用户消息追加到历史，在轮次边界检查是否需要 auto-compact。
2. 进入 `while` 循环：先跑分层压缩流水线（`_run_compression_pipeline`），再发起一次流式 API 调用。
3. 流式过程中逐字输出文本；当一个工具调用块完成时，若它是并发安全工具且权限为 allow，立即创建 asyncio 任务提前执行（见下）。
4. 收到完整响应后累计 token、记录最后一次调用时间。若响应没有工具调用，打印成本并退出循环。
5. 否则对每个工具调用做权限检查、执行、把结果（必要时经大结果落盘）封装成 `tool_result` 追加回历史，进入下一轮。

工具调用的统一分发在 `engine.py` 的 `_execute_tool_call()`：`enter/exit_plan_mode` 走 `PlanModeMixin`，`agent` 与 `skill` 走 engine 内的子 Agent/技能执行，`mcp__*` 路由到 MCP 管理器，其余交给 `tools.execute_tool`。

**双后端差异**：Anthropic 路径用原生 `messages.stream`，工具结果以 `tool_result` 内容块挂在 user 消息里，并支持 `thinking` 扩展思考；OpenAI 路径用 `chat.completions` 流式，工具结果以独立的 `role="tool"` 消息表达，并需自行把流式 delta 组装成完整的 `tool_calls`。两者共享 `models.py` 提供的模型元数据（上下文窗口、最大输出、是否支持思考）、重试封装 `_with_retry` 和工具格式转换 `_to_openai_tools`。

**流式工具早期执行**：在 Anthropic 流式回调 `_on_tool_block` 中，只读且权限放行的工具会在模型还在生成后续内容时就开始执行。等到处理工具结果时，这些"早启动"的任务只需 `await` 取回结果，从而把 IO 等待与模型生成重叠起来。

**预算控制**：每完成一轮工具调用，`_check_budget()` 检查累计估算费用（`--max-cost`）与轮次数（`--max-turns`），任一超限即打印原因并跳出循环。

**子 Agent 与技能 fork**：`_execute_agent_tool` 与 `_execute_skill_tool`（fork 模式）会构造一个 `is_sub_agent=True` 的新 `Agent`，用 `run_once()` 独立跑完任务，回传文本与本次 token 增量；子 Agent 不打印分隔线、不自动保存会话，输出被缓冲后整体返回给父 Agent。

## 关键数据流 / 取舍

```
user_message
  → 追加历史 → check_and_compact
  → loop:
      run_compression_pipeline
      stream API ──(tool_use 块完成)──▶ 并发安全工具早启动
      累计 token
      无 tool_use? → 退出
      有 tool_use → 权限检查 → 执行 → 大结果落盘 → tool_result 追加历史
      check_budget → 超限退出
```

取舍：循环以同步阻塞的多轮迭代为主，结构清晰但不做投机式多步预测；子 Agent 在同进程内以协程运行，隔离来自工具白名单与权限模式，而非操作系统级沙箱；Plan Mode 的只读保证由权限层强制，而非独立运行时。
