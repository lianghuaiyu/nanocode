from nanocode.memory import env_callables


def test_embed_none_when_env_missing(monkeypatch):
    for k in ("NANOCODE_MEMORY_EMBED_BASE_URL", "NANOCODE_MEMORY_EMBED_API_KEY",
              "NANOCODE_MEMORY_EMBED_MODEL", "NANOCODE_MEMORY_EMBED_DIM"):
        monkeypatch.delenv(k, raising=False)
    assert env_callables.build_embed_callable_from_env() is None


def test_embed_none_when_partial(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_BASE_URL", "https://x/v1")
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_API_KEY", "k")
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_MODEL", "m")
    monkeypatch.delenv("NANOCODE_MEMORY_EMBED_DIM", raising=False)
    assert env_callables.build_embed_callable_from_env() is None


def test_embed_bad_dim(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_BASE_URL", "https://x/v1")
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_API_KEY", "k")
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_MODEL", "m")
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_DIM", "notanint")
    assert env_callables.build_embed_callable_from_env() is None


def test_embed_present_when_complete(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_BASE_URL", "https://x/v1")
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_API_KEY", "k")
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_MODEL", "m")
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_DIM", "8")
    out = env_callables.build_embed_callable_from_env()
    assert out is not None and out[1] == 8 and callable(out[0])


def test_llm_none_without_key(monkeypatch):
    monkeypatch.delenv("NANOCODE_MEMORY_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert env_callables.build_llm_callable_from_env() is None


def test_llm_present_with_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert callable(env_callables.build_llm_callable_from_env())
