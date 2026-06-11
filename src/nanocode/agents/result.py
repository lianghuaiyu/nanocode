"""agents/result.py — typed ResultEnvelope（docs/15 §11.1）。

把 agent/agent_result.py 的 dict 信封形式化成 typed dataclass：host-derived files_read/modified
（绝不信任模型自述）+ 解析的 summary/findings + tokens + result_path + childSessionId + status/error。
有界 render（<=4KB 直通,否则截断 + 指针）。由 runtime spawn（Phase 6）拥有,**绝不**把 child transcript
折进父 —— 父 branch 只存这个有界信封 + child session id（§11.1）。

复用既有纯函数（build_agent_result / render_agent_result_envelope）,不重实现内容计算。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..agent.agent_result import build_agent_result, render_agent_result_envelope


@dataclass
class ResultEnvelope:
    """子 agent / child 线程的有界结构化结果（§11.1）。父 branch 只存它,不存 transcript。"""

    summary: str = ""
    findings: list[str] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    tokens: dict = field(default_factory=dict)
    result_path: str | None = None
    child_session_id: str | None = None
    status: str = "completed"        # completed | failed | timed_out | cancelled
    error: str | None = None

    @classmethod
    def from_result_dict(cls, d: dict, *, status: str = "completed",
                         error: str | None = None) -> "ResultEnvelope":
        return cls(
            summary=d.get("summary", ""),
            findings=list(d.get("findings") or []),
            files_read=list(d.get("files_read") or []),
            files_modified=list(d.get("files_modified") or []),
            tokens=dict(d.get("tokens") or {}),
            result_path=d.get("result_path"),
            child_session_id=d.get("childSessionId"),
            status=status,
            error=error,
        )

    @classmethod
    def build(cls, sub_agent, text: str, tokens: dict, result_path: str | None,
              *, status: str = "completed", error: str | None = None) -> "ResultEnvelope":
        """从 sub_agent 观测 + 文本装配（host-derived files,不信任模型自述）。复用 build_agent_result。"""
        d = build_agent_result(sub_agent, text, tokens, result_path)
        return cls.from_result_dict(d, status=status, error=error)

    def to_result_dict(self) -> dict:
        """转回 agent_result dict 形态（供 render_agent_result_envelope / 既有调用方）。"""
        return {
            "summary": self.summary,
            "findings": list(self.findings),
            "files_read": list(self.files_read),
            "files_modified": list(self.files_modified),
            "tokens": dict(self.tokens),
            "result_path": self.result_path,
            "childSessionId": self.child_session_id,
        }

    def render(self, raw_text: str = "") -> str:
        """渲染父上下文看到的有界信封（<=4KB 直通,否则截断 + result_path 指针）。"""
        return render_agent_result_envelope(self.to_result_dict(), raw_text)
