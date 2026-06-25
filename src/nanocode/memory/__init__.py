"""nanocode memory system (docs/20).

Host boundary: `MemoryService` owns the backend, policy, paths and injected
llm/embed callables. `AgentCore` never imports this package; the session tree
is the only context-injection channel.
"""
from .store import (get_memory_dir, MemoryEntry, list_memories, save_memory,
                    delete_memory, load_memory_index)
from .recall import (memory_age, memory_freshness_warning, RelevantMemory,
                     MemoryPrefetch, format_memories_for_injection)
from .models import (MemoryHit, MemoryEntryView, MemoryListResult,
                     MemoryReadResult, MemoryWriteResult)
from .policy import MemoryPolicy
from .service import MemoryService, MemoryServiceConfig, MemoryBackend
from .prompts import build_memory_prompt
from .markdown_backend import MarkdownMemoryBackend, OffMemoryBackend
from .simplemem_backend import SimpleMemBackend, create_simplemem_backend
from .maintenance import (
    parse_consolidation_plan, apply_plan, create_backup, rollback_backup,
    archive_file, prune_orphaned_evals, ConsolidationPlan, ConsolidationAction,
    ConsolidationResult, CURATOR_CONSOLIDATION_PROMPT, build_curator_user_message,
    evolve_min_confirmed, evolve_max_rounds,
)
from .eval_source import build_eval_curator_message, valid_memory_refs
from .retrieval_config_store import (
    load_retrieval_config, save_retrieval_config, rollback_retrieval_config,
)

# eval_store imports nanocode.session (host-coupled). Re-export it lazily (PEP 562)
# so importing the memory package — or the pure SimpleMem engine subpackage —
# does NOT transitively load the session layer (docs/20 §2.1 boundary hygiene).
_EVAL_EXPORTS = frozenset({
    "MemoryEvalCandidate", "add_pending", "list_pending", "list_confirmed",
    "list_rejected", "get_candidate", "confirm", "reject", "confirmed_dev_questions",
})


def __getattr__(name):
    if name in _EVAL_EXPORTS:
        from . import eval_store
        return getattr(eval_store, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
