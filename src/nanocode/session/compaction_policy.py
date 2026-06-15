"""session/compaction_policy.py — auto-compaction 阈值与失败熔断策略（docs/18 Phase 1）。

替换粗糙的 `0.85 * effective_window` 阈值，对齐 Claude Code autoCompact 的 buffer 语义：

- ``summary_output_reserve``：为 summarizer 自身的输出预留（= ``min(model_max_output_tokens, 20_000)``）。
- ``auto_threshold``：自动压缩触发线 = ``effective_window - summary_output_reserve - auto_buffer``。
- ``manual_blocking_limit``：手动 /compact 的硬上限 = ``effective_window - manual_buffer``。
- ``keep_recent_tokens``：compaction 保留的近期 suffix 预算。

本模块是**纯数据 + 纯属性**，刻意不 import 任何 agent/SDK 符号——session 层可直接构造。
``model_max_output_tokens`` 由调用方（AgentSession）经 ``models._get_max_output_tokens(model)`` 提供，
保持本模块 import-light、可独立单测。

注意（reserve 复合）：``effective_window`` 在 ``Agent.__init__`` 已是 ``context_window - 20_000``
（engine.py），即原始窗口先扣了一层 20k 安全垫；``auto_threshold`` 在其上再扣 ``summary_output_reserve``
（≤20k）+ ``auto_buffer``（13k）。这是有意为之——summarizer 调用本身要占输出预算，故触发线比朴素
``0.85 * window`` 略保守、更早压缩，避免压缩请求自己 prompt-too-long。
"""

from __future__ import annotations

from dataclasses import dataclass

SUMMARY_OUTPUT_RESERVE_CAP = 20_000


@dataclass(frozen=True)
class CompactionPolicy:
    """一次会话的 compaction 阈值与熔断参数（不可变；按 effective_window + 模型最大输出构造）。"""

    effective_window: int
    summary_output_reserve: int
    auto_buffer_tokens: int = 13_000
    warning_buffer_tokens: int = 20_000
    error_buffer_tokens: int = 20_000
    manual_buffer_tokens: int = 3_000
    max_consecutive_failures: int = 3

    @property
    def auto_threshold(self) -> int:
        """自动压缩触发线：``last_input_token_count`` 超过即触发 auto compact。"""
        return self.effective_window - self.summary_output_reserve - self.auto_buffer_tokens

    @property
    def manual_blocking_limit(self) -> int:
        """手动 /compact 的硬上限（超过则压缩请求自身可能 prompt-too-long，靠 Phase 3 retry 兜底）。"""
        return self.effective_window - self.manual_buffer_tokens

    def keep_recent_tokens(self) -> int:
        """compaction 保留的近期 suffix 预算：有效窗口的 15%，下限 4k、上限 20k。"""
        return min(20_000, max(4_000, int(self.effective_window * 0.15)))

    @classmethod
    def for_model(cls, effective_window: int, model_max_output_tokens: int,
                  **overrides) -> "CompactionPolicy":
        """从有效窗口 + 模型最大输出 token 构造。

        ``summary_output_reserve = min(model_max_output_tokens, 20_000)``——模型无最大输出元数据时，
        调用方（models._get_max_output_tokens）已返回保守默认（16384），无需在此再设默认。
        """
        return cls(effective_window=effective_window,
                   summary_output_reserve=min(model_max_output_tokens, SUMMARY_OUTPUT_RESERVE_CAP),
                   **overrides)
