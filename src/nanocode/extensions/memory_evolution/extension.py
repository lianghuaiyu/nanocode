"""memory_evolution/extension.py — activation factory (docs/22 §5.0.1).

`activate(api)` only registers contributions; it must not access MemoryService,
read env, or start background work. Real host work happens at task/command run
time with a fresh call-time context.
"""
from __future__ import annotations

from ..api import ExtensionAPI
from ..registry import HiddenAgentProfile, ModelRolePolicy
from .commands import run_memory_eval_generate_command, run_memory_optimize_command
from .manifest import MEMORY_DIAGNOSIS_ROLE, MEMORY_DIAGNOSTICIAN_TYPE
from .manifest import MANIFEST
from .tasks import run_memory_optimize_task


def activate(api: ExtensionAPI) -> None:
    commands = {c.name: c for c in MANIFEST.contributes.commands}
    api.register_command(commands["/memory optimize"], run_memory_optimize_command)
    api.register_command(commands["/memory eval generate"], run_memory_eval_generate_command)
    api.register_task_kind("memory_optimize", run_memory_optimize_task)
    # docs/22 §6: reserved hidden diagnosis agent + its model role. The agent
    # itself is a reserved type in agents/registry.py (tools=[], max_turns=1,
    # background, not model-spawnable, not project-overridable).
    api.register_model_role(
        MEMORY_DIAGNOSIS_ROLE,
        ModelRolePolicy(default="host", env_var="NANOCODE_MEMORY_EVOLVE_DIAG_MODEL"))
    api.register_hidden_agent(
        HiddenAgentProfile(agent_type=MEMORY_DIAGNOSTICIAN_TYPE,
                           description="Read-only retrieval failure diagnostician",
                           model_role=MEMORY_DIAGNOSIS_ROLE))
