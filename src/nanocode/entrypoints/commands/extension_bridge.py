"""commands/extension_bridge.py — merge system-extension commands into the builtin registry.

This is the integration seam between the (boundary-pure) `extensions/` package and
the builtin command layer. The extension contributes `(CommandContribution,
ext_handler)` pairs; the bridge wraps each into a builtin `Command` whose handler
resolves the *live* bound `ExtensionHost` from the command context at call time,
builds a fresh `ExtensionCommandContext`, and runs the extension handler.

Builtin-vs-extension command name collisions fail loud here (docs/22 §7 Phase 0
conflict rule #1) — the builtin registry is only known at this layer.
"""
from __future__ import annotations

from .registry import Registry
from .types import Command, CommandContext, CommandSpec, Local


def _make_handler(ext_handler, extension_id: str):
    async def _run(ctx: CommandContext, args: str) -> Local:
        thread = ctx.thread
        host = getattr(thread, "extension_host", None)
        if host is None or not host.is_active:
            return Local(output="memory evolution extension is not available for this session.")
        ext_ctx = host.create_command_context(extension_id)
        out = await ext_handler(ext_ctx, args)
        return Local(output=out if isinstance(out, str) else (out or ""))
    return _run


def merge_system_extension_commands(registry: Registry) -> "object":
    """Activate the system extensions and merge their commands into `registry`.

    Returns the activated (unbound) `ExtensionHost` so callers can reuse it (e.g.
    to enumerate task kinds). Fails loud on a builtin-vs-extension name collision."""
    from ...extensions import ExtensionHost
    from ...extensions.errors import ExtensionLoadError

    host = ExtensionHost.load_system_extensions().activate_all()
    builtin_names = {s.name for s in registry.specs()}
    for contribution, ext_handler, extension_id in host.command_contributions():
        if contribution.name in builtin_names:
            raise ExtensionLoadError(
                f"extension command {contribution.name!r} collides with a "
                f"non-replaceable builtin command")
        spec = CommandSpec(
            name=contribution.name, kind="local",
            description=contribution.description, arg_hint=contribution.arg_hint,
            match=contribution.match, source="extension")
        registry.register(Command(spec, _make_handler(ext_handler, extension_id)))
    return host
