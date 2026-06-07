# Trace —— agent 轨迹记录

> 源码位置：`src/nanocode/trace/`
> 关键文件：`tracer.py`（Tracer/NullTracer）、`sinks.py`（Sink 协议/JsonlSink）、`config.py`（开关/输出）
> 开关：默认关闭；`--trace` 或 `NANOCODE_TRACE=1` 开启

## 它解决什么
低侵入地记录一次运行的完整轨迹（trajectory）用于离线分析：用户消息、LLM 请求（含消息快照）、assistant 文本/thinking/工具调用、工具结果、权限决策、压缩、预算、token 用量。默认关闭，开启时零数据丢失（逐条 flush）。

## 它如何工作
Agent 持一个 `tracer`：关闭时是 `NullTracer`（`emit` 为零开销 no-op，不创建任何文件）；开启时是 `Tracer`，在循环约 10 个节点调用 `tracer.emit(type, **fields)`。事件统一附 `v/ts/session_id/parent_session_id/seq/type`，经可插拔 `Sink` 写出。默认 `JsonlSink` → `./.nanocode/traces/<session_id>.jsonl`（一行一事件）。`trace/` 是叶子包，不依赖核心；核心只多约 10 行 emit。instrumentation 吞掉一切异常，绝不影响 agent。

## 子 Agent 嵌套
子 Agent 由父传 `trace_parent=self.tracer`，在构造时 `trace_parent.child(session_id)` 派生：共享同一组 sink、带 `parent_session_id`。据此可重建嵌套调用树。

## 事件类型（schema v1）
`session_start, user_message, llm_request, assistant_message, llm_response, permission_decision, tool_call, tool_result, compaction, budget_exceeded, turn_end, session_end`

## 如何加自定义后端（sink）
实现 `Sink` 协议（`write(event: dict)` / `close()`），构造时传入：
```python
from nanocode.trace import make_tracer
tracer = make_tracer(session_id, enabled=True, sinks=[MyOTelSink(), MySqliteSink()])
```
或在 `config.build_default_sinks` 中追加。事件是普通 dict、与 schema 解耦，新增事件类型无需改 sink。

## 查看/汇总 trace（`nanocode trace`）

只读子命令，渲染 `./.nanocode/traces/` 下的 JSONL：

- `nanocode trace` —— 列出所有会话（id / 时间 / 事件数 / 估算费用 / 首条消息）。
- `nanocode trace <id>` —— 打印该会话紧凑时间线（每事件一行，子 Agent 缩进嵌套）。
  - id 支持前缀匹配与 `latest`。
- `nanocode trace <id> --full` —— 展开消息/工具结果全文。
- `nanocode trace <id> --summary` —— 汇总（轮数、各工具调用次数、token、估算费用、时长、子 Agent 数、budget/deny 次数）。

实现：`src/nanocode/trace/report.py`（纯函数渲染）+ `src/nanocode/entrypoints/trace_cmd.py`（命令处理器）。
