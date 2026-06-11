"""trajectory.eval — P4 在线启发式评估 / reward / failure attribution（DERIVED 派生标签）。

硬边界（用户强制，不可违反）：
- 本模块是对 canonical 树派生事件的**只读派生投影**：只读 ``list[TrajEvent]``（由
  ``_tree_events.tree_events`` 重建），产出 plain data（eval 记录 / 带 reward 的 step 副本 /
  attribution dict）。
- eval / reward 是**派生标签**：绝不写回树/wire、绝不调用任何 tracer/emit/sink，绝不污染
  execution-fact 事件源。reward 只该落 metrics.json / evals.jsonl（由 export 层负责），
  本模块只负责**计算**，不负责落盘。
- 不得 import 任何 runtime 模块（engine / backend / context_builder / session 写侧 / tracer /
  redaction 的写侧逻辑）、``events.*``、``trace.*``。允许：``_tree_events``（只读 session.manager/tree）、
  ``_text.truncate``（纯叶子 helper）、``trajectory.schema``。

容错铁律（docs/10「读侧应容忍」）：malformed/缺字段、summary-mode 事件（无 full messages/result，
仅 result_summary/result_hash/chars/messages_chars）都**绝不**得令投影崩溃——退化为 summary 字段
或空串。所有解析在 try 内兜底。

reward 是 **provisional（暂定）** 的有界启发式：tool_error / permission_denied /
budget_exceeded 等失败信号给负 reward；干净 step 留 ``None``（待 offline evaluator 或
human feedback 回填）。它不完美，仅够支撑失败聚类 / 轨迹筛选 / 行为克隆样本清洗。
"""
from __future__ import annotations

import re
from typing import Any

from ._text import truncate
from ._tree_events import TrajEvent

# Milestone B2：投影逻辑保留，仅换 source（wire → canonical 树适配器）。SessionEvent 名沿用为
# TrajEvent 的别名，最小化类型注解 diff。
SessionEvent = TrajEvent

# ─── 信号常量 ────────────────────────────────────────────────────────────────

# online_evals 产出的 signal 取值（docs/10「第一阶段可落地的低成本 reward 信号」）。
SIG_SHELL_EXIT_CODE = "shell_exit_code"
SIG_TESTS_PASS = "tests_pass"
SIG_TESTS_FAIL = "tests_fail"
SIG_TOOL_ERROR = "tool_error"
SIG_PERMISSION_DENIED = "permission_denied"
SIG_REACHED_FINAL_ANSWER = "reached_final_answer"
SIG_TOUCHED_FILE = "touched_file"
SIG_COMPACTION_OCCURRED = "compaction_occurred"
SIG_CONTEXT_OVERFLOW = "context_overflow"
SIG_BUDGET_EXCEEDED = "budget_exceeded"

# 负向（失败）信号：attach_rewards 据此给所在 step 负 reward；failure_attribution 据此定位首因。
_FAILURE_SIGNALS = frozenset({
    SIG_TOOL_ERROR,
    SIG_PERMISSION_DENIED,
    SIG_BUDGET_EXCEEDED,
    SIG_CONTEXT_OVERFLOW,
    SIG_TESTS_FAIL,
})

# reward 启发式（有界、暂定）：失败信号的惩罚值。
_REWARD_ON_FAILURE = -1.0

# 解析 shell 退出码：``run_shell`` 结果常以 ``(exit N)`` 收尾（N!=0 视为失败）。
_EXIT_RE = re.compile(r"\(exit\s+(-?\d+)\)")

# 启发式判定「这是一次 pytest/测试运行」的工具名 + 结果关键词。
_SHELL_TOOLS = frozenset({"run_shell", "shell", "bash"})
_TESTS_FAIL_RE = re.compile(r"\b(\d+)\s+failed\b", re.IGNORECASE)
_TESTS_PASS_RE = re.compile(r"\b(\d+)\s+passed\b", re.IGNORECASE)
_TESTS_ERROR_RE = re.compile(r"\b(\d+)\s+errors?\b", re.IGNORECASE)

# 触达文件的写类工具（docs/10「是否触达用户指定文件」）。
_WRITE_TOOLS = frozenset({"write_file", "edit_file", "write", "edit", "create_file"})


# ─── 小工具：健壮取值 ────────────────────────────────────────────────────────

def _data(ev: SessionEvent) -> dict:
    """安全取 ``ev.data``（恒 dict）。"""
    d = getattr(ev, "data", None)
    return d if isinstance(d, dict) else {}


def _result_text(d: dict) -> str:
    """从 tool_result 的 data 取结果文本：FULL 用 ``result``，SUMMARY 退化到 ``result_summary``。

    二者都可能缺（极端 summary / malformed）→ 退化为空串。绝不抛。
    """
    try:
        if "result" in d and d["result"] is not None:
            r = d["result"]
            return r if isinstance(r, str) else str(r)
        rs = d.get("result_summary")
        if rs is not None:
            return rs if isinstance(rs, str) else str(rs)
    except Exception:
        return ""
    return ""


def _snippet(text: str, n: int = 200) -> str:
    """复用 redaction.truncate 做短摘要（绝不抛）。"""
    try:
        return truncate(text, n)
    except Exception:
        return ""


def _eval_record(
    *,
    step_id: "str | None",
    turn_id: "str | None",
    agent_id: str,
    signal: str,
    value: Any,
    detail: str,
) -> dict:
    """统一 eval 记录形状：每个 signal 一条。"""
    return {
        "step_id": step_id,
        "turn_id": turn_id,
        "agent_id": agent_id,
        "signal": signal,
        "value": value,
        "detail": detail,
    }


def _is_error_result(text: str) -> bool:
    """工具结果是否表征错误：以 ``Error`` / ``Warning`` 开头（大小写不敏感、忽略前导空白）。"""
    try:
        s = text.lstrip()
    except Exception:
        return False
    low = s[:8].lower()
    return low.startswith("error") or low.startswith("warning")


def _is_block_result(text: str) -> bool:
    """该 tool_result 是否是 allowlist 拦截返回的「not permitted」错误文本。

    生产里被挡的工具会先 emit 一条 ``tool_blocked``（已产 tool_error eval），其配对
    ``tool_result`` 文本恒为 ``Error: tool '<name>' is not permitted ...``。据此识别并在
    tool_result 分支**跳过**重复的 tool_error，避免一次被挡产 2 条 tool_error（审阅 LOW 去重）。
    """
    try:
        low = text.lstrip().lower()
    except Exception:
        return False
    return low.startswith("error: tool ") and "not permitted" in low


def _as_int(value) -> "int | None":
    """容忍把任意值转 int；失败返回 None（用于 seq 比较，None 表示不可比）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _step_anchor_seq(step_id: "str | None") -> "int | None":
    """从 ``step_{agent}_{seq}[_final]`` 解析出锚 seq（agent_id 不含 ``_``，见 events.models）。

    ``step_main_3`` -> 3；``step_main_6_final`` -> 6；``step_agent-001_2`` -> 2。失败 -> None。
    """
    try:
        parts = (step_id or "").split("_")
        # parts[0]="step", parts[1]=agent_id（无下划线）, parts[2]=seq, [parts[3]="final"]
        return int(parts[2])
    except (IndexError, ValueError, TypeError):
        return None


def _step_id_for(ev: SessionEvent, steps: "list | None") -> "str | None":
    """把一条 event 关联到产生它的 step_id（若提供了 steps）。

    **按锚 seq 就近归属**（修审阅 HIGH 的 reward 误归属）：一个 turn 内所有事件共享同一
    ``turn_id``，若只按 (agent_id, turn_id) 取「最后一个匹配 step」，失败 tool_action 的
    tool_error 会被错记到该 turn 的 final step（最后一个），导致 attach_rewards 给**成功的
    最终答复**打负 reward，污染 RL 信号。正确做法：取同 agent（同 turn 若两侧都有）中
    锚 seq ``<=`` 本事件 seq 的**最大**锚 seq 的 step——即「触发本事件的那一步」（tool_result
    归到其 tool_call 锚的 tool_action；permission_decision 归到紧邻的 tool_action；
    无 tool_uses 的 assistant 归到该 assistant 锚的 final）。无 steps / 匹配不到 -> None。绝不抛。
    """
    if not steps:
        return None
    try:
        agent_id = getattr(ev, "agent_id", "") or "main"
        turn_id = getattr(ev, "turn_id", None)
        ev_seq = _as_int(getattr(ev, "seq", None))
        best_id = None
        best_seq = None
        for s in steps:
            s_id = getattr(s, "step_id", None)
            if s_id is None:
                continue
            s_agent = getattr(s, "agent_id", None) or "main"
            if s_agent != agent_id:
                continue
            if turn_id is not None:
                s_turn = getattr(s, "turn_id", None)
                if s_turn is not None and s_turn != turn_id:
                    continue
            anchor = _step_anchor_seq(s_id)
            if anchor is None:
                continue
            # 锚在本事件之后的 step 不可能「产生」本事件——排除（ev_seq 不可解析时放宽）。
            if ev_seq is not None and anchor > ev_seq:
                continue
            if best_seq is None or anchor > best_seq:
                best_seq = anchor
                best_id = s_id
        return best_id
    except Exception:
        return None


# ─── P4 API：online_evals ────────────────────────────────────────────────────

def online_evals(events: "list[SessionEvent]", steps: "list | None" = None) -> "list[dict]":
    """在线启发式评估：对每条出现的信号产出一条 eval 记录（plain dict）。

    一条记录形状：``{"step_id"|None, "turn_id"|None, "agent_id", "signal", "value", "detail"}``。
    signal 取值见模块顶部 ``SIG_*``。

    覆盖信号（docs/10）：
      - shell_exit_code：``run_shell`` 结果含 ``(exit N)``，N!=0 记一条（value=N）。
      - tests_pass / tests_fail：从 pytest 风格输出启发式解析（``K passed`` / ``K failed``）。
      - tool_error：tool_result 文本以 Error/Warning 开头，或 tool_blocked 事件。
      - permission_denied：permission_decision 且 action=="deny"。
      - reached_final_answer：assistant_message 无 tool_uses（该 turn 收尾，给出最终答复）。
      - touched_file：write/edit 类 tool_call 的 input.file_path。
      - compaction_occurred：compaction 事件。
      - context_overflow / budget_exceeded：budget_exceeded 事件（reason 含 context/overflow
        归为 context_overflow，否则 budget_exceeded）。

    容错：legacy / summary-mode / 缺字段 / malformed 都不崩——退化为 summary 字段或空串。
    """
    out: list[dict] = []
    if not events:
        return out

    for ev in events:
        try:
            etype = getattr(ev, "type", "") or ""
            agent_id = getattr(ev, "agent_id", "") or ""
            turn_id = getattr(ev, "turn_id", None)
            d = _data(ev)
            sid = _step_id_for(ev, steps)

            if etype == "tool_result":
                tool = d.get("tool") or ""
                text = _result_text(d)

                # tool_error：Error/Warning 开头。但若是 allowlist 拦截返回的
                # "Error: tool ... not permitted"，其配对的 tool_blocked 已产一条 tool_error，
                # 此处跳过以免一次被挡产 2 条 tool_error（审阅 LOW 去重）。
                if _is_error_result(text) and not _is_block_result(text):
                    out.append(_eval_record(
                        step_id=sid, turn_id=turn_id, agent_id=agent_id,
                        signal=SIG_TOOL_ERROR, value=True,
                        detail=_snippet(text),
                    ))

                # shell_exit_code：解析 (exit N)，非零记一条。
                if tool in _SHELL_TOOLS or "(exit" in text:
                    m = _EXIT_RE.search(text)
                    if m:
                        code = _as_int(m.group(1))
                        if code is not None and code != 0:
                            out.append(_eval_record(
                                step_id=sid, turn_id=turn_id, agent_id=agent_id,
                                signal=SIG_SHELL_EXIT_CODE, value=code,
                                detail=_snippet(text),
                            ))

                # tests_pass / tests_fail：pytest 风格启发式（仅 shell 工具的结果上判）。
                if tool in _SHELL_TOOLS:
                    fm = _TESTS_FAIL_RE.search(text)
                    em = _TESTS_ERROR_RE.search(text)
                    pm = _TESTS_PASS_RE.search(text)
                    n_fail = 0
                    if fm:
                        n_fail += _as_int(fm.group(1)) or 0
                    if em:
                        n_fail += _as_int(em.group(1)) or 0
                    if n_fail > 0:
                        out.append(_eval_record(
                            step_id=sid, turn_id=turn_id, agent_id=agent_id,
                            signal=SIG_TESTS_FAIL, value=n_fail,
                            detail=_snippet(text),
                        ))
                    elif pm:
                        n_pass = _as_int(pm.group(1)) or 0
                        if n_pass > 0:
                            out.append(_eval_record(
                                step_id=sid, turn_id=turn_id, agent_id=agent_id,
                                signal=SIG_TESTS_PASS, value=n_pass,
                                detail=_snippet(text),
                            ))

            elif etype == "tool_blocked":
                out.append(_eval_record(
                    step_id=sid, turn_id=turn_id, agent_id=agent_id,
                    signal=SIG_TOOL_ERROR, value=True,
                    detail=f"tool_blocked: {d.get('tool') or ''} ({d.get('reason') or ''})",
                ))

            elif etype == "permission_decision":
                if (d.get("action") or "") == "deny":
                    out.append(_eval_record(
                        step_id=sid, turn_id=turn_id, agent_id=agent_id,
                        signal=SIG_PERMISSION_DENIED, value=d.get("tool") or "",
                        detail=_snippet(d.get("message") or ""),
                    ))

            elif etype == "assistant_message":
                tool_uses = d.get("tool_uses")
                # 无 tool_uses（None / [] / 缺字段）= 该 turn 收尾给出最终答复。
                if not tool_uses:
                    out.append(_eval_record(
                        step_id=sid, turn_id=turn_id, agent_id=agent_id,
                        signal=SIG_REACHED_FINAL_ANSWER, value=True,
                        detail=_snippet(d.get("text") or ""),
                    ))

            elif etype == "tool_call":
                tool = d.get("tool") or ""
                if tool in _WRITE_TOOLS:
                    inp = d.get("input")
                    fp = ""
                    if isinstance(inp, dict):
                        fp = inp.get("file_path") or inp.get("path") or ""
                    out.append(_eval_record(
                        step_id=sid, turn_id=turn_id, agent_id=agent_id,
                        signal=SIG_TOUCHED_FILE, value=fp or True,
                        detail=f"{tool} {fp}".strip(),
                    ))

            elif etype == "compaction":
                out.append(_eval_record(
                    step_id=sid, turn_id=turn_id, agent_id=agent_id,
                    signal=SIG_COMPACTION_OCCURRED, value=d.get("kind") or True,
                    detail=f"before={d.get('message_count_before')} after={d.get('message_count_after')}",
                ))

            elif etype == "budget_exceeded":
                reason = d.get("reason") or ""
                low = reason.lower() if isinstance(reason, str) else ""
                if "context" in low or "overflow" in low:
                    signal = SIG_CONTEXT_OVERFLOW
                else:
                    signal = SIG_BUDGET_EXCEEDED
                out.append(_eval_record(
                    step_id=sid, turn_id=turn_id, agent_id=agent_id,
                    signal=signal, value=reason or True,
                    detail=_snippet(reason),
                ))
        except Exception:
            # 单条事件解析失败绝不拖垮整体投影。
            continue

    return out


def _is_failure_eval(ev: dict) -> bool:
    """一条 eval 是否表征失败（负向信号）：命中 ``_FAILURE_SIGNALS``，或 shell_exit_code 非零。

    attach_rewards（据此给 step 负 reward）与 failure_attribution（据此定位首因）共用此判定，
    保证「什么算失败」单一来源。
    """
    signal = ev.get("signal")
    if signal in _FAILURE_SIGNALS:
        return True
    return signal == SIG_SHELL_EXIT_CODE and ev.get("value") not in (0, None)


# ─── P4 API：attach_rewards ──────────────────────────────────────────────────

def attach_rewards(steps: "list", evals: "list[dict]") -> "list":
    """据 eval 记录给 step 回填 provisional reward，返回**新的** step 列表（不就地改）。

    reward 是有界启发式（暂定，可为 None）：
      - 某 step 命中任一失败信号（tool_error / permission_denied / budget_exceeded /
        context_overflow / tests_fail，或 shell_exit_code!=0）→ reward = -1.0。
      - 否则保持原 reward（通常 None）——干净 step 不强行赋 0，留给 offline / human 回填。

    仅能关联到 step_id 的 eval 才参与（其余 eval 按 turn/agent 聚合，不在此回填）。绝不抛。
    """
    if not steps:
        return []

    # 收集「带负向信号的 step_id」。
    penalized: set = set()
    for ev in (evals or []):
        try:
            if not isinstance(ev, dict):
                continue
            sid = ev.get("step_id")
            if sid is None:
                continue
            if _is_failure_eval(ev):
                penalized.add(sid)
        except Exception:
            continue

    out: list = []
    for s in steps:
        try:
            new_step = _copy_step(s)
            sid = getattr(new_step, "step_id", None)
            if sid is not None and sid in penalized:
                try:
                    new_step.reward = _REWARD_ON_FAILURE
                except Exception:
                    pass
            out.append(new_step)
        except Exception:
            out.append(s)
    return out


def _copy_step(step):
    """浅拷贝一个 step（dataclass 用 replace；否则原样返回）。绝不抛。"""
    try:
        import copy
        from dataclasses import is_dataclass

        if is_dataclass(step):
            return copy.replace(step)  # py3.13+ dataclass replace（保留类型）
        return copy.copy(step)
    except Exception:
        try:
            import copy
            return copy.copy(step)
        except Exception:
            return step


# ─── P4 API：failure_attribution ────────────────────────────────────────────

def failure_attribution(events: "list[SessionEvent]", evals: "list[dict]") -> "dict | None":
    """尽力定位失败首因：返回首个失败 eval 关联的 step/tool/turn，干净则 None。

    返回形状（best-effort）：
      ``{"signal", "step_id"|None, "turn_id"|None, "agent_id", "tool"|None, "detail"}``

    定位策略：取 ``evals`` 中**第一条**负向信号（evals 已按 events 顺序生成，故首条即时间序最早
    的失败）。从该 eval 的 detail / value 尽力提取 tool。若无负向信号 → 视为干净 → None。
    绝不抛。
    """
    if not evals:
        return None

    first = None
    for ev in evals:
        try:
            if not isinstance(ev, dict):
                continue
            if _is_failure_eval(ev):
                first = ev
                break
        except Exception:
            continue

    if first is None:
        return None  # 看起来是干净 run

    signal = first.get("signal")
    tool = _attribution_tool(signal, first)
    return {
        "signal": signal,
        "step_id": first.get("step_id"),
        "turn_id": first.get("turn_id"),
        "agent_id": first.get("agent_id"),
        "tool": tool,
        "detail": first.get("detail") or "",
    }


def _attribution_tool(signal: "str | None", ev: dict) -> "str | None":
    """从一条失败 eval 尽力提取涉事 tool 名（permission_denied 的 value 即 tool）。"""
    try:
        if signal == SIG_PERMISSION_DENIED:
            v = ev.get("value")
            return v if isinstance(v, str) and v else None
        # tool_blocked 把 tool 编进 detail（"tool_blocked: <tool> (<reason>)"）。
        detail = ev.get("detail") or ""
        if isinstance(detail, str) and detail.startswith("tool_blocked:"):
            rest = detail[len("tool_blocked:"):].strip()
            return rest.split(" ", 1)[0] or None
    except Exception:
        return None
    return None
