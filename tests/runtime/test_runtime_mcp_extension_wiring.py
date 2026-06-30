"""docs/26 G6: RuntimeServices wires untrusted extensions' MCP servers into the
McpManager BEFORE the lazy first-turn connect (out-of-process tier).
"""
import json
import pathlib

from nanocode.runtime.facade import AgentConfig, RuntimeServices


def _write_untrusted_ext(cwd: pathlib.Path) -> None:
    d = cwd / ".nanocode" / "extensions" / "acme"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({
        "id": "acme.tools",
        "mcp_servers": [{"name": "files", "command": "echo", "args": ["x"]}]}), encoding="utf-8")


def test_runtime_services_wires_untrusted_mcp_servers(tmp_path):
    _write_untrusted_ext(tmp_path)
    services = RuntimeServices.create(
        AgentConfig(api_key="test", cwd=str(tmp_path)), cwd=str(tmp_path))
    mgr = services.mcp_manager
    assert mgr is not None
    # extension-declared server is staged (namespaced, no '__') before any connect
    assert mgr._extension_servers  # non-empty
    key = next(iter(mgr._extension_servers))
    assert "__" not in key
    assert mgr._extension_servers[key]["command"] == "echo"
    assert mgr._extension_servers[key]["args"] == ["x"]
    assert mgr._connected is False  # staged, not yet connected


def test_runtime_services_no_untrusted_ext_is_clean(tmp_path):
    services = RuntimeServices.create(
        AgentConfig(api_key="test", cwd=str(tmp_path)), cwd=str(tmp_path))
    assert services.mcp_manager is not None
    assert services.mcp_manager._extension_servers == {}
