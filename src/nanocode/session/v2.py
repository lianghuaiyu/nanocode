"""Session v2 目录存储：derived state.json cache + per-agent artifacts（meta/prompt/result）+ task 目录。
会话历史的唯一权威是 canonical session.jsonl 树（docs/14）；本模块不存任何 messages 副本（docs/16 C-1/C-3）。"""
from __future__ import annotations

import json
from pathlib import Path

from ..paths import sessions_dir


def session_root(session_id: str) -> Path:
    return sessions_dir() / session_id


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def is_v2_session(session_id: str) -> bool:
    return (session_root(session_id) / "state.json").exists()


def write_state(session_id: str, state: dict) -> None:
    _write_json(session_root(session_id) / "state.json", state)


def read_state(session_id: str) -> dict | None:
    p = session_root(session_id) / "state.json"
    return _read_json(p, None) if p.exists() else None


def agent_dir(session_id: str, agent_id: str) -> Path:
    """每个 agent 的 artifact 主目录 = <session>/agents/<agent_id>/。

    主 agent 用 agent_id="main"；子 agent 用其 SubAgentRecord id（如 "agent-001"）。
    meta.json / prompt.txt / result.md 全部落在此目录下，
    保证「同一 agent 的全部产物自包含于一处」。
    """
    d = session_root(session_id) / "agents" / agent_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# 一个 agent 的标准 artifact 文件名（label -> filename）。集中此处的路径 grammar，
# 避免上层（tasks_tool 等）手拼 'agents'/<id>/<filename>（PERSIST-P1 的唯一真实去散点）。
AGENT_ARTIFACT_FILES = (("Result", "result.md"),
                        ("Meta", "meta.json"), ("Prompt", "prompt.txt"))


def agent_artifact_paths(session_id: str, agent_id: str) -> list[tuple[str, Path]]:
    """枚举一个 agent 的标准 artifact 路径 (label, path)，**不创建 agent 目录**（与
    agent_dir() 的 mkdir `<session>/agents/<id>` 副作用相反；只读用途）。注：session_root()
    仍可能确保顶层 sessions/ 目录存在——与旧 tasks_tool 手拼路径的行为一致。调用方按 .exists() 过滤。"""
    base = session_root(session_id) / "agents" / agent_id
    return [(label, base / fname) for label, fname in AGENT_ARTIFACT_FILES]


def write_agent_meta(session_id: str, agent_id: str, meta: dict) -> None:
    """写 <agent_dir>/meta.json（spawn 时 status=running，完成时补 status/ended_at）。"""
    _write_json(agent_dir(session_id, agent_id) / "meta.json", meta)


def read_agent_meta(session_id: str, agent_id: str) -> dict | None:
    p = agent_dir(session_id, agent_id) / "meta.json"
    return _read_json(p, None) if p.exists() else None


def write_agent_prompt(session_id: str, agent_id: str, prompt: str) -> None:
    """写 <agent_dir>/prompt.txt（spawn 时落子 agent 的任务 prompt）。"""
    (agent_dir(session_id, agent_id) / "prompt.txt").write_text(prompt or "", encoding="utf-8")


def write_agent_result(session_id: str, agent_id: str, text: str) -> str:
    """写 <agent_dir>/result.md（完成时落最终 assistant 文本），返回路径字符串。"""
    p = agent_dir(session_id, agent_id) / "result.md"
    p.write_text(text or "", encoding="utf-8")
    return str(p)


def task_dir(session_id: str, task_id: str) -> Path:
    d = session_root(session_id) / "tasks" / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d
