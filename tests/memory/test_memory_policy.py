from nanocode.memory.policy import (
    MemoryPolicy, ENABLED, DISABLED, POLLUTED,
)


def test_use_gate():
    assert MemoryPolicy(use_memories=True).allows_use
    assert not MemoryPolicy(use_memories=False).allows_use


def test_generate_gate():
    assert MemoryPolicy(generate_memories=True).allows_generation
    assert not MemoryPolicy(generate_memories=False).allows_generation


def test_disabled_thread_blocks_generation():
    p = MemoryPolicy(mode=DISABLED)
    assert not p.allows_generation


def test_external_context_marks_polluted_and_blocks_generation():
    p = MemoryPolicy()
    assert p.mode == ENABLED and p.allows_generation
    assert p.mark_external_context("web_fetch") is True
    assert p.mode == POLLUTED
    assert not p.allows_generation


def test_non_external_source_does_not_pollute():
    p = MemoryPolicy()
    assert p.mark_external_context("read_file") is False
    assert p.mode == ENABLED


def test_external_guard_off_keeps_generating():
    p = MemoryPolicy(disable_on_external_context=False)
    assert p.mark_external_context("mcp") is False
    assert p.mode == ENABLED and p.allows_generation


def test_polluted_still_allows_use():
    p = MemoryPolicy()
    p.mark_external_context("web_search")
    assert p.allows_use  # read/use is never blocked by pollution


def test_reset_thread_mode():
    p = MemoryPolicy()
    p.mark_external_context("tool_search")
    assert p.mode == POLLUTED
    p.reset_thread_mode()
    assert p.mode == ENABLED and p.allows_generation
