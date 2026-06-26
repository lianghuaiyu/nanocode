"""Typed errors for the SimpleMem engine (docs/20 §2.4 / §5.2).

Failures are explicit and diagnosable — never a silent `[]` or a quiet fallback
to another backend.
"""
from __future__ import annotations


class SimpleMemError(Exception):
    """Base class for engine errors."""


class EngineUnavailable(SimpleMemError):
    """A capability requiring an injected llm/embed callable was invoked but the
    callable is absent. Surfaced to the user as a typed unavailable result."""


class ExtractionFailed(SimpleMemError):
    """LLM extraction failed after retries (bad JSON, missing fields, or a raised
    llm call). Distinct from a *legal* empty extraction (`[]`): an empty array is
    a success that produces no entries, whereas this signals the batch was not
    extracted at all — so the generation watermark must NOT advance past it."""
