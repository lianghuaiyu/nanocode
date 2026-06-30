"""docs/22 Phase 0: ExtensionManifest schema + contribution validation.
docs/26 G6: untrusted declarative-only manifests (no entrypoint, mcp_servers only)."""
import pytest

from nanocode.extensions.manifest import (
    CommandContribution, ExtensionContributes, ExtensionManifest, McpServerSpec,
)


def test_manifest_minimal():
    m = ExtensionManifest(id="x", kind="system",
                          entrypoint="pkg.mod:activate")
    assert m.contributes == ExtensionContributes()
    assert m.capabilities == frozenset()


def test_manifest_rejects_unknown_kind():
    with pytest.raises(ValueError):
        ExtensionManifest(id="x", kind="project", entrypoint="pkg.mod:activate")


def test_manifest_rejects_bad_entrypoint():
    with pytest.raises(ValueError):
        ExtensionManifest(id="x", kind="system", entrypoint="pkg.mod.activate")


# ── docs/26 G6: untrusted declarative manifests ─────────────────────────────

def _untrusted(**kw):
    base = dict(id="u", kind="untrusted",
                contributes=ExtensionContributes(mcp_servers=(McpServerSpec("s", "echo"),)))
    base.update(kw)
    return ExtensionManifest(**base)


def test_untrusted_manifest_minimal_ok():
    m = _untrusted()
    assert m.kind == "untrusted" and m.entrypoint == ""
    assert m.contributes.mcp_servers[0] == McpServerSpec("s", "echo")


def test_untrusted_rejects_entrypoint():
    with pytest.raises(ValueError):
        _untrusted(entrypoint="pkg:activate")


def test_untrusted_rejects_empty_mcp_servers():
    with pytest.raises(ValueError):
        ExtensionManifest(id="u", kind="untrusted")


@pytest.mark.parametrize("contrib", [
    {"commands": (CommandContribution("/x"),)},
    {"task_kinds": ("k",)},
    {"hidden_agents": ("a",)},
    {"lifecycle_events": ("e",)},
    {"model_roles": ("r",)},
])
def test_untrusted_rejects_in_process_contributions(contrib):
    with pytest.raises(ValueError):
        ExtensionManifest(id="u", kind="untrusted",
                          contributes=ExtensionContributes(
                              mcp_servers=(McpServerSpec("s", "echo"),), **contrib))


def test_untrusted_rejects_capabilities():
    with pytest.raises(ValueError):
        _untrusted(capabilities=frozenset({"spawn:reserved"}))


def test_mcp_server_spec_frozen_and_hashable():
    s = McpServerSpec("s", "echo", args=("a",), env=(("K", "V"),))
    assert hash(s) == hash(McpServerSpec("s", "echo", args=("a",), env=(("K", "V"),)))
    with pytest.raises(Exception):
        s.command = "rm"  # frozen


def test_command_contribution_defaults():
    c = CommandContribution("/foo")
    assert c.match == "exact"
    assert c.description == "" and c.arg_hint == ""


def test_memory_evolution_manifest_shape():
    from nanocode.extensions.memory_evolution.manifest import MANIFEST
    assert MANIFEST.id == "nanocode.memory_evolution"
    assert MANIFEST.kind == "system"
    names = {c.name for c in MANIFEST.contributes.commands}
    assert names == {"/memory optimize", "/memory eval generate"}
    assert "memory_optimize" in MANIFEST.contributes.task_kinds
    assert "memory:write_retrieval_config" in MANIFEST.capabilities
