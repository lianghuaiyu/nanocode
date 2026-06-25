"""docs/22 Phase 0: ExtensionManifest schema + contribution validation."""
import pytest

from nanocode.extensions.manifest import (
    CommandContribution, ExtensionContributes, ExtensionManifest,
)


def test_manifest_minimal():
    m = ExtensionManifest(id="x", kind="system",
                          entrypoint="pkg.mod:activate")
    assert m.contributes == ExtensionContributes()
    assert m.capabilities == frozenset()


def test_manifest_rejects_non_system_kind():
    with pytest.raises(ValueError):
        ExtensionManifest(id="x", kind="project", entrypoint="pkg.mod:activate")


def test_manifest_rejects_bad_entrypoint():
    with pytest.raises(ValueError):
        ExtensionManifest(id="x", kind="system", entrypoint="pkg.mod.activate")


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
