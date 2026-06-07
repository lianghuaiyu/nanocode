"""上下文压缩相关常量与大结果落盘。

当工具结果超过阈值时写入磁盘，并把上下文里的条目替换为简短预览 + 文件路径；
模型可随后用 read_file 取回完整输出，不丢失信息。"""

from __future__ import annotations

import time

from ..paths import tool_results_dir

SNIPPABLE_TOOLS = {"read_file", "grep_search", "list_files", "run_shell", "sandbox_shell"}
SNIP_PLACEHOLDER = "[Content snipped - re-read if needed]"
SNIP_THRESHOLD = 0.60
MICROCOMPACT_IDLE_S = 5 * 60  # 5 minutes
KEEP_RECENT_RESULTS = 3


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
