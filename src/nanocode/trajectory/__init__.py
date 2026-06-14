"""nanocode.trajectory — trajectory（轨迹）DERIVED 投影层。

本包是对 merged wire 事件的**只读派生投影**，用于分析 / 复盘 / agentic-RL dataset 导出。

硬边界（用户强制，不可违反）：
- trajectory 是 DERIVED 投影：它**只读** merged wire，绝不写回 wire。
- trajectory **绝不**驱动 runtime，**绝不**参与 resume / fork。
- 任何 runtime 模块（agent/engine.py、agent/anthropic_backend.py、agent/openai_backend.py、
  agent/context_builder.py、session/agent.py、trace/tracer.py、trace/redaction.py）
  **绝不**得 import nanocode.trajectory。
- metrics / evals 是派生标签（reward / eval_result），只落 metrics.json / evals.jsonl，
  绝不污染 wire。

公开读侧 API（全部只读、绝不驱动 runtime）：
- ``project_session`` / ``build_steps`` —— wire 事件 -> Step 投影。
- ``compute_metrics`` —— harness 指标聚合。
- ``online_evals`` —— 在线启发式 eval / reward 信号（派生标签）。
- ``export_bundle`` / ``bundle_dir`` —— 把一个 session 导出为 trajectory bundle。
- ``Step`` / ``TrajectoryMetadata`` —— 纯数据 schema。
"""
from __future__ import annotations

from .config import trajectory_enabled, trajectory_level
from .eval import online_evals
from .export import bundle_dir, export_bundle
from .metrics import compute_metrics
from .project import build_steps, project_session
from .schema import Step, TrajectoryMetadata

__all__ = [
    "project_session",
    "build_steps",
    "compute_metrics",
    "online_evals",
    "export_bundle",
    "bundle_dir",
    "Step",
    "TrajectoryMetadata",
    "trajectory_enabled",
    "trajectory_level",
]
