"""主动 memory 工具测试：CRUD + 廉价 recall + 归档 delete + 注册 + 语义 recall 拦截。"""
import asyncio

from nanocode.tools import memory_tool as mt
from nanocode.memory.store import list_memories, get_memory_dir


def test_schema_registered():
    from nanocode.tools.registry import tool_definitions
    names = {t["name"] for t in tool_definitions}
    assert "memory" in names


def test_execute_dispatch_has_memory():
    from nanocode.tools.execute import _HANDLERS
    assert "memory" in _HANDLERS


def test_memory_schema_includes_consolidate():
    enum = mt.SCHEMA["input_schema"]["properties"]["action"]["enum"]
    assert "consolidate" in enum


def test_save_then_list_and_read():
    res = mt.run({"action": "save", "name": "pytest cmd", "type": "project",
                  "description": "how to test", "content": "pytest -q"})
    assert "Saved memory" in res
    entries = list_memories()
    assert any(e.name == "pytest cmd" for e in entries)
    fn = [e.filename for e in entries if e.name == "pytest cmd"][0]
    # list shows index
    idx = mt.run({"action": "list"})
    assert "pytest cmd" in idx
    # read full content
    body = mt.run({"action": "read", "filename": fn})
    assert "pytest -q" in body


def test_save_requires_name_and_type():
    assert "requires" in mt.run({"action": "save", "name": "x"})
    assert "Invalid type" in mt.run({"action": "save", "name": "x", "type": "bogus"})


def test_recall_keyword_ranks():
    mt.run({"action": "save", "name": "redis decision", "type": "project",
            "description": "use redis for cache", "content": "we chose redis"})
    mt.run({"action": "save", "name": "python style", "type": "feedback",
            "description": "prefer dataclasses", "content": "use dataclass"})
    res = mt.run({"action": "recall", "query": "redis cache"})
    assert "redis" in res.lower()
    # 不相关 query
    assert "No memories matched" in mt.run({"action": "recall", "query": "zzzznotarealword"})


def test_update_preserves_name_type():
    mt.run({"action": "save", "name": "upd target", "type": "project",
            "description": "old desc", "content": "old body"})
    fn = [e.filename for e in list_memories() if e.name == "upd target"][0]
    res = mt.run({"action": "update", "filename": fn, "content": "new body"})
    assert "Updated" in res
    body = mt.run({"action": "read", "filename": fn})
    assert "new body" in body and "old body" not in body


def test_update_unknown():
    assert "Unknown memory" in mt.run({"action": "update", "filename": "nope.md", "content": "x"})


def test_delete_archives_not_hard_delete():
    mt.run({"action": "save", "name": "to delete", "type": "project",
            "description": "d", "content": "bye"})
    fn = [e.filename for e in list_memories() if e.name == "to delete"][0]
    res = mt.run({"action": "delete", "filename": fn})
    assert "Archived" in res
    # 原目录已无该文件
    assert not (get_memory_dir() / fn).exists()
    # 归档区有它
    archive = get_memory_dir() / "_archive"
    assert archive.is_dir() and any(fn in p.name for p in archive.iterdir())
    # 索引不再含它
    assert fn not in mt.run({"action": "list"})


def test_delete_unknown():
    assert "Unknown memory" in mt.run({"action": "delete", "filename": "nope.md"})


def test_unknown_action():
    assert "Unknown memory action" in mt.run({"action": "frobnicate"})


def test_semantic_recall_via_engine_falls_back_without_client(monkeypatch):
    # engine 拦截语义档；无可用 side_query 时回退关键词档
    from nanocode.agent.engine import Agent
    a = Agent(api_key="test", permission_mode="bypassPermissions")
    mt.run({"action": "save", "name": "semantic mem", "type": "project",
            "description": "vector stuff", "content": "embeddings here"})
    # 强制 side_query 为 None → 回退关键词
    monkeypatch.setattr(a, "_build_side_query", lambda: None)
    res = asyncio.run(a._execute_tool_call("memory", {"action": "recall", "query": "embeddings", "semantic": True}))
    assert "embeddings" in res.lower() or "semantic mem" in res.lower()


def test_semantic_recall_falls_back_to_keyword_on_empty_llm(monkeypatch):
    # 语义档 LLM 返回空（故障或无选中）→ 回退关键词档而非静默返回空
    from nanocode.agent.engine import Agent
    a = Agent(api_key="test", permission_mode="bypassPermissions")
    mt.run({"action": "save", "name": "kafka note", "type": "project",
            "description": "use kafka for events", "content": "kafka topic config"})

    async def _empty(*args, **kwargs):
        return []  # 模拟 select_relevant_memories 吞异常返回空
    monkeypatch.setattr(a, "_build_side_query", lambda: (lambda s, u: ""))
    monkeypatch.setattr("nanocode.memory.select_relevant_memories", _empty)
    res = asyncio.run(a._execute_tool_call("memory", {"action": "recall", "query": "kafka", "semantic": True}))
    # 关键词档应命中 kafka，而不是返回 "No memories matched"
    assert "kafka" in res.lower()
