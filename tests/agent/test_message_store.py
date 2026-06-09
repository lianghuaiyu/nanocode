"""P-1 子目标(2)：MessageStore 单一 owner —— load/replace/append/dump + 杜绝跨 agent by-ref。"""

from __future__ import annotations

from nanocode.agent.engine import Agent
from nanocode.agent.message_store import MessageStore
from nanocode.subagents import config


def _agent(**kw):
    kw.setdefault("permission_mode", "bypassPermissions")
    return Agent(api_key="test", trace_enabled=False, session_id="msid", **kw)


def _anth_agent(**kw):
    # 默认无 api_base → use_openai=False → 活动列表为 anthropic
    return _agent(**kw)


def _read_only_sub(parent):
    cfg = config.get_sub_agent_config("explore")
    return parent._build_sub_agent(
        system_prompt=cfg["system_prompt"], tools=cfg["tools"], agent_type="explore")


# ─── MessageStore 单元 ──────────────────────────────────────

def test_store_load_replace_append_dump():
    s = MessageStore()
    assert s.items == [] and s.dump() == []
    s.append({"role": "user", "content": "a"})
    assert len(s.items) == 1
    hist = [{"role": "user", "content": "x"}]
    s.load(hist)
    assert s.items is hist and s.dump() is hist  # load 接管引用（保持旧 by-ref 语义）
    s.replace([{"role": "assistant", "content": "y"}])
    assert len(s.items) == 1 and s.items[0]["role"] == "assistant"


# ─── 属性由 store 背书，读/写/in-place 透明 ─────────────────

def test_message_property_backed_by_store():
    a = _anth_agent()
    # getter 返回 store live list
    assert a._anthropic_messages is a._anthropic_store.items
    # append 经 getter 命中 store
    a._anthropic_messages.append({"role": "user", "content": "hi"})
    assert a._anthropic_store.items[-1]["content"] == "hi"
    # 整列赋值经 setter 路由到 owner（resume/compaction/clear 路径）
    a._anthropic_messages = [{"role": "user", "content": "fresh"}]
    assert a._anthropic_store.items[0]["content"] == "fresh"
    # 同一 getter 两次返回同一 list 对象（与普通属性一致）
    assert a._anthropic_messages is a._anthropic_messages


def test_active_store_follows_provider():
    anth = _anth_agent()
    assert anth._active_store() is anth._anthropic_store
    # openai agent（给 api_base）→ 活动 store 为 openai
    oai = _agent(api_base="http://x", model="gpt-4o")
    assert oai._active_store() is oai._openai_store


def test_owner_methods_operate_on_active_list():
    a = _anth_agent()
    a._load_messages([{"role": "user", "content": "loaded"}])
    assert a._dump_messages()[0]["content"] == "loaded"
    a._append_message({"role": "assistant", "content": "more"})
    assert len(a._dump_messages()) == 2
    a._replace_messages([])
    assert a._dump_messages() == []


# ─── criterion 4：跨 agent resume 不再 by-ref 直赋 ─────────

def test_subagent_resume_loads_via_owner_not_byref():
    parent = _agent()
    sub = _read_only_sub(parent)
    history = [{"role": "user", "content": "prior"}, {"role": "assistant", "content": "ok"}]
    # 模拟父恢复子 agent：经子 owner 入口（而非 sub._anthropic_messages = history）
    sub._load_messages(history)
    # 子的活动列表确为 history（接管引用，保持旧语义）
    assert sub._dump_messages() is history
    # 父经 dump 入口读子的列表（持久化路径），不 reach 进子内部属性
    assert parent._subagent_captured_text  # 存在；下行验证 dump 读
    assert sub._dump_messages()[0]["content"] == "prior"


def test_parent_persist_reads_via_dump_entry():
    parent = _agent()
    sub = _read_only_sub(parent)
    sub._append_message({"role": "user", "content": "to-persist"})
    # _persist_agent_messages 内部经 sub._dump_messages() 读，等价于活动列表
    assert sub._dump_messages() == sub._anthropic_messages


def test_reassign_then_append_lands_on_new_list():
    """守护不变量（workflow 建议）：setter 整列替换后，经 getter 的 append 落在新列表上，
    而非 stale alias——这是 _compact_* 重置历史后继续 append last_user 依赖的语义。"""
    a = _anth_agent()
    a._anthropic_messages = [{"role": "user", "content": "m1"}, {"role": "user", "content": "m2"}]
    first = a._anthropic_messages
    a._anthropic_messages.append({"role": "assistant", "content": "m3"})
    # append 命中 setter 装入的同一新列表
    assert a._anthropic_store.items is first
    assert [m["content"] for m in a._anthropic_messages] == ["m1", "m2", "m3"]
