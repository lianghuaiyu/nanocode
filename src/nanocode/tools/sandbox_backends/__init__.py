"""本机 OS 沙盒后端包。当前实现：seatbelt（macOS）、bwrap（Linux）。

提供单一真相源的后端解析器 `resolve_native_backend()`：`SandboxManager`（docs/19，native-first /
VM-on-demand 唯一规划点）经它选后端，避免各入口各写一份选择逻辑。

周期安全：本包只 import 自己的 seatbelt/bwrap/base，**不** import
capabilities/run_shell/permissions/execute；resolve_native_backend 内部惰性 import 子模块。
"""

from __future__ import annotations

import sys


def resolve_native_backend():
    """darwin→seatbelt（若 sandbox-exec 在）；linux→bwrap（若 bwrap 在）；否则 None。"""
    from . import seatbelt, bwrap

    if sys.platform == "darwin" and seatbelt.is_available():
        return seatbelt
    if sys.platform.startswith("linux") and bwrap.is_available():
        return bwrap
    return None
