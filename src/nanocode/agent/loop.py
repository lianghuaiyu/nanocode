"""agent/loop.py — provider-independent 循环辅助（docs/15 §5）。

把两条后端循环里可纯函数化的小块抽出来（OpenAI 的 serial-check → parallel-batch 分组）,使
AgentCore 的循环更薄、可单测。纯函数,无 I/O、无 self。
"""

from __future__ import annotations

from ..tools import CONCURRENCY_SAFE_TOOLS


def group_openai_batches(checked: list[dict]) -> list[dict]:
    """OpenAI Phase 2 grouping：把 serial-checked 的 tool calls 分组——连续的 allowed +
    concurrency-safe 工具并成一个并行 batch,其余各自成串行 batch（移植自 openai loop,行为逐字一致）。

    入参 checked：[{tc, fn, inp, allowed, result?}, ...]（serial 权限判定后的结果）。
    返回：[{concurrent: bool, items: [...]}, ...]。
    """
    batches: list[dict] = []
    for ct in checked:
        safe = ct["allowed"] and ct["fn"] in CONCURRENCY_SAFE_TOOLS
        if safe and batches and batches[-1]["concurrent"]:
            batches[-1]["items"].append(ct)
        else:
            batches.append({"concurrent": safe, "items": [ct]})
    return batches
