"""memory/env_callables.py — host-side LLM/embedding callable builders.

These live on the *host* side of the memory boundary (docs/20 §2.1 #4): the
host resolves credentials/config from env and injects plain callables into the
`MemoryService`. The SimpleMem engine never constructs an OpenAI client itself.

`openai` is imported lazily inside the callables so that constructing these
builders (and the default markdown/off backends) never requires the provider
package.
"""
from __future__ import annotations

import os


def build_embed_callable_from_env():
    """Read NANOCODE_MEMORY_EMBED_* env. Returns (embed_fn, dim) or None."""
    base = os.environ.get("NANOCODE_MEMORY_EMBED_BASE_URL")
    key = os.environ.get("NANOCODE_MEMORY_EMBED_API_KEY")
    model = os.environ.get("NANOCODE_MEMORY_EMBED_MODEL")
    raw_dim = os.environ.get("NANOCODE_MEMORY_EMBED_DIM")
    if not (base and key and model and raw_dim):
        return None
    try:
        dim = int(raw_dim)
    except (TypeError, ValueError):
        return None
    if dim <= 0:
        return None

    def embed_fn(texts: list[str]) -> list[list[float]]:
        from openai import OpenAI
        client = OpenAI(base_url=base, api_key=key)
        resp = client.embeddings.create(model=model, input=list(texts))
        return [d.embedding for d in resp.data]

    return embed_fn, dim


def build_llm_callable_from_env():
    """Memory LLM: NANOCODE_MEMORY_LLM_* preferred, falls back to OPENAI_*.
    Returns a callable(messages) -> str, or None when no key is available."""
    key = os.environ.get("NANOCODE_MEMORY_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    base = os.environ.get("NANOCODE_MEMORY_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    model = os.environ.get("NANOCODE_MEMORY_LLM_MODEL", "gpt-4o-mini")

    def llm_fn(messages: list[dict]) -> str:
        from openai import OpenAI
        client = OpenAI(base_url=base, api_key=key) if base else OpenAI(api_key=key)
        resp = client.chat.completions.create(model=model, messages=messages, temperature=0.2)
        return resp.choices[0].message.content or ""

    return llm_fn
