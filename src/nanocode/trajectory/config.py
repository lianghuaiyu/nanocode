"""trajectory 采集开关与级别（CLI / env 门控）。

纯 env/flag 读取，无 runtime 依赖（import nanocode.trajectory 不得连带拉起 runtime——见
tests/trajectory/test_boundaries.py）。trajectory 是 canonical 树的 DERIVED 投影（Milestone B2）；
这两个开关 gate `nanocode trajectory export` 与 Agent 的 trajectory_enabled/level plain attrs。
"""
from __future__ import annotations

import os

_TRUE = {"1", "true", "yes", "on"}
_VALID_TRAJECTORY_LEVELS = {"summary", "full"}


def trajectory_enabled(flag: bool = False) -> bool:
    """trajectory 采集是否开启：显式 flag 优先，否则看 NANOCODE_TRAJECTORY 环境变量。"""
    if flag:
        return True
    return os.environ.get("NANOCODE_TRAJECTORY", "").strip().lower() in _TRUE


def trajectory_level(value: "str | None" = None) -> str:
    """trajectory 采集级别：显式 value 优先，否则看 NANOCODE_TRAJECTORY_LEVEL 环境变量；
    非法/缺省 → "summary"（保守默认，不写完整 payload）。"""
    raw = (value or os.environ.get("NANOCODE_TRAJECTORY_LEVEL", "")).strip().lower()
    return raw if raw in _VALID_TRAJECTORY_LEVELS else "summary"
