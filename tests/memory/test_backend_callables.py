from nanocode.memory import backend


def test_embed_callable_none_when_env_missing(monkeypatch):
    for k in ("NANOCODE_MEMORY_EMBED_BASE_URL", "NANOCODE_MEMORY_EMBED_API_KEY",
              "NANOCODE_MEMORY_EMBED_MODEL", "NANOCODE_MEMORY_EMBED_DIM"):
        monkeypatch.delenv(k, raising=False)
    assert backend.build_embed_callable_from_env() is None


def test_embed_callable_none_when_partial(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_BASE_URL", "https://x/v1")
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_API_KEY", "k")
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_MODEL", "m")
    monkeypatch.delenv("NANOCODE_MEMORY_EMBED_DIM", raising=False)
    assert backend.build_embed_callable_from_env() is None


def test_embed_callable_bad_dim(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_BASE_URL", "https://x/v1")
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_API_KEY", "k")
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_MODEL", "m")
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_DIM", "notanint")
    assert backend.build_embed_callable_from_env() is None


def test_embed_callable_present_when_complete(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_BASE_URL", "https://x/v1")
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_API_KEY", "k")
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_MODEL", "m")
    monkeypatch.setenv("NANOCODE_MEMORY_EMBED_DIM", "16")
    res = backend.build_embed_callable_from_env()
    assert res is not None
    fn, dim = res
    assert callable(fn) and dim == 16


def test_llm_callable_none_without_any_key(monkeypatch):
    for k in ("NANOCODE_MEMORY_LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert backend.build_llm_callable_from_env() is None


def test_llm_callable_present_with_memory_key(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_LLM_API_KEY", "k")
    monkeypatch.setenv("NANOCODE_MEMORY_LLM_BASE_URL", "https://x/v1")
    assert callable(backend.build_llm_callable_from_env())


def test_llm_callable_falls_back_to_openai_key(monkeypatch):
    monkeypatch.delenv("NANOCODE_MEMORY_LLM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x/v1")
    assert callable(backend.build_llm_callable_from_env())
