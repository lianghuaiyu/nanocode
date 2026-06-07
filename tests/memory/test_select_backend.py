import pytest
from nanocode.memory import backend
from nanocode.memory.backend import (
    select_backend, OffMemoryBackend, MarkdownMemoryBackend, create_simplemem_backend,
)


def test_select_off(monkeypatch):
    monkeypatch.delenv("NANOCODE_MEMORY_BACKEND", raising=False)
    assert isinstance(select_backend("off"), OffMemoryBackend)


def test_select_markdown(monkeypatch):
    monkeypatch.delenv("NANOCODE_MEMORY_BACKEND", raising=False)
    assert isinstance(select_backend("markdown"), MarkdownMemoryBackend)


def test_select_simplemem_success(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(backend, "create_simplemem_backend", lambda: sentinel)
    assert select_backend("simplemem") is sentinel


def test_select_simplemem_falls_back_to_markdown_on_failure(monkeypatch):
    def boom():
        raise RuntimeError("no embedding endpoint")
    monkeypatch.setattr(backend, "create_simplemem_backend", boom)
    warnings = []
    b = select_backend("simplemem", on_warning=warnings.append)
    assert isinstance(b, MarkdownMemoryBackend)
    # 显式 simplemem 失败 → warning（用户明确要了才警告）
    assert warnings and "markdown" in warnings[0].lower()


def test_create_simplemem_raises_without_embed_env(monkeypatch):
    monkeypatch.setattr(backend, "build_embed_callable_from_env", lambda: None)
    with pytest.raises(Exception):
        create_simplemem_backend()


# ── auto 分支（人工最终决策覆盖 #4：静默降级）──────────────────

def test_auto_without_embed_env_is_silent_markdown(monkeypatch):
    # auto + embed env 不齐全 → 静默 markdown，不打 warning，不尝试 simplemem
    monkeypatch.delenv("NANOCODE_MEMORY_BACKEND", raising=False)
    monkeypatch.setattr(backend, "build_embed_callable_from_env", lambda: None)
    called = {"n": 0}

    def boom():
        called["n"] += 1
        raise RuntimeError("should not be called")

    monkeypatch.setattr(backend, "create_simplemem_backend", boom)
    warnings = []
    b = select_backend(None, on_warning=warnings.append)   # 默认 auto
    assert isinstance(b, MarkdownMemoryBackend)
    assert called["n"] == 0          # 未尝试 simplemem
    assert warnings == []            # 静默，无 warning


def test_auto_with_embed_env_attempts_simplemem(monkeypatch):
    # auto + embed env 齐全 → 尝试 simplemem
    monkeypatch.delenv("NANOCODE_MEMORY_BACKEND", raising=False)
    monkeypatch.setattr(backend, "build_embed_callable_from_env", lambda: (lambda x: [], 8))
    sentinel = object()
    monkeypatch.setattr(backend, "create_simplemem_backend", lambda: sentinel)
    assert select_backend(None) is sentinel


def test_auto_with_embed_env_failure_silent_markdown(monkeypatch):
    # auto + embed env 齐全但 init 失败 → 静默降级 markdown（auto 不 warning）
    monkeypatch.delenv("NANOCODE_MEMORY_BACKEND", raising=False)
    monkeypatch.setattr(backend, "build_embed_callable_from_env", lambda: (lambda x: [], 8))

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(backend, "create_simplemem_backend", boom)
    warnings = []
    b = select_backend(None, on_warning=warnings.append)
    assert isinstance(b, MarkdownMemoryBackend)
    assert warnings == []            # auto 静默
