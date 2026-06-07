from nanocode.agent.compaction import persist_large_result


def test_small_passthrough():
    assert persist_large_result("read_file", "short output") == "short output"


def test_large_persisted():
    big = "line\n" * 40000  # > 30 KB
    out = persist_large_result("grep_search", big)
    assert "Result too large" in out
    assert "Preview (first 200 lines)" in out
    assert "read_file" in out  # 提示可用 read_file 取回
