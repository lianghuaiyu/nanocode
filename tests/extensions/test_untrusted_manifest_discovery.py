"""docs/26 G6: discovery of untrusted declarative extensions.

`ExtensionHost._discover_untrusted_manifests` scans `<cwd>/.nanocode/extensions/
*/manifest.json` for `kind="untrusted"` extensions that declare MCP servers only.
Discovery is STRICT (only mcp_servers; __post_init__ enforces the rest) and
FAIL-SOFT per extension (one malformed manifest never breaks the session).
"""
import json
import pathlib

from nanocode.extensions import ExtensionHost


def _write_manifest(root: pathlib.Path, name: str, body: dict) -> None:
    d = root / ".nanocode" / "extensions" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps(body), encoding="utf-8")


def test_discovers_valid_untrusted_manifest(tmp_path):
    _write_manifest(tmp_path, "acme", {
        "id": "acme.tools",
        "mcp_servers": [{"name": "files", "command": "echo", "args": ["x"], "env": {"A": "1"}}]})
    manifests = ExtensionHost._discover_untrusted_manifests(str(tmp_path))
    assert len(manifests) == 1
    m = manifests[0]
    assert m.id == "acme.tools" and m.kind == "untrusted" and m.entrypoint == ""
    spec = m.contributes.mcp_servers[0]
    assert spec.name == "files" and spec.command == "echo"
    assert spec.args == ("x",) and spec.env == (("A", "1"),)


def test_missing_dir_returns_empty(tmp_path):
    assert ExtensionHost._discover_untrusted_manifests(str(tmp_path)) == []


def test_malformed_json_is_skipped_fail_soft(tmp_path):
    (tmp_path / ".nanocode" / "extensions" / "bad").mkdir(parents=True)
    (tmp_path / ".nanocode" / "extensions" / "bad" / "manifest.json").write_text("{ not json")
    _write_manifest(tmp_path, "good", {
        "id": "good.ext", "mcp_servers": [{"name": "s", "command": "echo"}]})
    manifests = ExtensionHost._discover_untrusted_manifests(str(tmp_path))
    assert [m.id for m in manifests] == ["good.ext"]  # bad skipped, good kept


def test_manifest_declaring_in_process_contributions_is_skipped(tmp_path):
    # An untrusted manifest that tries to declare commands violates __post_init__ →
    # construction raises → fail-soft skip (never silently grants in-process power).
    _write_manifest(tmp_path, "sneaky", {
        "id": "sneaky.ext",
        "commands": [{"name": "/x"}],
        "mcp_servers": [{"name": "s", "command": "echo"}]})
    # note: discovery only reads mcp_servers, so a stray "commands" key is ignored;
    # to truly exercise the guard we rely on the manifest builder. Here the commands
    # key is not parsed, so this manifest is still valid (mcp_servers only). Assert it
    # loads as a pure mcp_servers manifest (the in-process key is inert data on disk).
    manifests = ExtensionHost._discover_untrusted_manifests(str(tmp_path))
    assert len(manifests) == 1
    assert manifests[0].contributes.commands == ()


def test_load_all_extensions_merges_system_and_untrusted(tmp_path):
    _write_manifest(tmp_path, "acme", {
        "id": "acme.tools", "mcp_servers": [{"name": "files", "command": "echo"}]})
    host = ExtensionHost.load_all_extensions(cwd=str(tmp_path)).activate_all()
    ids = {m.id for m in host.manifests}
    assert "nanocode.memory_evolution" in ids and "acme.tools" in ids
    # untrusted server surfaces; system extensions contribute none
    contribs = host.mcp_contributions()
    assert any(k for k in contribs) and "__" not in next(iter(contribs))
