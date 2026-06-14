"""runtime/rebind.py — runtime-owned Agent session replacement helpers."""

from __future__ import annotations

import os
import time

from ..agent.events import NoticeRaised
from ..agent.subagent_manager import SubAgentManager
from ..session import v2 as _session_v2
from ..skills.discovery import reset_skill_cache
from ..tasks.manager import TaskManager
from ..tasks.models import TERMINAL_TASK_STATUSES


def _reload_task_state(agent, state) -> None:
    """Load derived v2 task state and mark non-terminal records as lost."""
    if not (state and isinstance(state, dict)):
        return
    agent.task_manager.load_state(state)
    for t in agent.task_manager.list_tasks():
        if t.status not in TERMINAL_TASK_STATUSES:
            agent.task_manager.update_task(t.id, status="lost")
    for a in agent.task_manager.list_subagents():
        if a.status in ("running", "idle"):
            agent.task_manager.update_subagent(a.id, status="lost")


def _reset_working_sets(agent) -> None:
    """Reset session-scoped working sets during a main-agent rebind."""
    agent._sent_skill_names = set()
    agent._pending_skill_bodies = []
    agent._activated_path_skills = set()
    agent._active_hooks = []
    agent._confirmed_paths.clear()
    agent._read_file_state = {}
    agent._files_read = set()
    agent._files_modified = set()
    agent._already_surfaced_memories = set()
    agent._session_memory_bytes = 0
    reset_skill_cache()


def _reset_session_mode(agent) -> None:
    """Reset permission/plan state to the construction-time baseline."""
    agent.permission_mode = agent._base_permission_mode
    agent._pre_plan_mode = None
    agent._pending_context_break = False
    agent._apply_permission_mode_prompt()


def rebind_agent_session(agent, new_mgr, *, artifact_id: str = "main") -> None:
    """Rebind a main Agent instance to a runtime-owned locked SessionManager."""
    if agent.is_sub_agent:
        raise RuntimeError("rebind_session is for the main agent only")
    new_sid = new_mgr.session_id
    if new_sid == agent.session_id:
        return
    old_sid = agent.session_id
    built = new_mgr.build_context()

    old_mgr = agent._session_mgr
    agent.agent_session.auto_save()
    if old_mgr is not None and old_mgr is not new_mgr:
        old_mgr.close()
    try:
        from ..tools.sandbox_shell import cleanup_persist_sandbox
        cleanup_persist_sandbox(old_sid)
    except Exception:
        pass

    agent.session_id = new_sid
    agent._tree_session_id = new_sid
    agent.artifact_id = artifact_id
    os.environ["NANOCODE_SESSION_ID"] = new_sid
    agent._session_mgr = new_mgr
    agent.session_start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    agent.task_manager = TaskManager()
    agent._subagents = SubAgentManager(agent)
    _reload_task_state(agent, _session_v2.read_state(new_sid) if _session_v2.is_v2_session(new_sid) else None)
    agent.total_input_tokens = 0
    agent.total_output_tokens = 0
    agent.last_input_token_count = 0
    agent.current_turns = 0
    agent._aborted = False
    _reset_working_sets(agent)
    _reset_session_mode(agent)
    agent.emit(NoticeRaised(text=f"Session → {new_sid} ({len(built.messages)} messages)."))
