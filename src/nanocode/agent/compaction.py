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

from ..paths import tool_results_dir

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
