"""大结果落盘（per-tool-result 封顶）。

当工具结果超过阈值时写入磁盘，并把上下文里的条目替换为简短预览 + 文件路径；
模型可随后用 read_file 取回完整输出，不丢失信息。每条 tool result 都经此封顶
（两 backend 在 _persist_large_result 调用），故单条大输出不会撑爆上下文。

docs/13 cutover S3：原 snip/microcompact 多层 in-place 裁剪 tier（CompressionPipeline）已删除——
树为唯一会话存储后，累积裁剪由 summary-compaction-as-entry 接管；单条大结果仍由此函数封顶。
"""

from __future__ import annotations

import time

from ..paths import tool_results_dir


def persist_large_result(tool_name: str, result: str) -> str:
    THRESHOLD = 30 * 1024  # 30 KB
    if len(result.encode()) <= THRESHOLD:
        return result
    d = tool_results_dir()
    filename = f"{int(time.time() * 1000)}-{tool_name}.txt"
    filepath = d / filename
    filepath.write_text(result, encoding="utf-8")

    lines = result.split("\n")
    preview = "\n".join(lines[:200])
    size_kb = len(result.encode()) / 1024

    return (
        f"[Result too large ({size_kb:.1f} KB, {len(lines)} lines). "
        f"Full output saved to {filepath}. "
        f"You can use read_file to see the full result.]\n\n"
        f"Preview (first 200 lines):\n{preview}"
    )
