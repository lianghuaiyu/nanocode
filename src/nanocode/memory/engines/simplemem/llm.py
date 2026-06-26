"""Host-injected LLM adapter (docs/20 §6.3).

`LlmClient` wraps a host callable `(messages: list[dict]) -> str`. When no
callable is injected, LLM-requiring operations raise `EngineUnavailable` — the
engine never constructs an OpenAI client and never reads env.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from .errors import EngineUnavailable

LlmCallable = Callable[[list], str]


class LlmClient:
    def __init__(self, callable_: "LlmCallable | None" = None) -> None:
        self._callable = callable_

    @property
    def available(self) -> bool:
        return self._callable is not None

    def complete(self, messages: list) -> str:
        if self._callable is None:
            raise EngineUnavailable(
                "SimpleMem LLM operation requested but no llm callable was injected")
        return self._callable(messages)

    @staticmethod
    def extract_json(text: str) -> Any:
        """Best-effort JSON extraction from model output (pure, no network)."""
        if not text or not text.strip():
            raise ValueError("empty response")
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # fenced block
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        if m:
            try:
                return json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                pass
        # balanced object/array scan
        for open_c, close_c in (("{", "}"), ("[", "]")):
            start = text.find(open_c)
            if start == -1:
                continue
            depth = 0
            in_str = False
            esc = False
            for i in range(start, len(text)):
                c = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                    continue
                if c == '"':
                    in_str = True
                elif c == open_c:
                    depth += 1
                elif c == close_c:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except json.JSONDecodeError:
                            break
        raise ValueError(f"could not extract JSON from response: {text[:200]!r}")
