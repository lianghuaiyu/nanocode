"""Entrypoint package exports.

Keep package import light for embedded hosts and slash-command modules. The CLI
pulls in the agent loop and provider SDKs, so expose ``main`` lazily.
"""

from __future__ import annotations


def __getattr__(name: str):
    if name == "main":
        from .cli import main
        return main
    raise AttributeError(name)

__all__ = ["main"]
