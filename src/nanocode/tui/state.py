"""tui/state.py —— TUI ViewModel（docs/18）。

不存 ANSI 字符串，存结构化视图模型——可单测、与渲染解耦。reconciled 计划下（`full_screen=False`、
保留终端 scrollback），`timeline` **不是 v1 正文渲染源**（正文走 scrollback），而用于：
① session 切换时 re-hydrate；② 驱动 footer 的 active-tools/plan 摘要；③ 给后续 sidebar 备料；
④ reducer 单测的断言面。footer 实时状态读 `StatusSnapshot`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Union

# ─── Timeline items ────────────────────────────────────────


@dataclass
class UserItem:
    text: str
    kind: str = "user"


@dataclass
class AssistantItem:
    text: str = ""
    complete: bool = False
    kind: str = "assistant"


@dataclass
class ThinkingItem:
    text: str = ""
    complete: bool = False
    kind: str = "thinking"


@dataclass
class ToolItem:
    """一次工具调用的视图模型，按 tool_use_id 关联请求→结果。

    领域文案（name→标题、result_summary、diff 解析）由客户端 `entrypoints/render.py` 填充——
    本结构只持中立事实。"""

    id: str
    name: str
    input: dict = field(default_factory=dict)
    status: Literal["running", "done", "error", "denied"] = "running"
    result_summary: str = ""
    result_excerpt: str = ""
    chars: int = 0
    latency_ms: int | None = None
    kind: str = "tool"


@dataclass
class NoticeItem:
    text: str
    level: str = "info"  # info | warn | retry
    kind: str = "notice"


@dataclass
class ErrorItem:
    text: str
    kind: str = "error"


@dataclass
class SubAgentItem:
    agent_type: str
    description: str
    status: Literal["running", "done"] = "running"
    kind: str = "sub_agent"


@dataclass
class SessionBoundaryItem:
    """session 切换边界（/new /resume /clone /fork 后插入，标记新会话起点）。"""

    session_id: str
    label: str = ""
    kind: str = "session_boundary"


TimelineItem = Union[
    UserItem,
    AssistantItem,
    ThinkingItem,
    ToolItem,
    NoticeItem,
    ErrorItem,
    SubAgentItem,
    SessionBoundaryItem,
]


# ─── Status + modal ────────────────────────────────────────


@dataclass
class StatusSnapshot:
    """footer / 状态栏的只读快照——对位 `RuntimeThread.status()`（+ `state()` 的 is_processing）。"""

    session_id: str = ""
    session_name: str | None = None
    cwd: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    context_window: int = 0
    thinking: str | None = None
    is_processing: bool = False

    @classmethod
    def from_status(cls, d: dict) -> "StatusSnapshot":
        """从 `RuntimeThread.status()` / `state()` 的 dict 构造（多取的 is_processing 容缺省）。"""
        return cls(
            session_id=d.get("session_id", ""),
            session_name=d.get("session_name"),
            cwd=d.get("cwd", ""),
            model=d.get("model", ""),
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            cost_usd=d.get("cost_usd"),
            context_window=d.get("context_window", 0),
            thinking=d.get("thinking"),
            is_processing=d.get("is_processing", False),
        )


@dataclass
class ApprovalModal:
    """危险动作待审批的 modal 状态（对位 `ApprovalRequested` 事件；决策仍经注入的 confirm_fn 往返）。"""

    command: str
    message: str
    request_id: str = ""


@dataclass
class PlanModal:
    """plan 审批 modal（对位 plan_approval_fn）：展示计划 + 1-4 选项。

    1 clear+execute / 2 execute keep / 3 manual-execute / 4 keep-planning。feedback（选项 4）
    经文本输入回填属后续步骤——step 3 先返回无 feedback。"""

    plan_content: str


@dataclass
class SelectorState:
    """in-app 选择器 overlay 状态（docs/18：/resume /tree /fork 从独立 Application 改成 app 内区域）。

    model 是 owner 的 SelectorModel（tui/selector.py 协议）；index 为光标位。
    渲染（标题/列表/预览/提示）与按键路由在 TuiApp，结果经 future 回 owner。"""

    model: Any
    index: int = 0


Mode = Literal["idle", "running", "approval", "plan_approval", "selector", "ask_text", "error"]


@dataclass
class TuiState:
    """TUI 的完整视图模型。reducer 据订阅事件流增量更新。"""

    status: StatusSnapshot = field(default_factory=StatusSnapshot)
    title: str = ""
    timeline: list = field(default_factory=list)
    # 进行中的工具，按 tool_use_id 索引（结果观测到即出表；timeline 内仍保留该 ToolItem）。
    active_tools: dict = field(default_factory=dict)
    mode: Mode = "idle"
    modal: ApprovalModal | None = None
    plan_modal: PlanModal | None = None
    selector: "SelectorState | None" = None    # /resume /tree /fork in-app overlay
    text_prompt: str | None = None             # ask_text（rename/label）的提示串
    notice: str | None = None
