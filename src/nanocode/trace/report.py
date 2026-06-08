"""读取 trace JSONL 并渲染为人类可读视图（纯函数、只读）。"""
from __future__ import annotations

import json
from pathlib import Path

from .config import trace_dir


def _cost(inp: int, out: int) -> float:
    return inp / 1_000_000 * 3 + out / 1_000_000 * 15


def load_events(path: Path) -> list[dict]:
    """逐行 json.loads；跳过空行与坏行。"""
    events: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except OSError:
        pass
    return events


def load_session_events(main_path: Path) -> list[dict]:
    """主轨迹 + 其名下沙箱子轨迹，合并为一个事件流。
    子轨迹约定路径：<主目录>/<主session stem>/sandbox/<tag>/<child>.jsonl。"""
    events = load_events(main_path)
    child_root = main_path.parent / main_path.stem / "sandbox"
    if child_root.is_dir():
        for child in sorted(child_root.rglob("*.jsonl")):
            events.extend(load_events(child))
    return events


def list_sessions() -> list[dict]:
    """扫 trace_dir() 下 *.jsonl；流式早停：每文件只解析约 3 行。"""
    sessions: list[dict] = []
    for p in trace_dir().glob("*.jsonl"):
        try:
            nonempty = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        except OSError:
            continue
        model = start_ts = first_user_msg = ""
        cost_usd = 0.0
        if nonempty:
            try:
                e0 = json.loads(nonempty[0])
                model = e0.get("model", "")
                start_ts = e0.get("ts", "")
            except Exception:
                pass
            for l in nonempty:
                try:
                    e = json.loads(l)
                except Exception:
                    continue
                if e.get("type") == "user_message":
                    first_user_msg = e.get("text") or ""
                    break
            try:
                last = json.loads(nonempty[-1])
                if last.get("type") == "session_end":
                    cost_usd = _cost(last.get("input_tokens", 0), last.get("output_tokens", 0))
            except Exception:
                pass
        sessions.append({
            "session_id": p.stem, "path": p, "mtime": p.stat().st_mtime,
            "n_events": len(nonempty), "model": model, "start_ts": start_ts,
            "first_user_msg": first_user_msg, "cost_usd": cost_usd,
        })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


def resolve_session(arg: str) -> Path:
    """arg='latest' → 最新；否则按文件名 stem 前缀匹配（0→FileNotFoundError，多→ValueError）。"""
    files = list(trace_dir().glob("*.jsonl"))
    if not files:
        raise FileNotFoundError("no trace files found in ./.nanocode/traces/")
    if arg == "latest":
        return max(files, key=lambda p: p.stat().st_mtime)
    matches = [p for p in files if p.stem.startswith(arg)]
    if not matches:
        raise FileNotFoundError(f"no trace session matching '{arg}'")
    if len(matches) > 1:
        raise ValueError("ambiguous session id '" + arg + "': " + ", ".join(sorted(p.stem for p in matches)))
    return matches[0]


# ─── 渲染 ───────────────────────────────────────────────

from collections import Counter
from datetime import datetime


def _truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + "…"


def render_session_list(sessions: list[dict]) -> str:
    if not sessions:
        return "No traces found. Run nanocode with --trace (or NANOCODE_TRACE=1) first."
    lines = [f"{'SESSION':10}  {'WHEN':19}  {'EVENTS':>6}  {'COST':>8}  FIRST MESSAGE"]
    for s in sessions:
        when = (s.get("start_ts") or "")[:19].replace("T", " ")
        msg = _truncate((s.get("first_user_msg") or "").replace("\n", " "), 50)
        lines.append(f"{s['session_id']:10}  {when:19}  {s['n_events']:>6}  ${s['cost_usd']:>7.4f}  {msg}")
    return "\n".join(lines)


def _sum_session_start(e, full):
    s = f"{e.get('model','')} · {e.get('permission_mode','')}"
    if e.get("parent_session_id"):
        s += f" [sub of {e['parent_session_id']}]"
    return s


def _sum_user(e, full):
    return _truncate(e.get("text", ""), 1000 if full else 100)


def _sum_llm_req(e, full):
    s = f"msgs={e.get('message_count', '?')}"
    if full and e.get("messages") is not None:
        s += "\n" + json.dumps(e["messages"], ensure_ascii=False, indent=2)
    return s


def _sum_assistant(e, full):
    parts = []
    txt = _truncate(e.get("text", ""), 2000 if full else 80)
    if txt:
        parts.append(txt)
    tools = [tu.get("name") for tu in (e.get("tool_uses") or [])]
    if tools:
        parts.append(f"tools={tools}")
    if e.get("thinking"):
        parts.append("(thinking)")
    return " ".join(parts)


def _sum_llm_resp(e, full):
    return f"in={e.get('input_tokens', 0)} out={e.get('output_tokens', 0)}"


def _sum_perm(e, full):
    return f"{e.get('tool', '')} → {e.get('action', '')}"


def _sum_tool_call(e, full):
    inp = e.get("input")
    body = json.dumps(inp, ensure_ascii=False) if inp is not None else ""
    return f"{e.get('tool', '')} {_truncate(body, 4000 if full else 80)}".rstrip()


def _sum_tool_result(e, full):
    s = f"{e.get('tool', '')} ({e.get('chars', '?')} chars)"
    if full and e.get("result") is not None:
        s += "\n" + str(e["result"])
    return s


def _sum_compaction(e, full):
    return e.get("kind", "")


def _sum_budget(e, full):
    return e.get("reason", "")


def _sum_turn(e, full):
    return f"in={e.get('input_tokens', 0)} out={e.get('output_tokens', 0)} turns={e.get('turns', 0)}"


_SUMMARIZERS = {
    "session_start": _sum_session_start,
    "user_message": _sum_user,
    "llm_request": _sum_llm_req,
    "assistant_message": _sum_assistant,
    "llm_response": _sum_llm_resp,
    "permission_decision": _sum_perm,
    "tool_call": _sum_tool_call,
    "tool_result": _sum_tool_result,
    "compaction": _sum_compaction,
    "budget_exceeded": _sum_budget,
    "turn_end": _sum_turn,
    "session_end": _sum_turn,
}


def _depths(events):
    parent_of = {}
    for e in events:
        sid = e.get("session_id")
        if sid and sid not in parent_of:
            parent_of[sid] = e.get("parent_session_id")

    def depth(sid):
        d, seen = 0, set()
        while sid and parent_of.get(sid) and sid not in seen:
            seen.add(sid)
            sid = parent_of[sid]
            d += 1
        return d
    return depth


def render_timeline(events: list[dict], full: bool = False) -> str:
    if not events:
        return "(no events)"
    depth = _depths(events)
    lines = []
    for e in events:
        indent = "  " * depth(e.get("session_id"))
        fn = _SUMMARIZERS.get(e.get("type"))
        summary = fn(e, full) if fn else ""
        lines.append(f"{indent}{e.get('seq', '?'):>3} {e.get('type', '?'):<20} {summary}".rstrip())
    return "\n".join(lines)


def render_summary(events: list[dict]) -> str:
    if not events:
        return "(no events)"
    tool_calls = Counter(e.get("tool") for e in events if e.get("type") == "tool_call")
    ends = [e for e in events if e.get("type") in ("session_end", "turn_end")]
    if ends:
        last = ends[-1]
        in_tok, out_tok, turns = last.get("input_tokens", 0), last.get("output_tokens", 0), last.get("turns", 0)
    else:
        resp = [e for e in events if e.get("type") == "llm_response"]
        in_tok = sum(e.get("input_tokens", 0) for e in resp)
        out_tok = sum(e.get("output_tokens", 0) for e in resp)
        turns = 0
    ts = [e.get("ts") for e in events if e.get("ts")]
    dur = ""
    if len(ts) >= 2:
        try:
            dur = f"{(datetime.fromisoformat(ts[-1]) - datetime.fromisoformat(ts[0])).total_seconds():.1f}s"
        except Exception:
            dur = ""
    sub_agents = len({e.get("session_id") for e in events if e.get("parent_session_id")})
    n_budget = sum(1 for e in events if e.get("type") == "budget_exceeded")
    n_deny = sum(1 for e in events if e.get("type") == "permission_decision" and e.get("action") == "deny")
    lines = [
        f"events:      {len(events)}",
        f"turns:       {turns}",
        f"tokens:      {in_tok} in / {out_tok} out",
        f"est. cost:   ${_cost(in_tok, out_tok):.4f}",
        f"duration:    {dur}",
        f"sub-agents:  {sub_agents}",
        f"budget hits: {n_budget}",
        f"denied:      {n_deny}",
        "tool calls:",
    ]
    for tool, n in tool_calls.most_common():
        lines.append(f"  {tool}: {n}")
    if not tool_calls:
        lines.append("  (none)")
    return "\n".join(lines)


# ─── wire lane（always-on per-agent event tree）视图 ──────────────
# 复用上面的 _SUMMARIZERS 渲染体；但 wire lane 以 agent_id 分组（非 session_id），
# 故时间线用 AGENT 列、汇总按 agent_id 数子 agent，而不走 _depths/parent_session_id。

def render_wire_session_list(sessions: list[dict]) -> str:
    if not sessions:
        return "No wire sessions found under ~/.nanocode/sessions/."
    lines = [f"{'SESSION':18}  {'WHEN':19}  {'AGENTS':>6}  {'EVENTS':>6}  FIRST MESSAGE"]
    for s in sessions:
        when = (s.get("start_ts") or "")[:19].replace("T", " ")
        msg = _truncate((s.get("first_user_msg") or "").replace("\n", " "), 50)
        lines.append(f"{s['session_id']:18}  {when:19}  {s.get('n_agents', 1):>6}  {s.get('n_events', 0):>6}  {msg}")
    return "\n".join(lines)


def _wire_body(e, full: bool) -> str:
    """用 SessionEvent 的 payload（.data）调用既有 _SUMMARIZERS 渲染体。"""
    fn = _SUMMARIZERS.get(e.type)
    if not fn:
        return ""
    d = dict(e.data)
    d.setdefault("parent_session_id", e.parent_session_id)  # session_start 体会用
    return fn(d, full)


def render_wire_timeline(events, full: bool = False) -> str:
    if not events:
        return "(no events)"
    lines = [f"{'AGENT':12} {'#':>3} {'TYPE':<18} DETAIL"]
    for e in events:
        body = _wire_body(e, full)
        lines.append(f"{e.agent_id:<12} {e.seq:>3} {e.type:<18} {body}".rstrip())
    return "\n".join(lines)


def render_wire_summary(events, full: bool = False) -> str:
    if not events:
        return "(no events)"
    disp = [{"type": e.type, "ts": e.ts, **e.data} for e in events]
    tool_calls = Counter(d.get("tool") for d in disp if d.get("type") == "tool_call")
    ends = [d for d in disp if d.get("type") in ("session_end", "turn_end")]
    if ends:
        last = ends[-1]
        in_tok, out_tok, turns = last.get("input_tokens", 0), last.get("output_tokens", 0), last.get("turns", 0)
    else:
        resp = [d for d in disp if d.get("type") == "llm_response"]
        in_tok = sum(d.get("input_tokens", 0) for d in resp)
        out_tok = sum(d.get("output_tokens", 0) for d in resp)
        turns = 0
    ts = [e.ts for e in events if e.ts]
    dur = ""
    if len(ts) >= 2:
        try:
            dur = f"{(datetime.fromisoformat(ts[-1]) - datetime.fromisoformat(ts[0])).total_seconds():.1f}s"
        except Exception:
            dur = ""
    agents = sorted({e.agent_id for e in events})
    sub_agents = len([a for a in agents if a != "main"])
    n_budget = sum(1 for d in disp if d.get("type") == "budget_exceeded")
    n_deny = sum(1 for d in disp if d.get("type") == "permission_decision" and d.get("action") == "deny")
    n_legacy = sum(1 for e in events if e.legacy)
    lines = [
        f"events:      {len(events)}" + (f" ({n_legacy} legacy)" if n_legacy else ""),
        f"turns:       {turns}",
        f"tokens:      {in_tok} in / {out_tok} out",
        f"est. cost:   ${_cost(in_tok, out_tok):.4f}",
        f"duration:    {dur}",
        f"agents:      {len(agents)} ({sub_agents} sub)",
        f"budget hits: {n_budget}",
        f"denied:      {n_deny}",
        "tool calls:",
    ]
    for tool, n in tool_calls.most_common():
        lines.append(f"  {tool}: {n}")
    if not tool_calls:
        lines.append("  (none)")
    return "\n".join(lines)
