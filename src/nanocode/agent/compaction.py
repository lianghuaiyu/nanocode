"""大结果落盘（per-tool-result 封顶）。

当工具结果超过阈值时写入磁盘，并把上下文里的条目替换为简短预览 + 文件路径；
模型可随后用 read_file 取回完整输出，不丢失信息。每条 tool result 都经此封顶
（两 backend 在 _persist_large_result 调用），故单条大输出不会撑爆上下文。

docs/16 #8：shell 类工具的预览 **tail-keep**（pi truncateTail 同义）——失败命令的
报错/堆栈在输出**尾部**，头部预览会恰好裁掉最关键的部分；其余工具保持头部预览
（文件/搜索类输出头部信息密度更高）。

docs/13 cutover S3：原 snip/microcompact 多层 in-place 裁剪 tier（CompressionPipeline）已删除——
树为唯一会话存储后，累积裁剪由 summary-compaction-as-entry 接管；单条大结果仍由此函数封顶。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..paths import tool_results_dir


# ─── before_compact 可拔插钩子契约（docs/26 G4，对位 pi session_before_compact）─────────
# 中性层 ②b：session/（产摘要的插入点）与 extensions/（策略实现）都可下行 import 这两个类型，
# 而 session/ 永不 import extensions/——钩子只经注入的 callable（agent._compaction_strategy）调用。

@dataclass(frozen=True)
class CompactionRequest:
    """喂给 before_compact 策略的 **curated** 压缩输入（docs/26 G4）。

    只含中性投影 + 标量——绝不递 raw SessionManager / Agent / tree Entry（嵌入边界不变量）：
    - messages：cut 之前 prefix 的 provider-shaped 投影（summarizer 看到的 = 模型曾看到的前缀）。
    - tokens_before：压缩前 last_input_token_count。
    - trigger：manual | auto | overflow_retry（与内置 details.trigger 同词汇）。
    - instructions：`/compact [prompt]` 的自定义摘要指令（无则 None）。
    - file_ops：{"read": [...], "modified": [...]}（宿主观测 + 树上 toolCall 累计的读/改文件）。
    - previous_summary：上一代 compaction 的 summary（当前恒 None，预留多代上下文）。"""
    messages: list = field(default_factory=list)
    tokens_before: int = 0
    trigger: str = "manual"
    instructions: "str | None" = None
    file_ops: dict = field(default_factory=dict)
    previous_summary: "str | None" = None


@dataclass(frozen=True)
class CompactionOutcome:
    """before_compact 策略的产出（docs/26 G4）。

    - summary 非空 → 用它当本次 compaction 摘要（内核仍走 format/record_event/cut/fold/restore）。
    - summary is None 且 cancel=False → **弃权**：回退内置 summarizer。
    - cancel=True → **取消**本次压缩：内核早退，不写 COMPACTION entry、对话不收缩。"""
    summary: "str | None" = None
    cancel: bool = False


# 预览 tail-keep 的工具：输出尾部承载终态（exit code 前的报错/堆栈/失败断言）。
_TAIL_KEEP_TOOLS = frozenset({"run_shell"})


def persist_large_result(tool_name: str, result: str) -> str:
    THRESHOLD = 30 * 1024  # 30 KB
    if len(result.encode()) <= THRESHOLD:
        return result
    d = tool_results_dir()
    filename = f"{int(time.time() * 1000)}-{tool_name}.txt"
    filepath = d / filename
    filepath.write_text(result, encoding="utf-8")

    lines = result.split("\n")
    size_kb = len(result.encode()) / 1024

    if tool_name in _TAIL_KEEP_TOOLS:
        preview_label = "last 200 lines"
        preview = "\n".join(lines[-200:])
    else:
        preview_label = "first 200 lines"
        preview = "\n".join(lines[:200])

    return (
        f"[Result too large ({size_kb:.1f} KB, {len(lines)} lines). "
        f"Full output saved to {filepath}. "
        f"You can use read_file to see the full result.]\n\n"
        f"Preview ({preview_label}):\n{preview}"
    )
