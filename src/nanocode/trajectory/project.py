"""trajectory.project — merged wire 事件 -> Step 投影（docs/10 P2）。

DERIVED 只读投影：把 per-agent wire 的 flat-additive 事件配对成
``observation -> action -> result -> next_state`` 的 step 序列，供分析 / 复盘 /
agentic-RL dataset 导出。

硬边界（用户强制）：
- 本模块**只读** merged wire（经 ``events.reader.merge_session_events``），绝不写回 wire。
- 绝不驱动 runtime、绝不参与 resume / fork。
- 仅 import ``events`` 读侧（reader/models）、``trace.redaction.truncate``（纯叶子 helper）
  与 ``trajectory.schema``；**绝不** import 任何 runtime 模块。
- eval_result / reward 在此恒为 None——它们是派生标签，由后续 eval pipeline 回填，
  绝不在投影里产生。

健壮性（docs/10「P2 验收 / 兼容性」）：legacy flat 行、malformed/缺字段行、summary-mode
事件（无完整 messages/result，只有 *_chars / *_hash / *_summary）都**绝不**使投影崩溃——
降级为摘要字段或空串。
"""
from __future__ import annotations

import json

from ..events.models import SessionEvent
from ..events.reader import merge_session_events
from ..trace.redaction import truncate
from . import schema
from .schema import Step

# 风险启发（docs/10 risk_level）：中危工具（写盘 / 执行 shell）。
_MEDIUM_RISK_TOOLS = frozenset({"write_file", "edit_file", "run_shell", "sandbox_shell"})
# 高危：执行 shell 类工具 + 危险命令模式时升到 high。
_SHELL_TOOLS = frozenset({"run_shell", "sandbox_shell"})
# 危险命令子串启发（粗粒度，宁可多标 high；这是只读分析标签，不影响 runtime）。
_DANGEROUS_CMD_SUBSTRINGS = (
    "rm -rf", "rm -fr", "rm  -rf", ":(){", "mkfs", "dd if=", "> /dev/",
    "chmod -r 777", "chmod 777", "curl ", "wget ", "sudo ", "shutdown",
    "reboot", "kill -9", "git push --force", "git push -f", "force-push",
    "drop table", "drop database", "truncate table",
)


def project_session(session_id: str) -> list[Step]:
    """读取某 session 的 merged wire 并投影为 Step 列表。

    = ``build_steps(merge_session_events(session_id))``。读侧入口，绝不写回。
    """
    return build_steps(merge_session_events(session_id))


def build_steps(events: "list[SessionEvent]") -> list[Step]:
    """把一段 merged SessionEvent 投影为 Step 列表（纯函数，绝不抛）。

    配对规则（docs/10 step model）：
    - tool_action：``tool_call`` 与同 agent_id / 同 tool_use_id 的 ``tool_result`` 配对。
    - llm_decision：``llm_request -> assistant_message -> llm_response``（token 取自 response）。
    - final：``turn_end`` / ``session_end``，或 tool_uses 为空的 ``assistant_message``（done=True）。

    parent_step_id = 同 (agent_id, branch_id) 链上前一个 step 的 step_id；step_id =
    ``step_{agent_id}_{seq}``。seq 取触发该 step 的「锚事件」的 wire seq。**按 (agent, branch)
    串链**——fork 分支不与主链相互压扁（审阅 HIGH）；分支首 step 尽力接到 fork 点的 step
    （该分支首事件的 parent_event_id 指向 fork 事件，经 event_to_step 解析）。
    """
    steps: list[Step] = []
    try:
        # (agent_id, branch_id) -> 该分支链上一个 step_id（按分支隔离，fork 不压扁）。
        last_step_by_branch: dict[tuple, "str | None"] = {}
        # 锚事件 id -> step_id：供分支首 step 解析其 fork 点（parent_event_id）所在的 step。
        event_to_step: dict[str, str] = {}
        # branch_id -> 该分支首事件的 parent_event_id（fork 点 event id）；main 无、记 None。
        branch_first_parent = _collect_branch_fork_points(events)

        # 预扫：permission deny 集合（按 agent_id -> 被拒 tool 名集合），供风险启发。
        denied_tools = _collect_denied_tools(events)

        # 索引便于配对：按 (agent_id, tool_use_id) 找 tool_result；按 agent 顺序找 assistant/llm_response。
        tool_results = _index_tool_results(events)

        # 待消费的 llm_request（按 agent_id 暂存，等其后的 assistant_message + llm_response）。
        pending_llm: dict[str, SessionEvent] = {}

        def _emit(step: Step, anchor: "SessionEvent | None", ev: SessionEvent) -> None:
            step.branch_id = _safe_branch(ev)
            _link(step, last_step_by_branch, event_to_step, branch_first_parent, anchor)
            steps.append(step)

        for i, ev in enumerate(events):
            etype = _safe_type(ev)
            agent = _safe_agent(ev)

            if etype == "llm_request":
                # 暂存，等同 agent 的 assistant_message / llm_response 闭合为 llm_decision。
                pending_llm[agent] = ev
                continue

            if etype == "assistant_message":
                req = pending_llm.pop(agent, None)
                resp = _find_next_llm_response(events, i, agent)
                tool_uses = _safe_list(_data(ev).get("tool_uses"))
                step = _make_llm_decision(req, ev, resp, agent, denied=False)
                _emit(step, req if req is not None else ev, ev)
                # tool_uses 为空 = 模型给出最终答复，无后续工具轮 -> final（回合级，done=False）。
                if not tool_uses:
                    fin = _make_final_from_assistant(ev, agent)
                    _emit(fin, ev, ev)
                continue

            if etype == "tool_call":
                tu_id = _data(ev).get("tool_use_id")
                tool = _data(ev).get("tool")
                result_ev = tool_results.get((agent, tu_id)) if tu_id is not None else None
                obs = _preceding_assistant_summary(events, i, agent)
                step = _make_tool_action(
                    ev, result_ev, agent, tool=tool, obs=obs, denied_tools=denied_tools)
                _emit(step, ev, ev)
                continue

            if etype in ("turn_end", "session_end"):
                fin = _make_final_terminal(ev, agent, etype)
                _emit(fin, ev, ev)
                continue
    except Exception:
        # instrumentation/投影绝不抛入调用方；尽量返回已成功投影的 step。
        return steps
    return steps


# ── 配对/索引 helper ──────────────────────────────────────────


def _index_tool_results(events: "list[SessionEvent]") -> dict:
    """(agent_id, tool_use_id) -> 首个匹配的 tool_result 事件。"""
    out: dict = {}
    for ev in events:
        try:
            if _safe_type(ev) != "tool_result":
                continue
            tu_id = _data(ev).get("tool_use_id")
            key = (_safe_agent(ev), tu_id)
            if tu_id is not None and key not in out:
                out[key] = ev
        except Exception:
            continue
    return out


def _collect_denied_tools(events: "list[SessionEvent]") -> dict:
    """agent_id -> {被 permission_decision deny 的 tool 名}。供 high-risk 启发。"""
    out: dict[str, set] = {}
    for ev in events:
        try:
            if _safe_type(ev) != "permission_decision":
                continue
            if _data(ev).get("action") == "deny":
                tool = _data(ev).get("tool")
                if tool:
                    out.setdefault(_safe_agent(ev), set()).add(tool)
        except Exception:
            continue
    return out


def _find_next_llm_response(events, idx: int, agent: str) -> "SessionEvent | None":
    """从 idx 之后找同 agent 的下一个 llm_response（在遇到下一个 llm_request 前）。"""
    for j in range(idx + 1, len(events)):
        ev = events[j]
        if _safe_agent(ev) != agent:
            continue
        t = _safe_type(ev)
        if t == "llm_response":
            return ev
        if t == "llm_request":
            break  # 已进入下一轮，本轮无 response
    return None


def _preceding_assistant_summary(events, idx: int, agent: str) -> str:
    """tool_call 之前最近的同 agent assistant_message 文本（observation）。"""
    for j in range(idx - 1, -1, -1):
        ev = events[j]
        if _safe_agent(ev) != agent:
            continue
        if _safe_type(ev) == "assistant_message":
            return truncate(_safe_str(_data(ev).get("text")))
        if _safe_type(ev) == "tool_result":
            continue  # 同轮的其它工具结果，继续向上找 assistant
    return ""


# ── step 构造 helper ──────────────────────────────────────────


def _make_tool_action(
    call_ev: SessionEvent, result_ev: "SessionEvent | None", agent: str, *,
    tool, obs: str, denied_tools: dict,
) -> Step:
    """tool_call (+ 配对 tool_result) -> tool_action step。"""
    tool_name = _safe_str(tool) if tool is not None else ""
    args_summary = _args_summary(_data(call_ev))
    result_summary = _result_summary(result_ev)
    latency = _latency_ms(_ts(call_ev), _ts(result_ev)) if result_ev is not None else None
    risk = _risk_level(tool_name, _data(call_ev), agent, denied_tools)
    return Step(
        trajectory_id=_traj_id(call_ev),
        episode_id=_episode_id(call_ev),
        step_id=schema.step_id(agent, _seq(call_ev)),
        parent_step_id=None,
        turn_id=_turn(call_ev),
        agent_id=agent,
        step_type="tool_action",
        observation_summary=obs,
        action={"type": "tool_call", "tool": tool_name, "args_summary": args_summary},
        result_summary=result_summary,
        next_state_summary=truncate(result_summary, 200),
        latency_ms=latency,
        risk_level=risk,
        done=False,
    )


def _make_llm_decision(
    req_ev: "SessionEvent | None", asst_ev: SessionEvent,
    resp_ev: "SessionEvent | None", agent: str, *, denied: bool,
) -> Step:
    """llm_request -> assistant_message -> llm_response -> llm_decision step。

    锚 seq 取 llm_request（若缺则取 assistant_message）。token / latency 取自 llm_response。
    """
    anchor = req_ev if req_ev is not None else asst_ev
    in_tok = out_tok = 0
    if resp_ev is not None:
        in_tok = _as_int(_data(resp_ev).get("input_tokens"))
        out_tok = _as_int(_data(resp_ev).get("output_tokens"))
    latency = None
    if req_ev is not None and resp_ev is not None:
        latency = _latency_ms(_ts(req_ev), _ts(resp_ev))
    obs = _request_observation(req_ev)
    asst_text = _safe_str(_data(asst_ev).get("text"))
    tool_uses = _safe_list(_data(asst_ev).get("tool_uses"))
    return Step(
        trajectory_id=_traj_id(anchor),
        episode_id=_episode_id(anchor),
        step_id=schema.step_id(agent, _seq(anchor)),
        parent_step_id=None,
        turn_id=_turn(anchor),
        agent_id=agent,
        step_type="llm_decision",
        observation_summary=obs,
        action={
            "type": "assistant",
            "text_summary": truncate(asst_text, 500),
            "n_tool_uses": len(tool_uses),
        },
        result_summary=truncate(asst_text, 500),
        next_state_summary="",
        latency_ms=latency,
        input_tokens=in_tok,
        output_tokens=out_tok,
        risk_level="low",
        done=False,
    )


def _make_final_from_assistant(asst_ev: SessionEvent, agent: str) -> Step:
    """tool_uses 为空的 assistant_message -> 该 turn 收尾的 final step。

    episode = session（``episode_id = session_id``，docs/10），故**回合级**最终答复 ``done=False``——
    一个 episode 只应在 session_end 出现一次 ``done=True``（审阅 LOW：避免每个 turn 都产一个
    done=True，迷惑 episode 级 RL 消费方）。
    """
    text = _safe_str(_data(asst_ev).get("text"))
    return Step(
        trajectory_id=_traj_id(asst_ev),
        episode_id=_episode_id(asst_ev),
        step_id=schema.step_id(agent, _seq(asst_ev)) + "_final",
        parent_step_id=None,
        turn_id=_turn(asst_ev),
        agent_id=agent,
        step_type="final",
        observation_summary="",
        action={"type": "final"},
        result_summary=truncate(text, 500),
        next_state_summary="",
        risk_level="low",
        done=False,
    )


def _make_final_terminal(ev: SessionEvent, agent: str, etype: str) -> Step:
    """turn_end / session_end -> final step。

    - ``done`` 仅 ``session_end`` 为 True（episode=session 的唯一终止；turn_end 是回合边界，
      done=False）——审阅 LOW：一个 episode 只应有一个 done=True。
    - **不**把 turn_end/session_end 的 token 写进 step：这两类事件携带的是 agent 的**累计**
      totals（engine 的 turn_end / cli 的 session_end 都发累计值），而 llm_decision step 已逐次
      记了 per-call token；若 final step 再带累计值，跨 steps.jsonl 累加 per-step token 会把整段
      session 的成本重复摊到终止步（审阅 MEDIUM 成本误归属）。累计 totals 只留在
      metrics.json / TrajectoryMetadata。终止标记本身无增量动作成本，故 token=0。
    """
    return Step(
        trajectory_id=_traj_id(ev),
        episode_id=_episode_id(ev),
        step_id=schema.step_id(agent, _seq(ev)),
        parent_step_id=None,
        turn_id=_turn(ev),
        agent_id=agent,
        step_type="final",
        observation_summary="",
        action={"type": etype},
        result_summary="",
        next_state_summary="",
        input_tokens=0,
        output_tokens=0,
        risk_level="low",
        done=(etype == "session_end"),
    )


def _link(step: Step, last_step_by_branch: dict, event_to_step: dict,
          branch_first_parent: dict, anchor_ev: "SessionEvent | None") -> None:
    """把 step 接到 **(agent_id, branch_id)** 链：parent_step_id = 该分支链上一个 step_id；
    分支首 step 尽力接到 fork 点的 step（该分支首事件 parent_event_id 指向的事件所投影的 step），
    解析不到则 None。更新链尾，并把锚事件 id -> step_id 记入 event_to_step（供后续分支解析 fork）。

    按分支隔离避免 fork 被压扁（审阅 HIGH）：原先按 agent 串链会把不同分支的 step 串成一条线、
    且丢失 branch 身份。
    """
    key = (step.agent_id, step.branch_id)
    if key in last_step_by_branch:
        step.parent_step_id = last_step_by_branch[key]
    else:
        fork_evt = branch_first_parent.get(step.branch_id)
        step.parent_step_id = event_to_step.get(fork_evt) if fork_evt else None
    last_step_by_branch[key] = step.step_id
    ev_id = getattr(anchor_ev, "id", None) if anchor_ev is not None else None
    if ev_id:
        event_to_step[ev_id] = step.step_id


def _collect_branch_fork_points(events: "list[SessionEvent]") -> dict:
    """branch_id -> 该分支**首事件**的 parent_event_id（fork 点 event id）。

    main 分支首事件无 parent_event_id（不记）；fork 出的分支首事件由 Tracer.begin_branch
    打上 parent_event_id（指向 fork 点），据此让该分支首 step 接到 fork 点所在的 step。
    按事件出现序取每个 branch 的首个事件。绝不抛。
    """
    out: dict[str, str] = {}
    seen: set = set()
    for ev in events:
        try:
            b = _safe_branch(ev)
            if b in seen:
                continue
            seen.add(b)
            pev = getattr(ev, "parent_event_id", None)
            if pev:
                out[b] = _safe_str(pev)
        except Exception:
            continue
    return out


# ── 字段提取 / 摘要 helper（全 defensive，绝不抛）───────────────


def _args_summary(d: dict) -> str:
    """工具入参摘要：full 级有 ``input`` 时序列化截断；summary 级只有 hash 时返回占位。"""
    if "input" in d:
        val = d.get("input")
        try:
            serialized = val if isinstance(val, str) else json.dumps(
                val, ensure_ascii=False, default=str)
        except Exception:
            serialized = _safe_str(val)
        return truncate(serialized, 500)
    # summary-mode tool_call 可能只留摘要/hash 字段。
    if d.get("args_summary"):
        return truncate(_safe_str(d.get("args_summary")), 500)
    if d.get("input_hash") or d.get("args_hash"):
        return "(summarized)"
    return ""


def _result_summary(result_ev: "SessionEvent | None") -> str:
    """tool_result 摘要：full 级有 ``result`` 时截断；summary 级取 ``result_summary``/hash。"""
    if result_ev is None:
        return ""
    d = _data(result_ev)
    if "result" in d:
        return truncate(_safe_str(d.get("result")))
    if d.get("result_summary"):
        return truncate(_safe_str(d.get("result_summary")))
    if d.get("result_hash"):
        return "(summarized)"
    return ""


def _request_observation(req_ev: "SessionEvent | None") -> str:
    """llm_request 的 observation 摘要：message_count + messages_chars（summary 级）。"""
    if req_ev is None:
        return ""
    d = _data(req_ev)
    parts = []
    if d.get("message_count") is not None:
        parts.append(f"messages={d.get('message_count')}")
    if d.get("messages_chars") is not None:
        parts.append(f"chars={d.get('messages_chars')}")
    elif "messages" in d:
        try:
            parts.append(f"chars={len(json.dumps(d['messages'], ensure_ascii=False, default=str))}")
        except Exception:
            pass
    return " ".join(parts)


def _risk_level(tool_name: str, call_data: dict, agent: str, denied_tools: dict) -> str:
    """风险启发：deny / 危险 shell 命令 -> high；写盘/执行类 -> medium；其余 low。"""
    try:
        if tool_name and tool_name in denied_tools.get(agent, set()):
            return "high"
        if tool_name in _SHELL_TOOLS and _looks_dangerous(call_data):
            return "high"
        if tool_name in _MEDIUM_RISK_TOOLS:
            return "medium"
    except Exception:
        return "low"
    return "low"


def _looks_dangerous(call_data: dict) -> bool:
    """从 tool_call 入参里粗略嗅探危险 shell 命令。"""
    try:
        inp = call_data.get("input")
        text = ""
        if isinstance(inp, str):
            text = inp
        elif isinstance(inp, dict):
            # run_shell/sandbox_shell 的命令多在 command / cmd / script 键。
            for k in ("command", "cmd", "script", "args"):
                v = inp.get(k)
                if v:
                    text += " " + (v if isinstance(v, str) else _safe_str(v))
        else:
            text = call_data.get("args_summary") or ""
        low = text.lower()
        return any(sub in low for sub in _DANGEROUS_CMD_SUBSTRINGS)
    except Exception:
        return False


def _latency_ms(start_ts: str, end_ts: str) -> "int | None":
    """两个 ISO8601 ts 的毫秒差；解析失败返回 None。"""
    try:
        s = _parse_ts(start_ts)
        e = _parse_ts(end_ts)
        if s is None or e is None:
            return None
        delta = (e - s).total_seconds() * 1000.0
        if delta < 0:
            return None
        return int(delta)
    except Exception:
        return None


def _parse_ts(ts: str):
    from datetime import datetime

    if not ts or not isinstance(ts, str):
        return None
    raw = ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


# ── SessionEvent 字段安全读取（兼容 legacy / 缺字段）──────────


def _data(ev: SessionEvent) -> dict:
    d = getattr(ev, "data", None)
    return d if isinstance(d, dict) else {}


def _safe_type(ev: SessionEvent) -> str:
    return _safe_str(getattr(ev, "type", ""))


def _safe_agent(ev: SessionEvent) -> str:
    a = getattr(ev, "agent_id", "")
    return _safe_str(a) if a else "main"


def _safe_branch(ev: SessionEvent) -> str:
    b = getattr(ev, "branch_id", "")
    return _safe_str(b) if b else "main"


def _seq(ev: SessionEvent) -> int:
    return _as_int(getattr(ev, "seq", 0))


def _ts(ev: "SessionEvent | None") -> str:
    if ev is None:
        return ""
    return _safe_str(getattr(ev, "ts", ""))


def _turn(ev: SessionEvent) -> "str | None":
    return getattr(ev, "turn_id", None)


def _episode_id(ev: SessionEvent) -> str:
    return _safe_str(getattr(ev, "session_id", ""))


def _traj_id(ev: SessionEvent) -> str:
    """trajectory_id：优先取 wire envelope 注入的 ``trajectory_id``，否则 ``traj_<session_id>``。"""
    tid = _data(ev).get("trajectory_id")
    if tid:
        return _safe_str(tid)
    sid = _episode_id(ev)
    return f"traj_{sid}" if sid else "traj_"


def _safe_str(val) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    try:
        return str(val)
    except Exception:
        return ""


def _safe_list(val) -> list:
    return val if isinstance(val, list) else []


def _as_int(val) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0
