import asyncio
from nanocode.tools import execute_tool, get_deferred_tool_names, reset_activated_tools


def _run(coro):
    return asyncio.run(coro)


def test_read_before_edit_guard(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("hi")
    out = _run(execute_tool("write_file", {"file_path": str(p), "content": "new"}, {}))
    assert "must read this file before" in out.lower()
    assert p.read_text() == "hi"  # 未写入


def test_read_then_write_ok(tmp_path):
    p = tmp_path / "y.txt"
    p.write_text("hi")
    state = {}
    _run(execute_tool("read_file", {"file_path": str(p)}, state))
    out = _run(execute_tool("write_file", {"file_path": str(p), "content": "new"}, state))
    assert "Successfully wrote" in out
    assert p.read_text() == "new"


def test_tool_search_activates_deferred():
    reset_activated_tools()
    assert "enter_plan_mode" in get_deferred_tool_names()
    out = _run(execute_tool("tool_search", {"query": "plan"}))
    assert "enter_plan_mode" in out
    assert "enter_plan_mode" not in get_deferred_tool_names()


def test_unknown_tool():
    out = _run(execute_tool("does_not_exist", {}))
    assert "Unknown tool" in out
