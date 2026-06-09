"""EventSink：Agent core 与表现层（terminal UI）之间的注入式边界。

P-1 解耦目标 (3)：core（engine + backends + models）不再直接 import ..ui / rich。
所有 print_* / spinner / 助手文本 / 重试通知都经 self._sink.<event>() 发出；默认
TerminalSink 包装现有 ui.py（主 agent 行为零变化），子 agent 注入 BufferSink（捕获
助手文本到 buffer、其余 UI 事件抑制）——复刻今天 _output_buffer 的语义。

事件集来自 P-1 测绘（map-p1-decouple-surface）：assistant_markdown / thinking /
spinner_start / spinner_stop / tool_call / tool_result / cost / info / confirmation /
sub_agent_start / sub_agent_end / retry。core 对 sink 只调用、不感知具体实现，故无
UI sink（NullSink/BufferSink）下可跑一个完整 fake turn。

P-1 容许的唯一行为偏移（cross-validation 记录）：后台子 agent 流式输出文本时不再停掉
父 agent 正在转的 spinner（旧代码经全局 `ui.stop_spinner()` 会误停父 spinner；现走子的
BufferSink.spinner_stop() = no-op）。这本是无内容、时序依赖的 spinner 抖动，新行为严格更
正确、无用户可见语义影响。子 agent 的 retry 通知同理被 BufferSink 抑制（旧经全局 print_retry
打到父 console）——属可接受的更干净抑制。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EventSink(Protocol):
    """core 向表现层发事件的协议（全部为 fire-and-forget，绝不影响 agent 控制流）。"""

    def assistant_markdown(self, text: str) -> None: ...
    def thinking(self, text: str) -> None: ...
    def spinner_start(self, label: str = "Thinking") -> None: ...
    def spinner_stop(self) -> None: ...
    def tool_call(self, name: str, inp: dict) -> None: ...
    def tool_result(self, name: str, result: str) -> None: ...
    def cost(self, input_tokens: int, output_tokens: int) -> None: ...
    def info(self, message: str) -> None: ...
    def confirmation(self, command: str) -> None: ...
    def sub_agent_start(self, agent_type: str, description: str) -> None: ...
    def sub_agent_end(self, agent_type: str, description: str) -> None: ...
    def retry(self, attempt: int, max_retries: int, reason: str) -> None: ...


class TerminalSink:
    """默认 sink：把事件委托给现有 ui.py（rich Console + 线程 spinner）。

    主 agent 用它——逐方法转调既有 ui 函数，故行为与解耦前逐字节一致；core 不再
    直接 import ..ui，rich 的耦合收敛到此处。
    """

    def assistant_markdown(self, text: str) -> None:
        from .. import ui
        ui.render_assistant_markdown(text)

    def thinking(self, text: str) -> None:
        from .. import ui
        ui.render_thinking(text)

    def spinner_start(self, label: str = "Thinking") -> None:
        from .. import ui
        ui.start_spinner(label)

    def spinner_stop(self) -> None:
        from .. import ui
        ui.stop_spinner()

    def tool_call(self, name: str, inp: dict) -> None:
        from .. import ui
        ui.print_tool_call(name, inp)

    def tool_result(self, name: str, result: str) -> None:
        from .. import ui
        ui.print_tool_result(name, result)

    def cost(self, input_tokens: int, output_tokens: int) -> None:
        from .. import ui
        ui.print_cost(input_tokens, output_tokens)

    def info(self, message: str) -> None:
        from .. import ui
        ui.print_info(message)

    def confirmation(self, command: str) -> None:
        from .. import ui
        ui.print_confirmation(command)

    def sub_agent_start(self, agent_type: str, description: str) -> None:
        from .. import ui
        ui.print_sub_agent_start(agent_type, description)

    def sub_agent_end(self, agent_type: str, description: str) -> None:
        from .. import ui
        ui.print_sub_agent_end(agent_type, description)

    def retry(self, attempt: int, max_retries: int, reason: str) -> None:
        from .. import ui
        ui.print_retry(attempt, max_retries, reason)


class NullSink:
    """全 no-op sink：core 在无表现层时（测试 / headless / fake turn）使用。"""

    def assistant_markdown(self, text: str) -> None: ...
    def thinking(self, text: str) -> None: ...
    def spinner_start(self, label: str = "Thinking") -> None: ...
    def spinner_stop(self) -> None: ...
    def tool_call(self, name: str, inp: dict) -> None: ...
    def tool_result(self, name: str, result: str) -> None: ...
    def cost(self, input_tokens: int, output_tokens: int) -> None: ...
    def info(self, message: str) -> None: ...
    def confirmation(self, command: str) -> None: ...
    def sub_agent_start(self, agent_type: str, description: str) -> None: ...
    def sub_agent_end(self, agent_type: str, description: str) -> None: ...
    def retry(self, attempt: int, max_retries: int, reason: str) -> None: ...


class BufferSink(NullSink):
    """子 agent sink：捕获助手 markdown 文本到 buffer，其余 UI 事件抑制。

    复刻解耦前 _output_buffer 的语义——子 agent 的最终文本经 ``.text()`` 取回（供
    run_once / 前台终结路径），而 tool_call/tool_result/spinner/cost 等不打到父 console。
    """

    def __init__(self) -> None:
        self._chunks: list[str] = []

    def assistant_markdown(self, text: str) -> None:
        self._chunks.append(text)

    def text(self) -> str:
        return "".join(self._chunks)

    def reset(self) -> None:
        """清空捕获——每次 run_once 入口调用，复刻旧 `_output_buffer = []` 的每轮重置，
        使复用的（持久/resume）子 agent 实例不把上一轮文本泄漏进本轮结果。"""
        self._chunks = []


class TeeSink:
    """把每个事件分发给多个 sink（如 TerminalSink 显示 + BufferSink 捕获）。

    P4 用于主线程 TurnResult.final_response：不回归 TerminalSink 打印的前提下捕获助手文本。
    显式列出全部 EventSink 方法（不用 __getattr__ 兜底），避免 hasattr/isinstance 误命中。
    """

    def __init__(self, *sinks) -> None:
        self._sinks = sinks

    def assistant_markdown(self, text: str) -> None:
        for s in self._sinks: s.assistant_markdown(text)

    def thinking(self, text: str) -> None:
        for s in self._sinks: s.thinking(text)

    def spinner_start(self, label: str = "Thinking") -> None:
        for s in self._sinks: s.spinner_start(label)

    def spinner_stop(self) -> None:
        for s in self._sinks: s.spinner_stop()

    def tool_call(self, name: str, inp: dict) -> None:
        for s in self._sinks: s.tool_call(name, inp)

    def tool_result(self, name: str, result: str) -> None:
        for s in self._sinks: s.tool_result(name, result)

    def cost(self, input_tokens: int, output_tokens: int) -> None:
        for s in self._sinks: s.cost(input_tokens, output_tokens)

    def info(self, message: str) -> None:
        for s in self._sinks: s.info(message)

    def confirmation(self, command: str) -> None:
        for s in self._sinks: s.confirmation(command)

    def sub_agent_start(self, agent_type: str, description: str) -> None:
        for s in self._sinks: s.sub_agent_start(agent_type, description)

    def sub_agent_end(self, agent_type: str, description: str) -> None:
        for s in self._sinks: s.sub_agent_end(agent_type, description)

    def retry(self, attempt: int, max_retries: int, reason: str) -> None:
        for s in self._sinks: s.retry(attempt, max_retries, reason)
