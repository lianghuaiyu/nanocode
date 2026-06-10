"""trajectory.metrics — 从 merged wire 派生的 P3 harness 指标聚合（docs/10 P3）。

DERIVED 投影层（硬边界，见 trajectory/__init__.py）：
- 本模块**只读** merged wire（``list[SessionEvent]``），绝不写回 wire、绝不驱动 runtime、
  绝不参与 resume / fork。reward / eval_result 是派生标签，只来自传入的 ``steps``，绝不
  从这里写进 wire。
- 仅依赖标准库与（可选）``nanocode.trajectory.schema``（PURE）；**不**得 import 任何
  runtime 模块。``nanocode.trace.redaction.truncate`` 可按需复用，但本聚合不需截断。

健壮性铁律：投影绝不崩。legacy 行（``.legacy`` True）、summary 级事件（无 full
``messages``/``result``，只有 ``messages_chars``/``result_summary`` 等）、malformed/缺字段
的行都必须容忍——缺数据降级为 0/None，绝不抛进调用方。

输入：``events`` 为 ``nanocode.events.reader.merge_session_events(session_id)`` 的产物
（展示序 ``(ts, agent_id, seq, line_no)``）。配对（llm_request→llm_response、
tool_call→tool_result）在**单 agent 内**按 seq 进行——跨兄弟 agent 无全序，故先按 agent_id
分桶再配对。
"""
from __future__ import annotations

from datetime import datetime

# 价格（docs/10 / 任务契约）：input $3/M、output $15/M。
_COST_IN_PER_TOKEN = 3.0 / 1_000_000
_COST_OUT_PER_TOKEN = 15.0 / 1_000_000

# 文件类工具（其 input.file_path 计入 files_touched）。
_FILE_TOOLS = frozenset({"read_file", "write_file", "edit_file"})

# tool_result 摘要被视作「失败」的前缀（大小写不敏感比对时统一 lower）。
_FAILURE_PREFIXES = ("error", "warning")


def _as_int(value, default: int = 0) -> int:
    """容忍把任意值转 int；失败返回默认。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_ts(ts) -> "datetime | None":
    """把 ISO8601 ts 解析为 datetime；失败/空返回 None（绝不抛）。"""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        # 兼容 ``Z`` 后缀的 legacy timestamp。
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return None


def _delta_ms(a, b) -> "int | None":
    """两个 ISO8601 ts 之间的毫秒差（b - a）；任一不可解析返回 None。负值钳为 0。"""
    ta, tb = _parse_ts(a), _parse_ts(b)
    if ta is None or tb is None:
        return None
    try:
        ms = int((tb - ta).total_seconds() * 1000)
    except Exception:
        return None
    return ms if ms >= 0 else 0


def _result_text(data: dict) -> str:
    """从 tool_result 的 data 取结果文本：FULL 用 ``result``，SUMMARY 用 ``result_summary``。

    两者都缺则空串（不崩）。统一 str 化以便前缀比对。
    """
    if not isinstance(data, dict):
        return ""
    val = data.get("result")
    if val is None:
        val = data.get("result_summary")
    if val is None:
        return ""
    return val if isinstance(val, str) else str(val)


def _is_failure_text(text: str) -> bool:
    """结果文本是否以 Error/Warning 开头（去前导空白、大小写不敏感）。"""
    if not text:
        return False
    return text.lstrip().lower().startswith(_FAILURE_PREFIXES)


def _avg(total: "int | float", n: int) -> "float | None":
    return (total / n) if n else None


def _mean_int(values: "list[int]") -> "int | None":
    return int(sum(values) / len(values)) if values else None


def _pytest_outcome(result_text: str) -> dict:
    """从一次 pytest/test run 的结果文本 best-effort 解析 passed/failed 计数。

    启发式（heuristic，刻意保守、不保证精确）：扫描 pytest 末行风格的
    ``N passed`` / ``N failed`` / ``N error`` 词组（如 ``3 passed, 1 failed in 0.2s``）。
    解析不出任何计数时返回 ``{"passed": 0, "failed": 0, "matched": False}``——matched=False
    表示「跑了测试但无法判定结果」，由调用方据此只计 tests_run、不增减 pass/fail。
    """
    import re

    passed = failed = 0
    matched = False
    text = result_text or ""
    for n, word in re.findall(r"(\d+)\s+(passed|failed|error|errors)", text, flags=re.IGNORECASE):
        cnt = _as_int(n)
        w = word.lower()
        if w == "passed":
            passed += cnt
            matched = True
        else:  # failed / error / errors
            failed += cnt
            matched = True
    return {"passed": passed, "failed": failed, "matched": matched}


def _looks_like_test_command(cmd: str) -> bool:
    """run_shell 命令是否像在跑测试（含 ``pytest`` 或 ``test`` 词）。启发式。"""
    if not isinstance(cmd, str) or not cmd:
        return False
    low = cmd.lower()
    return "pytest" in low or "test" in low


def compute_metrics(events: "list", steps: "list | None" = None) -> dict:
    """聚合 P3 harness 指标集（docs/10 P3）。

    参数：
      - ``events``：merged wire 的 ``SessionEvent`` 列表（只读）。
      - ``steps``：可选的派生 step 列表（``trajectory.schema.Step`` 或其 ``to_record()`` dict），
        仅用于读 ``high_risk_action_count`` / reward 等派生标签——**绝不**写回 wire。

    返回扁平 dict（缺数据降级 0/None），含 ``per_agent`` / ``per_tool`` 两个 breakdown。
    全程容忍 legacy / summary / malformed，绝不抛。
    """
    events = events or []

    # ── 顶层累加器 ──────────────────────────────────────────────
    total_turns = 0
    total_tool_calls = 0
    tool_failure_count = 0
    tool_blocked_count = 0
    permission_deny_count = 0
    permission_decision_count = 0
    total_input_tokens = 0
    total_output_tokens = 0
    compaction_count = 0
    timeout_cancel_error_count = 0
    files_touched: set[str] = set()
    tests_run = 0
    tests_passed = 0
    tests_failed = 0

    model_latencies: list[int] = []
    tool_latencies: list[int] = []

    # ── per-agent / per-tool breakdown 累加器 ──────────────────
    per_agent: dict[str, dict] = {}
    per_tool: dict[str, dict] = {}

    def _agent_bucket(aid: str) -> dict:
        b = per_agent.get(aid)
        if b is None:
            b = {
                "total_turns": 0,
                "total_tool_calls": 0,
                "tool_failure_count": 0,
                "tool_blocked_count": 0,
                "permission_deny_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "compaction_count": 0,
                "model_latency_ms_sum": 0,
                "tool_latency_ms_sum": 0,
            }
            per_agent[aid] = b
        return b

    def _tool_bucket(tool: str) -> dict:
        b = per_tool.get(tool)
        if b is None:
            b = {"calls": 0, "failures": 0, "latency_ms_sum": 0, "latency_ms_count": 0}
            per_tool[tool] = b
        return b

    # 按 agent 分桶（保留各自相对顺序，用于单 agent 内的配对）。
    # agent_id 归一与 project._safe_agent 一致（falsy -> "main"），否则 legacy/空 agent_id 的
    # per_agent 桶键（""）与 step.agent_id（"main"）对不上，下游 per-agent join 错位（审阅 LOW）。
    by_agent: dict[str, list] = {}
    for ev in events:
        aid = getattr(ev, "agent_id", "") or "main"
        by_agent.setdefault(aid, []).append(ev)

    for aid, agent_events in by_agent.items():
        ab = _agent_bucket(aid)
        # 单 agent 内按 seq 排序后配对（merge 已是展示序，但配对要的是因果/seq 序）。
        ordered = sorted(agent_events, key=lambda e: (_as_int(getattr(e, "seq", 0)), getattr(e, "line_no", 0)))

        # llm_request 等待其后第一条 llm_response 配对延迟。
        pending_llm_req_ts: "str | None" = None
        # tool_call 按 tool_use_id 配对 tool_result；无 id 时退化为 FIFO。
        # 值为 (tool, ts, command)——command 仅 run_shell 携带，供 tool_result 端的
        # tests_run 启发式判定（命令在 tool_call 上，结果在 tool_result 上）。
        pending_tool_calls: dict = {}  # tool_use_id -> (tool, ts, command)
        pending_tool_fifo: list = []   # (tool, ts, command) 当 tool_use_id 缺失

        for ev in ordered:
            etype = getattr(ev, "type", "") or ""
            data = getattr(ev, "data", None)
            if not isinstance(data, dict):
                data = {}
            ts = getattr(ev, "ts", "") or ""

            if etype == "turn_end":
                total_turns += 1
                ab["total_turns"] += 1

            elif etype == "llm_request":
                pending_llm_req_ts = ts

            elif etype == "llm_response":
                total_input_tokens += _as_int(data.get("input_tokens"))
                total_output_tokens += _as_int(data.get("output_tokens"))
                ab["input_tokens"] += _as_int(data.get("input_tokens"))
                ab["output_tokens"] += _as_int(data.get("output_tokens"))
                if pending_llm_req_ts is not None:
                    d = _delta_ms(pending_llm_req_ts, ts)
                    if d is not None:
                        model_latencies.append(d)
                        ab["model_latency_ms_sum"] += d
                    pending_llm_req_ts = None

            elif etype == "tool_call":
                total_tool_calls += 1
                ab["total_tool_calls"] += 1
                tool = data.get("tool") or "<unknown>"
                tb = _tool_bucket(tool)
                tb["calls"] += 1
                inp = data.get("input")
                command = ""
                if isinstance(inp, dict):
                    command = inp.get("command") or inp.get("cmd") or ""
                tuid = data.get("tool_use_id")
                if tuid is not None:
                    pending_tool_calls[tuid] = (tool, ts, command)
                else:
                    pending_tool_fifo.append((tool, ts, command))
                # files_touched：文件类工具的 input.file_path。
                if tool in _FILE_TOOLS:
                    if isinstance(inp, dict):
                        fp = inp.get("file_path")
                        if isinstance(fp, str) and fp:
                            files_touched.add(fp)

            elif etype == "tool_result":
                tool = data.get("tool") or "<unknown>"
                rtext = _result_text(data)
                failed = _is_failure_text(rtext)
                if failed:
                    tool_failure_count += 1
                    ab["tool_failure_count"] += 1
                # 配对延迟：先按 tool_use_id，再退化 FIFO。也取回 tool_call 端的 command。
                call_ts = None
                call_cmd = ""
                tuid = data.get("tool_use_id")
                if tuid is not None and tuid in pending_tool_calls:
                    _, call_ts, call_cmd = pending_tool_calls.pop(tuid)
                elif pending_tool_fifo:
                    _, call_ts, call_cmd = pending_tool_fifo.pop(0)
                if call_ts is not None:
                    d = _delta_ms(call_ts, ts)
                    if d is not None:
                        tool_latencies.append(d)
                        ab["tool_latency_ms_sum"] += d
                        tbk = _tool_bucket(tool)
                        tbk["latency_ms_sum"] += d
                        tbk["latency_ms_count"] += 1
                if failed:
                    _tool_bucket(tool)["failures"] += 1
                # tests_run/passed/failed（best-effort，仅 run_shell 的 pytest/test 命令）。
                # 命令文本在 tool_call 端，已随配对取回为 call_cmd。
                if tool == "run_shell" and _looks_like_test_command(str(call_cmd)):
                    tests_run += 1
                    oc = _pytest_outcome(rtext)
                    tests_passed += oc["passed"]
                    tests_failed += oc["failed"]
                    # 无法解析计数但确属失败前缀 → 至少记 1 failed。
                    if not oc["matched"] and failed:
                        tests_failed += 1

            elif etype == "tool_blocked":
                # tool_blocked 是 allowlist/策略拦截的**信息信号**，在此不重复计 call/failure。
                # 生产真实序列：tool_call（计 call）-> tool_blocked -> tool_result（结果文本以
                # "Error: tool ... is not permitted" 开头，计 failure），三者同指一次被挡调用。
                # 若此处再加 call/failure，单次被挡会被记成 2 call + 2 failure（审阅 HIGH 双计）。
                # 故只累加独立的 tool_blocked_count（孤立 tool_blocked，如 hook 路径，亦只计此项）。
                tool_blocked_count += 1
                ab["tool_blocked_count"] += 1

            elif etype == "permission_decision":
                permission_decision_count += 1
                action = (data.get("action") or "").lower() if isinstance(data.get("action"), str) else ""
                if action in ("deny", "denied", "reject", "rejected"):
                    permission_deny_count += 1
                    ab["permission_deny_count"] += 1

            elif etype == "compaction":
                compaction_count += 1
                ab["compaction_count"] += 1

            # timeout / cancel / error best-effort：扫 budget_exceeded / session_end 与
            # 任意事件 data 里的 error/cancelled/timeout 标志位。
            if etype == "budget_exceeded":
                timeout_cancel_error_count += 1
            else:
                if _has_timeout_cancel_error(etype, data):
                    timeout_cancel_error_count += 1

    # high_risk_action_count：仅来自传入 steps（派生标签；events 里无 risk_level）。
    high_risk_action_count = _count_high_risk(steps)

    # ── 比率 ────────────────────────────────────────────────────
    tool_failure_rate = (tool_failure_count / total_tool_calls) if total_tool_calls else 0.0
    deny_rate = (permission_deny_count / permission_decision_count) if permission_decision_count else 0.0

    est_cost_usd = (
        total_input_tokens * _COST_IN_PER_TOKEN + total_output_tokens * _COST_OUT_PER_TOKEN
    )

    # per_tool 补 avg latency。
    for tb in per_tool.values():
        tb["latency_ms_avg"] = _avg(tb["latency_ms_sum"], tb["latency_ms_count"])

    return {
        "total_turns": total_turns,
        "total_tool_calls": total_tool_calls,
        "tool_failure_count": tool_failure_count,
        "tool_failure_rate": tool_failure_rate,
        "tool_blocked_count": tool_blocked_count,
        "permission_deny_count": permission_deny_count,
        "permission_decision_count": permission_decision_count,
        "deny_rate": deny_rate,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "est_cost_usd": est_cost_usd,
        "model_latency_ms": {
            "sum": sum(model_latencies),
            "avg": _avg(sum(model_latencies), len(model_latencies)),
            "count": len(model_latencies),
        },
        "tool_latency_ms": {
            "sum": sum(tool_latencies),
            "avg": _avg(sum(tool_latencies), len(tool_latencies)),
            "count": len(tool_latencies),
        },
        "compaction_count": compaction_count,
        "timeout_cancel_error_count": timeout_cancel_error_count,
        "high_risk_action_count": high_risk_action_count,
        "files_touched": sorted(files_touched),
        "tests_run": tests_run,
        "tests_passed": tests_passed,
        "tests_failed": tests_failed,
        "per_agent": per_agent,
        "per_tool": per_tool,
    }


def _has_timeout_cancel_error(etype: str, data: dict) -> bool:
    """best-effort：事件 data 是否携带 timeout / cancel / error 信号。

    扫常见布尔/字符串字段（``error`` / ``cancelled`` / ``canceled`` / ``timeout`` / ``aborted``，
    以及 ``status``/``reason`` 含相应词）。保守：只看明确字段，不据结果文本前缀（那归
    tool_failure）。绝不抛。
    """
    if not isinstance(data, dict):
        return False
    for key in ("error", "cancelled", "canceled", "timeout", "timed_out", "aborted"):
        v = data.get(key)
        if v is True:
            return True
        if isinstance(v, str) and v.strip():
            return True
    for key in ("status", "reason", "final_status"):
        v = data.get(key)
        if isinstance(v, str):
            low = v.lower()
            if any(w in low for w in ("timeout", "timed_out", "cancel", "aborted", "error")):
                return True
    return False


def _count_high_risk(steps: "list | None") -> int:
    """从派生 steps 数高风险动作数（risk_level == 'high'）。steps 缺/异形 → 0。

    兼容两种形态：``Step`` dataclass（有 ``.risk_level``）或其 ``to_record()`` dict
    （``metadata.risk_level``）。
    """
    if not steps:
        return 0
    n = 0
    for s in steps:
        try:
            rl = getattr(s, "risk_level", None)
            if rl is None and isinstance(s, dict):
                meta = s.get("metadata")
                if isinstance(meta, dict):
                    rl = meta.get("risk_level")
                if rl is None:
                    rl = s.get("risk_level")
            if isinstance(rl, str) and rl.lower() == "high":
                n += 1
        except Exception:
            continue
    return n
