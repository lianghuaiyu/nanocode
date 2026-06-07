import pytest
from nanocode.memory import store


@pytest.fixture
def chdir_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_slugify():
    assert store._slugify("Hello World!") == "hello_world"


def test_save_list_index_delete(chdir_tmp):
    fn = store.save_memory("My Note", "a desc", "project", "the content")
    items = store.list_memories()
    assert any(m.name == "My Note" for m in items)
    idx = store.load_memory_index()
    assert "My Note" in idx
    assert store.delete_memory(fn) is True
    assert store.list_memories() == []


def test_list_memories_reads_nested_metadata_type():
    from nanocode.memory import store
    d = store.get_memory_dir()
    (d / "project_x.md").write_text(
        '---\nname: x\n'
        'description: "hi"\n'
        'metadata:\n  node_type: memory\n  type: project\n---\n'
        'body content'
    )
    entries = [e for e in store.list_memories() if e.name == "x"]
    assert len(entries) == 1
    assert entries[0].type == "project"      # 嵌套 metadata.type 必须被读到
    assert entries[0].description == "hi"     # 引号被 YAML 剥掉
