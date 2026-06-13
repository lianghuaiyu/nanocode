"""entrypoints/interactive/selector.py — 选择器协议（docs/18：渲染已迁入 TuiApp 的 in-app overlay）。

docs/18 之前本模块还含一个独立的全屏 `Application`（`run_selector`）——在 cutover 后会与运行中的
TuiApp 嵌套，违背 Pi「overlay 挂同一 app」的模型（Pi `interactive-mode.ts` 经 `ui.showOverlay()`
把 SessionSelector/TreeSelector 作为组件叠在内容上，而非另起 app）。故独立 Application 已删，渲染移入
`tui/app.py:TuiApp.run_selector`（in-app 区域 overlay）。本模块只保留 **owner 协议**：

- `SelectorModel`：owner（session_select / tree_select）实现的钩子（标题/项/列表行/预览/提示/自定义键）。
- `KeyResult` / `Outcome`：on_key 返回与选择器退出结果。
- `WIDE_THRESHOLD` / `_terminal_width`：宽窄自适应阈值（TuiApp 渲染时复用）。

owner 经注入的 `SelectorHost`（TuiApp 实现）调 `host.run_selector(model)` / `host.ask_text(...)`，
不再依赖本模块的 Application。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Any, Literal

from prompt_toolkit.formatted_text import AnyFormattedText

WIDE_THRESHOLD = 90


@dataclass
class KeyResult:
    """owner.on_key 的返回:continue(吞键)/refresh(项变了重画)/done(选中退出)/cancel/edit(退出做文本输入)。"""
    kind: Literal["continue", "refresh", "done", "cancel", "edit"]
    result: Any = None
    edit_action: str | None = None


@dataclass
class Outcome:
    kind: Literal["done", "cancel", "edit"]
    item: Any = None
    edit_action: str | None = None
    index: int = 0          # 退出时的光标位置(供 owner 重跑选择器时恢复)


class SelectorModel:
    """owner 实现这些钩子;list_text/preview_text 收 width 以便自行截断。"""
    def title(self) -> str: return ""
    def items(self) -> list: return []
    def list_text(self, item: Any, selected: bool, width: int) -> AnyFormattedText: return ""
    def preview_text(self, item: Any, width: int) -> list[str]: return []
    def hint(self) -> str: return ""
    def on_key(self, key: str, item: Any, index: int) -> KeyResult | None: return None
    # 自定义键集合(传给 KeyBindings 动态绑定);导航/enter/退出键不必列。
    def extra_keys(self) -> tuple[str, ...]: return ()


def _terminal_width() -> int:
    try:
        return shutil.get_terminal_size((80, 24)).columns
    except Exception:
        return 80
