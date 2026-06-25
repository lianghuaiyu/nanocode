import pytest

from nanocode.memory.service import MemoryServiceConfig, DEFAULT_BACKEND


def test_default_is_markdown(monkeypatch):
    monkeypatch.delenv("NANOCODE_MEMORY_BACKEND", raising=False)
    assert MemoryServiceConfig.resolve(None).backend == "markdown" == DEFAULT_BACKEND


def test_cli_choice_wins(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_BACKEND", "markdown")
    assert MemoryServiceConfig.resolve("off").backend == "off"


def test_env_overrides_default(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_BACKEND", "off")
    assert MemoryServiceConfig.resolve(None).backend == "off"


def test_invalid_backend_fails_loud(monkeypatch):
    monkeypatch.delenv("NANOCODE_MEMORY_BACKEND", raising=False)
    with pytest.raises(ValueError, match="unknown memory backend"):
        MemoryServiceConfig.resolve("bogus")
    monkeypatch.setenv("NANOCODE_MEMORY_BACKEND", "auto")  # "auto" is no longer valid
    with pytest.raises(ValueError, match="unknown memory backend"):
        MemoryServiceConfig.resolve(None)


def test_simplemem_explicit(monkeypatch):
    monkeypatch.delenv("NANOCODE_MEMORY_BACKEND", raising=False)
    assert MemoryServiceConfig.resolve("simplemem").backend == "simplemem"


def test_policy_flags(monkeypatch):
    for k in ("NANOCODE_MEMORY_USE", "NANOCODE_MEMORY_GENERATE", "NANOCODE_MEMORY_EXTERNAL_GUARD"):
        monkeypatch.delenv(k, raising=False)
    c = MemoryServiceConfig.resolve(None)
    assert c.use_memories and c.generate_memories and c.disable_on_external_context
    monkeypatch.setenv("NANOCODE_MEMORY_USE", "false")
    monkeypatch.setenv("NANOCODE_MEMORY_GENERATE", "0")
    monkeypatch.setenv("NANOCODE_MEMORY_EXTERNAL_GUARD", "no")
    c = MemoryServiceConfig.resolve(None)
    assert not c.use_memories and not c.generate_memories and not c.disable_on_external_context
