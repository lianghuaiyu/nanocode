"""Structured diagnostics for the SimpleMem engine (docs/20 §6.4).

The engine never prints. It logs to `nanocode.memory.simplemem`; callers stay
quiet by default and can opt into diagnostics via standard logging config.
"""
from __future__ import annotations

import logging

log = logging.getLogger("nanocode.memory.simplemem")

__all__ = ["log"]
