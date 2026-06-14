"""Lightweight model policy for context assembly.

This module must stay free of agent/provider imports: slash commands and
embedded hosts can import it without pulling the agent loop or SDK backends.
"""

from __future__ import annotations


def model_uses_repo_map(model: str) -> bool:
    """Aider-style default: enable repo map for model families that benefit from it."""
    m = model.lower()
    if "/o3-mini" in m:
        return True
    if "gpt-4.1" in m:
        return True
    if "/o1-mini" in m or "/o1-preview" in m or "/o1" in m:
        return True
    if "deepseek" in m and ("v3" in m or "r1" in m or "reasoning" in m):
        return True
    if ("llama3" in m or "llama-3" in m) and "70b" in m:
        return True
    if "gpt-4-turbo" in m or ("gpt-4-" in m and "-preview" in m):
        return True
    if "gpt-4" in m or "claude-3-opus" in m:
        return True
    if "sonnet-4-" in m or "opus-4-" in m or "haiku-4-" in m:
        return True
    if "3.5-sonnet" in m or "3-5-sonnet" in m or "3-7-sonnet" in m:
        return True
    if "qwen" in m and "coder" in m and ("2.5" in m or "2-5" in m) and "32b" in m:
        return True
    if "qwq" in m and "32b" in m and "preview" not in m:
        return True
    if "qwen3" in m and "235b" in m:
        return True
    return False
