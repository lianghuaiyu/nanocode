"""docs/22 Phase 2: backend-aware EVAL-mode curator input (eval_source)."""
from nanocode.paths import project_memory_dir
from nanocode.memory.eval_source import build_eval_curator_message, valid_memory_refs


class _MarkdownBackend:
    name = "markdown"


class _OffBackend:
    name = "off"


def test_off_backend_returns_sentinel():
    msg = build_eval_curator_message(_OffBackend())
    assert msg.startswith("No memories")


def test_none_backend_returns_sentinel():
    assert build_eval_curator_message(None).startswith("No memories")


def test_markdown_empty_returns_sentinel():
    project_memory_dir()  # 创建空目录（conftest NANOCODE_HOME 隔离）
    assert build_eval_curator_message(_MarkdownBackend()).startswith("No memories")


def test_markdown_includes_contents_and_excludes_index():
    mem = project_memory_dir()
    (mem / "project_goals.md").write_text(
        "---\nname: goals\n---\nWe want to ship v2 by end of Q1."
    )
    (mem / "MEMORY.md").write_text("index, should be excluded")
    msg = build_eval_curator_message(_MarkdownBackend())
    assert "project_goals.md" in msg
    assert "ship v2 by end of Q1" in msg
    assert "should be excluded" not in msg  # MEMORY.md 被跳过


def test_markdown_valid_refs():
    mem = project_memory_dir()
    (mem / "a.md").write_text("x")
    (mem / "MEMORY.md").write_text("idx")
    refs = valid_memory_refs(_MarkdownBackend())
    assert "a.md" in refs and "MEMORY.md" not in refs


def test_exported_from_memory_package():
    from nanocode.memory import build_eval_curator_message as exported
    assert exported is build_eval_curator_message


# ── simplemem backend ───────────────────────────────────────────────

def _simplemem_backend(tmp_path):
    import pytest
    pytest.importorskip("lancedb")
    from nanocode.memory.engines.simplemem import (
        SimpleMemConfig, MemoryNote, create_simplemem_engine,
    )
    from nanocode.memory.simplemem_backend import SimpleMemBackend

    dim = 16

    def embed(texts):
        out = []
        for t in texts:
            v = [0.0] * dim
            for tok in t.lower().split():
                v[sum(ord(c) for c in tok) % dim] += 1.0
            out.append(v)
        return out

    cfg = SimpleMemConfig(root=str(tmp_path / "store"), embed_dimension=dim)
    eng = create_simplemem_engine(cfg, llm=None, embed=embed, data_root=str(tmp_path))
    eng.add_note(MemoryNote(title="Deploy guide", content="deploy the fleet service"))
    return SimpleMemBackend(eng)


def test_simplemem_message_uses_entry_refs(tmp_path):
    backend = _simplemem_backend(tmp_path)
    msg = build_eval_curator_message(backend)
    assert "simplemem://" in msg
    assert "deploy the fleet service" in msg
    refs = valid_memory_refs(backend)
    assert refs and all(r.startswith("simplemem://") for r in refs)
