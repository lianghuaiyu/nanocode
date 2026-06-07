import json

import pytest

pytest.importorskip("lancedb")  # SimpleMem backend needs the optional [simplemem] extra

from nanocode.memory import store
from nanocode.memory.backend import SimpleMemBackend, ImportResult
from nanocode.memory.maintenance import _simplemem_dir
from nanocode._vendor import simplemem
from tests.memory.test_simplemem_backend import stub_embed, stub_llm, EMBED_DIM


def _backend(tmp_path):
    sysm = simplemem.create(
        mode="text", db_path=str(tmp_path / "lance"),
        llm_callable=stub_llm, embed_callable=stub_embed,
        embed_dimension=EMBED_DIM, clear_db=True,
        enable_planning=False, enable_reflection=False,
    )
    return SimpleMemBackend(system=sysm, db_path=str(tmp_path / "lance"))


def test_import_imports_all_then_idempotent(tmp_path):
    store.save_memory("Alpha", "a", "project", "alpha body content")
    store.save_memory("Beta", "b", "project", "beta body content")
    b = _backend(tmp_path)

    r1 = b.import_markdown_memories()
    assert isinstance(r1, ImportResult)
    assert r1.imported == 2 and r1.skipped == 0

    r2 = b.import_markdown_memories()        # 幂等：全 skip
    assert r2.imported == 0 and r2.skipped == 2


def test_import_records_hashes_file(tmp_path):
    store.save_memory("Alpha", "a", "project", "alpha body")
    b = _backend(tmp_path)
    b.import_markdown_memories()
    hp = _simplemem_dir() / "imported_hashes.json"
    assert hp.exists()
    data = json.loads(hp.read_text())
    assert any(k.startswith("project_alpha") for k in data)


def test_import_reimports_when_content_changes(tmp_path):
    store.save_memory("Alpha", "a", "project", "v1 body")
    b = _backend(tmp_path)
    assert b.import_markdown_memories().imported == 1
    store.save_memory("Alpha", "a", "project", "v2 changed body")  # 同名覆盖，hash 变
    r = b.import_markdown_memories()
    assert r.imported == 1 and r.skipped == 0


def test_import_empty_dir(tmp_path):
    b = _backend(tmp_path)
    r = b.import_markdown_memories()
    assert r.imported == 0 and r.skipped == 0
