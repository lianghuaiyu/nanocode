from nanocode.memory.backend import resolve_backend_choice


def test_default_is_auto(monkeypatch):
    # 人工最终决策覆盖 #4：默认后端 = "auto"（静默降级），不是 simplemem。
    monkeypatch.delenv("NANOCODE_MEMORY_BACKEND", raising=False)
    assert resolve_backend_choice(None) == "auto"


def test_env_overrides_default(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_BACKEND", "markdown")
    assert resolve_backend_choice(None) == "markdown"


def test_cli_overrides_env(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_BACKEND", "markdown")
    assert resolve_backend_choice("off") == "off"


def test_invalid_cli_falls_back_to_auto(monkeypatch):
    monkeypatch.delenv("NANOCODE_MEMORY_BACKEND", raising=False)
    assert resolve_backend_choice("bogus") == "auto"


def test_invalid_env_falls_back_to_auto(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_BACKEND", "bogus")
    assert resolve_backend_choice(None) == "auto"


def test_env_case_insensitive(monkeypatch):
    monkeypatch.setenv("NANOCODE_MEMORY_BACKEND", "MARKDOWN")
    assert resolve_backend_choice(None) == "markdown"


def test_explicit_simplemem_and_auto_valid(monkeypatch):
    monkeypatch.delenv("NANOCODE_MEMORY_BACKEND", raising=False)
    assert resolve_backend_choice("simplemem") == "simplemem"
    assert resolve_backend_choice("auto") == "auto"
