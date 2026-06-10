"""trajectory.schema — Step / TrajectoryMetadata 的纯数据 schema（docs/10）。

PURE 模块：无 nanocode import。供 Foundation 之后的 projection / export 消费。

Step 是「读侧投影概念」：一条可训练/可复盘的关键动作，投影为
``observation -> action -> result -> next_state -> reward``。盘上 wire 仍是 flat-additive
事件；本 schema 描述的是 trajectory 导出（steps.jsonl）的逻辑形态。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 可投影为 step 的关键动作类型（docs/10 step_type 的稳定子集）。
STEP_TYPES = frozenset({
    "llm_decision",
    "tool_action",
    "approval",
    "compaction",
    "final",
})

# 风险分级（docs/10 risk_level）。
RISK_LEVELS = frozenset({"low", "medium", "high"})


def step_id(agent_id: str, seq: int) -> str:
    """确定性 step id：``step_{agent_id}_{seq}``（与 event_id 同构，便于关联 wire seq）。"""
    return f"step_{agent_id}_{seq}"


@dataclass
class Step:
    """一条 trajectory step（steps.jsonl 的一行的逻辑视图）。

    字段对应 docs/10 step-level schema：稳定表达「agent 看到什么 / 做了什么 / 环境返回什么 /
    状态如何变 / 成本与风险 / 结果好不好」。reward / eval_result 是派生标签，第一阶段可为空。
    """

    trajectory_id: str
    episode_id: str  # = session_id
    step_id: str
    parent_step_id: "str | None"
    turn_id: "str | None"
    agent_id: str
    step_type: str
    branch_id: str = "main"  # fork 分支身份；投影按 (agent_id, branch_id) 串 parent 链
    observation_summary: str = ""
    action: dict = field(default_factory=dict)  # {"type": "tool_call", "tool": "...", "args_summary": "..."}
    result_summary: str = ""
    next_state_summary: str = ""
    latency_ms: "int | None" = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    risk_level: str = "low"  # "low" | "medium" | "high"
    eval_result: "dict | None" = None
    reward: "float | None" = None
    done: bool = False

    def to_record(self) -> dict:
        """投影为 steps.jsonl 的一行 dict（docs/10 示例形态）。

        保留扁平字段（trajectory_id / episode_id / step_id / observation / action / result /
        next_state_summary / reward / done），把成本聚合到 ``cost`` 子 dict
        ``{"tokens": in+out, "latency_ms": ...}``，并归集 ``metadata``
        ``{"agent_id", "turn_id", "risk_level"}``。
        """
        return {
            "trajectory_id": self.trajectory_id,
            "episode_id": self.episode_id,
            "step_id": self.step_id,
            "parent_step_id": self.parent_step_id,
            "step_type": self.step_type,
            "observation": self.observation_summary,
            "action": dict(self.action),
            "result": self.result_summary,
            "next_state_summary": self.next_state_summary,
            "reward": self.reward,
            "done": self.done,
            "eval_result": self.eval_result,
            "cost": {
                "tokens": self.input_tokens + self.output_tokens,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cost": self.cost,
                "latency_ms": self.latency_ms,
            },
            "metadata": {
                "agent_id": self.agent_id,
                "branch_id": self.branch_id,
                "turn_id": self.turn_id,
                "risk_level": self.risk_level,
            },
        }


@dataclass
class TrajectoryMetadata:
    """一次 trajectory 的元数据（metadata.json 的逻辑视图，docs/10 trajectory-level 字段子集）。"""

    trajectory_id: str
    episode_id: str  # = session_id
    model: "str | None" = None
    start_time: "str | None" = None
    end_time: "str | None" = None
    final_status: "str | None" = None  # completed | failed | cancelled | timeout
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost: float = 0.0
    n_steps: int = 0
