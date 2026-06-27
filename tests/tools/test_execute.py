import asyncio
from nanocode.tools import REGISTRY, execute_tool, get_deferred_tool_names, reset_activated_tools
from nanocode.tools.context import default_tool_context

_CTX = default_tool_context()


def _run(coro):
    return asyncio.run(coro)


def test_read_before_edit_guard(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("hi")
    out = _run(execute_tool("write_file", {"file_path": str(p), "content": "new"}, {},
                            ctx=_CTX, registry=REGISTRY))
    assert "must read this file before" in out.lower()
    assert p.read_text() == "hi"  # 未写入


def test_read_then_write_ok(tmp_path):
    p = tmp_path / "y.txt"
    p.write_text("hi")
    state = {}
    _run(execute_tool("read_file", {"file_path": str(p)}, state,
                      ctx=_CTX, registry=REGISTRY))
    out = _run(execute_tool("write_file", {"file_path": str(p), "content": "new"}, state,
                            ctx=_CTX, registry=REGISTRY))
    assert "Successfully wrote" in out
    assert p.read_text() == "new"


def test_tool_search_activates_deferred():
    reset_activated_tools()
    assert "enter_plan_mode" in get_deferred_tool_names(registry=REGISTRY)
    out = _run(execute_tool("tool_search", {"query": "plan"}, ctx=_CTX, registry=REGISTRY))
    assert "enter_plan_mode" in out
    assert "enter_plan_mode" not in get_deferred_tool_names(registry=REGISTRY)


def test_unknown_tool():
    out = _run(execute_tool("does_not_exist", {}, ctx=_CTX, registry=REGISTRY))
    assert "Unknown tool" in out
