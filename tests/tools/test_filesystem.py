from nanocode.tools import read_file, write_file, edit_file, list_files, grep_search


def test_read_file(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("l1\nl2")
    out = read_file.run({"file_path": str(p)})
    assert "l1" in out and "l2" in out


def test_write_file(tmp_path):
    p = tmp_path / "b.txt"
    out = write_file.run({"file_path": str(p), "content": "hello"})
    assert p.read_text() == "hello"
    assert "Successfully wrote" in out


def test_edit_file(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text("foo bar")
    out = edit_file.run({"file_path": str(p), "old_string": "foo", "new_string": "baz"})
    assert p.read_text() == "baz bar"
    assert "Successfully edited" in out


def test_edit_not_unique(tmp_path):
    p = tmp_path / "d.txt"
    p.write_text("x x")
    out = edit_file.run({"file_path": str(p), "old_string": "x", "new_string": "y"})
    assert "unique" in out.lower()


def test_edit_not_found(tmp_path):
    p = tmp_path / "e.txt"
    p.write_text("abc")
    out = edit_file.run({"file_path": str(p), "old_string": "zzz", "new_string": "y"})
    assert "not found" in out.lower()


def test_list_files(tmp_path):
    (tmp_path / "f.py").write_text("x")
    out = list_files.run({"pattern": "*.py", "path": str(tmp_path)})
    assert "f.py" in out


def test_list_files_sorted_by_mtime_newest_first(tmp_path):
    import os
    for i, name in enumerate(["old.py", "mid.py", "new.py"]):
        p = tmp_path / name
        p.write_text("x")
        os.utime(p, (1000 + i * 100, 1000 + i * 100))  # 递增 mtime
    out = list_files.run({"pattern": "*.py", "path": str(tmp_path)})
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines == ["new.py", "mid.py", "old.py"]


def test_list_files_truncation_reports_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr(list_files, "MAX_RESULTS", 2)
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text("x")
    out = list_files.run({"pattern": "*.py", "path": str(tmp_path)})
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == 3          # 2 文件 + 1 截断提示
    assert "3 more" in out          # 5 - 2 = 3



def test_grep_search(tmp_path):
    (tmp_path / "g.txt").write_text("needle here\nother")
    out = grep_search.run({"pattern": "needle", "path": str(tmp_path)})
    assert "needle" in out
