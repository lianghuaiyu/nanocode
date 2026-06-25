"""memory-evolution system extension (docs/22 §5.0.2).

EvolveMem's correct identity in nanocode: a controlled *system* extension, not a
`SimpleMemEngine` internal module, not a user-overridable plugin, not a single
sub-agent. It owns the optimizer orchestration, reports, agent prompts, and model
routing; it proposes retrieval configs but never writes the session tree, never
bypasses the CapabilityRouter / TaskManager / MemoryService, and never makes the
turn hot path call an LLM.
"""
