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
    out = list_files.run({"path": str(tmp_path)})
    assert "f.py" in out


def test_list_files_schema_matches_pi_style_ls():
    props = list_files.SCHEMA["input_schema"]["properties"]
    assert set(props) == {"path", "limit"}
    assert list_files.SCHEMA["input_schema"]["required"] == []


def test_list_files_sorted_alphabetically_with_directory_suffix(tmp_path):
    (tmp_path / "Beta.txt").write_text("x")
    (tmp_path / "alpha").mkdir()
    (tmp_path / ".env").write_text("x")
    out = list_files.run({"path": str(tmp_path)})
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines == [".env", "alpha/", "Beta.txt"]


def test_list_files_limit_reports_overflow(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text("x")
    out = list_files.run({"path": str(tmp_path), "limit": 2})
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines[:2] == ["f0.py", "f1.py"]
    assert "[2 entries limit reached. Use limit=4 for more]" in out


def test_list_files_legacy_recursive_glob_lists_prefix_only(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    (tmp_path / "src" / "pkg").mkdir()
    (tmp_path / "src" / "pkg" / "deep.py").write_text("x")
    out = list_files.run({"pattern": "src/**/*", "path": str(tmp_path)})
    assert "a.py" in out
    assert "pkg/" in out
    assert "deep.py" not in out



def test_grep_search(tmp_path):
    (tmp_path / "g.txt").write_text("needle here\nother")
    out = grep_search.run({"pattern": "needle", "path": str(tmp_path)})
    assert "needle" in out
