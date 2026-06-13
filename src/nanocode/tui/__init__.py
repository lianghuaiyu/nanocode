"""nanocode.tui —— 与 agent 无关的终端 TUI 层（docs/17 三层中的「通用渲染框架」+ docs/18 客户端化）。

历史上本层是单文件 `tui.py`（Pi `packages/tui` 对位：console / markdown / spinner / bullet /
connector / diff / 部件——零领域知识）。docs/18 起拆成 package：

- `primitives`  —— 原有的通用渲染原语（rich console、print_*、render_*、spinner、plan/approval 显示）。
                   **零领域知识**；core 不 import 本层（渲染只在订阅端 client）。
- `state`       —— TUI ViewModel（TuiState / TimelineItem / ToolItem / StatusSnapshot）。不存 ANSI，
                   存结构化视图模型，可单测。
- `reducer`     —— 把订阅信封（`{type, event}` 的 typed AgentEvent）归约进 TuiState。纯函数式，无 UI 依赖。
- `selector`    —— in-app selector owner 协议。
- `footer`      —— Pi 式 footer 纯渲染。
- `session_pages` —— Pi 式 /resume /tree /fork 页面组件。

为不破坏既有 importer（`from .. import tui; tui.print_*`、`from ..tui import set_verbose`、
`import nanocode.tui as tui`），__init__ 原样 re-export 全部 primitives。
"""

from __future__ import annotations

from .primitives import *  # noqa: F401,F403 —— 通用渲染原语（console/print_*/render_*/spinner/...）

# ViewModel + reducer（docs/18）——显式 re-export 供 `from nanocode.tui import TuiState` 等。
from .state import (  # noqa: F401
    ApprovalModal,
    AssistantItem,
    ErrorItem,
    NoticeItem,
    PlanModal,
    SessionBoundaryItem,
    StatusSnapshot,
    SubAgentItem,
    ThinkingItem,
    ToolItem,
    TuiState,
    UserItem,
)
from .reducer import reduce  # noqa: F401
