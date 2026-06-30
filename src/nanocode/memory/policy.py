"""memory/policy.py — memory governance (docs/20 §5/§7 Phase 5).

Separates *using* long-term memory (read/recall/static-prompt/tools) from
*generating* it (rollout → extraction → consolidation), and tracks per-thread
pollution so external context never silently seeds long-term memory.

The policy object is owned by `MemoryService` (host-side). `AgentCore` never
sees it; the only way it influences the model is via the service deciding
whether to surface a static prompt / prefetch / expose tools.
"""
from __future__ import annotations

from dataclasses import dataclass

# thread memory mode
ENABLED = "enabled"
DISABLED = "disabled"
POLLUTED = "polluted"

# External-context sources that pollute a thread for the purpose of *generation*
# (read/use is unaffected). Mirrors Codex `disable_on_external_context`.
EXTERNAL_CONTEXT_SOURCES = frozenset({
    "web_fetch", "web_search", "tool_search", "mcp", "deferred_tool",
})


@dataclass
class MemoryPolicy:
    """Per-thread memory governance state.

    `use_memories` / `generate_memories` are static capability switches
    (config). `mode` is dynamic per-thread state advanced at runtime when the
    thread consumes external context.
    """

    use_memories: bool = True
    generate_memories: bool = True
    disable_on_external_context: bool = True
    mode: str = ENABLED

    # ── read/use side ────────────────────────────────────────────────
    @property
    def allows_use(self) -> bool:
        """Static prompt + prefetch + dedicated tools are gated on this."""
        return self.use_memories

    # ── generation side ──────────────────────────────────────────────
    @property
    def allows_generation(self) -> bool:
        """Background rollout→memory generation is gated on this.

        Blocked by: generate_memories=False, a disabled thread, or a polluted
        thread (when disable_on_external_context is on)."""
        if not self.generate_memories:
            return False
        if self.mode == DISABLED:
            return False
        if self.mode == POLLUTED and self.disable_on_external_context:
            return False
        return True

    def mark_external_context(self, source: str) -> bool:
        """Advance the thread to polluted when an external-context source is
        consumed. Returns True if the mode actually transitioned.

        A `disabled` thread stays disabled (it's already non-generating); a
        thread becomes polluted only when external pollution would otherwise
        change the generation outcome."""
        if not self.disable_on_external_context:
            return False
        if source not in EXTERNAL_CONTEXT_SOURCES:
            return False
        if self.mode == ENABLED:
            self.mode = POLLUTED
            return True
        return False

    def reset_thread_mode(self) -> None:
        """Reset dynamic per-thread pollution (new/resumed session boundary).

        Static capability switches (use/generate/disable_on_external_context)
        are config and survive; only the runtime mode resets to enabled."""
        self.mode = ENABLED

    def snapshot(self) -> dict:
        return {
            "use_memories": self.use_memories,
            "generate_memories": self.generate_memories,
            "disable_on_external_context": self.disable_on_external_context,
            "mode": self.mode,
        }
