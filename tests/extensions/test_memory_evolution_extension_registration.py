"""docs/22 Phase 0 §7: the /memory optimize and /memory eval generate commands
are owned by the memory-evolution system extension and merge into the builtin
command registry (no dual builtin entry point)."""
from nanocode.entrypoints.commands.builtin import build_registry, _BUILTINS


def test_memory_optimize_not_a_builtin_handler():
    names = {b[0] for b in _BUILTINS}
    assert "/memory optimize" not in names
    assert "/memory eval generate" not in names


def test_merged_registry_resolves_extension_commands():
    r = build_registry()
    assert r.lookup("/memory optimize").spec.name == "/memory optimize"
    assert r.lookup("/memory optimize --diagnose").spec.name == "/memory optimize"
    assert r.lookup("/memory eval generate").spec.name == "/memory eval generate"
    # most-specific-first must still hold for the eval family
    assert r.lookup("/memory eval pending").spec.name == "/memory eval"
    assert r.lookup("/memory").spec.name == "/memory"


def test_extension_command_source_is_builtin_merged():
    r = build_registry()
    spec = r.lookup("/memory optimize").spec
    assert spec.description.startswith("Run host-owned retrieval optimization")
