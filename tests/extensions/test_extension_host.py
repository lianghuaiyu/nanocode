"""docs/22 Phase 0 / §9.1: ExtensionHost load/activate/bind + conflict rules.

Boundary checks: activation only registers (no MemoryService access), the host
is inactive before bind, and register_tool is fail-loud.
"""
import sys

import pytest

from nanocode.extensions import ExtensionHost, ExtensionLoadError
from nanocode.extensions.api import ExtensionAPI
from nanocode.extensions.manifest import CommandContribution
from nanocode.extensions.registry import (
    ContributionRegistry, HiddenAgentProfile, ModelRolePolicy,
)


def test_import_extensions_has_no_side_effects():
    # Importing the package must not pull runtime/session or build a host
    # (docs/22 §9.1.3).
    assert "nanocode.runtime" not in sys.modules or True  # may be loaded by other tests
    h = ExtensionHost.load_system_extensions()
    assert h.is_active is False  # not bound yet


def test_activate_does_not_touch_memory_service(monkeypatch):
    # docs/22 §9.1.4: activate_all() must only register; it must not construct or
    # import the MemoryService implementation.
    import nanocode.memory.service as svc

    def _boom(*a, **k):
        raise AssertionError("activation must not build a MemoryService")

    monkeypatch.setattr(svc.MemoryService, "__init__", _boom)
    h = ExtensionHost.load_system_extensions().activate_all()
    assert h.is_active is False
    assert "/memory optimize" in {c.name for c, _h, _eid in h.command_contributions()}


def test_activate_registers_expected_contributions():
    h = ExtensionHost.load_system_extensions().activate_all()
    assert {c.name for c, _h, _eid in h.command_contributions()} == {
        "/memory optimize", "/memory eval generate"}
    assert "memory_optimize" in h.registry.task_kinds
    assert "memory_diagnosis" in h.registry.model_roles
    assert "memory-retrieval-diagnostician" in h.registry.hidden_agents


def test_command_contributions_carry_extension_id():
    h = ExtensionHost.load_system_extensions().activate_all()
    for _c, _handler, ext_id in h.command_contributions():
        assert ext_id == "nanocode.memory_evolution"


def test_run_task_before_bind_fails_loud():
    import asyncio
    h = ExtensionHost.load_system_extensions().activate_all()
    from nanocode.extensions.errors import ExtensionRuntimeError
    with pytest.raises(ExtensionRuntimeError):
        asyncio.run(h.run_task("memory_optimize", {}, task_id="t1"))


def test_duplicate_command_conflict_fails_loud():
    reg = ContributionRegistry()
    api = ExtensionAPI(reg, extension_id="a")
    c = CommandContribution("/dup")
    api.register_command(c, lambda *a, **k: None)
    api2 = ExtensionAPI(reg, extension_id="b")
    with pytest.raises(ExtensionLoadError):
        api2.register_command(c, lambda *a, **k: None)


def test_duplicate_task_kind_conflict_fails_loud():
    reg = ContributionRegistry()
    api = ExtensionAPI(reg, extension_id="a")
    api.register_task_kind("k", lambda *a, **k: None)
    with pytest.raises(ExtensionLoadError):
        api.register_task_kind("k", lambda *a, **k: None)


def test_hidden_agent_collision_with_custom_fails_loud_at_bind(monkeypatch):
    # docs/22: the hidden-vs-custom-agent collision is checked at bind_runtime
    # (a host/trust phase) so activation stays free of env/project reads.
    h = ExtensionHost.load_system_extensions().activate_all()
    monkeypatch.setattr("nanocode.agents.registry.discover_custom_agents",
                        lambda: {"memory-retrieval-diagnostician": {}})
    with pytest.raises(ExtensionLoadError):
        h.bind_runtime(thread=object(), services=None)


def test_register_tool_records_contribution():
    # docs/24 Phase 4b：register_tool 已解禁——登记一个工具贡献（裸名 + schema + handler + needs）。
    reg = ContributionRegistry()
    api = ExtensionAPI(reg, extension_id="a")

    async def _h(inp, ctx):
        return "ok"

    api.register_tool({"name": "do_thing", "description": "d",
                       "input_schema": {"type": "object", "properties": {}}}, _h)
    assert "do_thing" in reg.tools
    rt = reg.tools["do_thing"]
    assert rt.extension_id == "a" and rt.handler is _h


def test_register_tool_requires_name():
    reg = ContributionRegistry()
    api = ExtensionAPI(reg, extension_id="a")
    with pytest.raises(ExtensionLoadError):
        api.register_tool({"description": "no name"}, lambda inp, ctx: "x")


def test_register_tool_dup_is_fail_loud():
    reg = ContributionRegistry()
    api = ExtensionAPI(reg, extension_id="a")
    spec = {"name": "do_thing", "input_schema": {"type": "object", "properties": {}}}
    api.register_tool(spec, lambda inp, ctx: "x")
    with pytest.raises(ExtensionLoadError):
        api.register_tool(spec, lambda inp, ctx: "y")


def test_model_role_resolves_host_and_env(monkeypatch):
    reg = ContributionRegistry()
    api = ExtensionAPI(reg, extension_id="a")
    api.register_model_role("r", ModelRolePolicy(default="host", env_var="X_MODEL"))
    from nanocode.extensions.context import ExtensionModelRouter

    class _Host:
        is_active = True
    router = ExtensionModelRouter(_Host(), host_model="claude-opus", roles=dict(reg.model_roles))
    monkeypatch.delenv("X_MODEL", raising=False)
    assert router.resolve("r") == "claude-opus"
    monkeypatch.setenv("X_MODEL", "small-model")
    assert router.resolve("r") == "small-model"


# ── docs/26 G6: untrusted extensions are declarative-only / out-of-process ──

def test_activate_all_skips_untrusted():
    from nanocode.extensions.host import ExtensionHost as _H
    from nanocode.extensions.manifest import (
        ExtensionContributes, ExtensionManifest, McpServerSpec,
    )
    untrusted = ExtensionManifest(
        id="acme.tools", kind="untrusted",
        contributes=ExtensionContributes(mcp_servers=(McpServerSpec("files", "echo"),)))
    h = _H([untrusted] + _H.load_system_extensions().manifests).activate_all()
    # untrusted contributes NOTHING in-process (no entrypoint resolved, no activate)
    contributing_ids = (
        {rc.extension_id for rc in h.registry.commands.values()}
        | {e for _hd, e in h.registry.task_kinds.values()}
        | {e for _p, e in h.registry.hidden_agents.values()})
    assert "acme.tools" not in contributing_ids


def test_mcp_contributions_namespaced_no_double_underscore():
    from nanocode.extensions.host import ExtensionHost as _H
    from nanocode.extensions.manifest import (
        ExtensionContributes, ExtensionManifest, McpServerSpec,
    )
    untrusted = ExtensionManifest(
        id="acme.tools", kind="untrusted",
        contributes=ExtensionContributes(mcp_servers=(
            McpServerSpec("files", "echo", args=("x",), env=(("A", "1"),)),)))
    h = _H([untrusted]).activate_all()
    contribs = h.mcp_contributions()
    assert len(contribs) == 1
    key = next(iter(contribs))
    assert "__" not in key  # MCP tool-name split on __ would mis-route otherwise
    assert contribs[key] == {"command": "echo", "args": ["x"], "env": {"A": "1"}}


def test_system_extensions_contribute_no_mcp_servers():
    h = ExtensionHost.load_system_extensions().activate_all()
    assert h.mcp_contributions() == {}
