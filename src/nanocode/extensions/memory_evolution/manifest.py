"""memory_evolution/manifest.py — built-in system extension manifest (docs/22 §5.0.2)."""
from __future__ import annotations

from ..manifest import (
    CommandContribution, ExtensionContributes, ExtensionManifest,
)

# Reserved hidden agent type registered by this extension (docs/22 §6).
MEMORY_DIAGNOSTICIAN_TYPE = "memory-retrieval-diagnostician"

# Extension model role driving the diagnostician (docs/22 §5.4).
MEMORY_DIAGNOSIS_ROLE = "memory_diagnosis"

MANIFEST = ExtensionManifest(
    id="nanocode.memory_evolution",
    kind="system",
    entrypoint="nanocode.extensions.memory_evolution.extension:activate",
    contributes=ExtensionContributes(
        commands=(
            CommandContribution(
                "/memory optimize", match="exact_or_prefix",
                description="Run host-owned retrieval optimization on confirmed memory eval candidates",
                arg_hint="[--diagnose]"),
            CommandContribution(
                "/memory eval generate", match="exact",
                description="Run an EVAL-mode curator pass to propose pending eval candidates"),
        ),
        task_kinds=("memory_optimize",),
        hidden_agents=(MEMORY_DIAGNOSTICIAN_TYPE,),
        lifecycle_events=(),
        model_roles=(MEMORY_DIAGNOSIS_ROLE,),
    ),
    capabilities=frozenset({
        "memory:read",
        "memory:evaluate",
        "memory:write_retrieval_config",
        "task:create",
        "model:diagnose",
    }),
)
