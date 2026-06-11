"""P2: session/v2 agent_dir helper + artifact writers (meta/prompt/result/wire)."""

import json

from nanocode.session import v2


def test_agent_dir_path_shape():
    d = v2.agent_dir("sA", "agent-001")
    assert d.is_dir()
    assert d.name == "agent-001"
    assert d.parent.name == "agents"
    assert d.parent.parent.name == "sA"
    # 与 messages.json 同一目录（一条代码路径）。
    assert v2.session_root("sA") / "agents" / "agent-001" == d


def test_messages_use_agent_dir_roundtrip():
    v2.write_agent_messages("sB", "agent-001", [{"role": "user", "content": "x"}])
    # 物理上落在 agent_dir 下
    p = v2.agent_dir("sB", "agent-001") / "messages.json"
    assert p.exists()
    assert v2.read_agent_messages("sB", "agent-001")[0]["content"] == "x"


def test_write_and_read_agent_meta():
    assert v2.read_agent_meta("sC", "agent-001") is None
    v2.write_agent_meta("sC", "agent-001", {"id": "agent-001", "status": "running"})
    meta = v2.read_agent_meta("sC", "agent-001")
    assert meta["status"] == "running"
    p = v2.agent_dir("sC", "agent-001") / "meta.json"
    assert json.loads(p.read_text())["id"] == "agent-001"


def test_write_agent_prompt():
    v2.write_agent_prompt("sD", "agent-001", "do the thing")
    p = v2.agent_dir("sD", "agent-001") / "prompt.txt"
    assert p.read_text(encoding="utf-8") == "do the thing"


def test_write_agent_result_returns_path():
    path = v2.write_agent_result("sE", "agent-001", "# final\nbody")
    assert path.endswith("result.md")
    assert "agent-001" in path
    p = v2.agent_dir("sE", "agent-001") / "result.md"
    assert p.read_text(encoding="utf-8") == "# final\nbody"
