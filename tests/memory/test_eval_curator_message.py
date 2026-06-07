from nanocode.paths import project_memory_dir
from nanocode.memory.maintenance import build_eval_curator_message


def test_empty_returns_sentinel():
    project_memory_dir()  # 创建空目录（conftest NANOCODE_HOME 隔离）
    msg = build_eval_curator_message()
    assert msg.startswith("No memory files")


def test_includes_memory_contents_and_excludes_index():
    mem = project_memory_dir()
    (mem / "project_goals.md").write_text(
        "---\nname: goals\n---\nWe want to ship v2 by end of Q1."
    )
    (mem / "MEMORY.md").write_text("index, should be excluded")
    msg = build_eval_curator_message()
    assert "project_goals.md" in msg
    assert "ship v2 by end of Q1" in msg
    assert "should be excluded" not in msg  # MEMORY.md 被跳过


def test_exported_from_memory_package():
    from nanocode.memory import build_eval_curator_message as exported
    assert exported is build_eval_curator_message
