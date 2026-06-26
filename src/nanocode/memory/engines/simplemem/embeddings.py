"""Host-injected embedding adapter (docs/20 §6.3).

`Embedder` wraps a host callable `(texts: list[str]) -> list[list[float]]` plus
its fixed dimension. No sentence-transformers, no torch, no env. When no
callable is injected the embedder is unavailable and embedding ops raise.
"""
from __future__ import annotations

from typing import Callable

from .errors import EngineUnavailable

EmbedCallable = Callable[[list], list]


class Embedder:
    def __init__(self, callable_: "EmbedCallable | None", dimension: int) -> None:
        self._callable = callable_
        if dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        self.dimension = dimension

    @property
    def available(self) -> bool:
        return self._callable is not None

    def encode_documents(self, texts: list) -> list:
        return self._encode(texts)

    def encode_query(self, text: str) -> list:
        return self._encode([text])[0]

    def _encode(self, texts: list) -> list:
        if self._callable is None:
            raise EngineUnavailable(
                "SimpleMem embedding requested but no embed callable was injected")
        vectors = self._callable(list(texts))
        out = []
        for v in vectors:
            row = [float(x) for x in v]
            if len(row) != self.dimension:
                raise ValueError(
                    f"embedding dimension mismatch: got {len(row)}, expected {self.dimension}")
            out.append(row)
        return out
