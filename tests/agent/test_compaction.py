from nanocode.agent.compaction import persist_large_result
from nanocode.tools.context import default_tool_context

_CTX = default_tool_context()


def test_small_passthrough():
    assert persist_large_result("read_file", "short output") == "short output"


def test_large_persisted():
    big = "line\n" * 40000  # > 30 KB
    out = persist_large_result("grep_search", big)
    assert "Result too large" in out
    assert "Preview (first 200 lines)" in out
    assert "read_file" in out  # 提示可用 read_file 取回


# ─── docs/16 #8：output-cap 补齐 ─────────────────────────────────────────────

def test_persist_large_result_shell_preview_keeps_tail(tmp_path, monkeypatch):
    # 失败命令的报错在尾部——shell 类工具的 spill 预览必须 tail-keep（pi truncateTail 同义）。
    from nanocode.agent.compaction import persist_large_result
    monkeypatch.setattr("nanocode.agent.compaction.tool_results_dir", lambda: tmp_path)
    body = "\n".join(f"line-{i}" for i in range(5000)) + "\nFAILED: the real error"
    out = persist_large_result("run_shell", body)
    assert "Preview (last 200 lines)" in out
    assert "FAILED: the real error" in out          # 尾部保留
    assert "line-0\n" not in out                     # 头部被裁


def test_persist_large_result_other_tools_keep_head(tmp_path, monkeypatch):
    from nanocode.agent.compaction import persist_large_result
    monkeypatch.setattr("nanocode.agent.compaction.tool_results_dir", lambda: tmp_path)
    body = "head-marker\n" + "\n".join(f"l{i}" * 10 for i in range(8000))
    out = persist_large_result("read_file", body)
    assert "Preview (first 200 lines)" in out
    assert "head-marker" in out


def test_grep_line_cap_truncates_long_matches(tmp_path):
    from nanocode.tools import grep_search
    (tmp_path / "minified.js").write_text("needle " + "x" * 5000 + "\n")
    out = grep_search.run(_CTX, {"pattern": "needle", "path": str(tmp_path)})
    line = next(l for l in out.split("\n") if "needle" in l)
    assert len(line) <= grep_search.MAX_LINE_CHARS + 100   # 500 cap + 路径前缀 + 截断标记
    assert "chars]" in line                                 # 截断标记在场
