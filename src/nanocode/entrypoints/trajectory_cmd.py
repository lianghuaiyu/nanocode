"""`nanocode trajectory` 子命令：把 canonical 会话树投影为 trajectory（只读 DERIVED 视图）。

`list` / `show <id>` / `export <id> [--out DIR]`：
- list   —— 列出可投影的 canonical 会话（trajectory._tree_events.list_tree_sessions）。
- show   —— 投影 steps（trajectory.project_session）+ 指标摘要（trajectory.compute_metrics）。
- export —— 导出 trajectory bundle（trajectory.export_bundle）并打印 bundle 路径。

硬边界：本命令只**读** canonical 树，绝不写回；trajectory 是 DERIVED 投影，绝不驱动 runtime。
坏 id 容忍：print_error + 返回 1。
"""
from __future__ import annotations

import argparse

from ..ui import console, print_error, print_info


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="nanocode trajectory",
        description="Project the canonical session tree into a trajectory (read-only analysis/RL view).",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="list sessions available as trajectories")

    p_show = sub.add_parser("show", help="per-step table + metrics summary for a session")
    p_show.add_argument("session", help="session id prefix or 'latest'")

    p_export = sub.add_parser("export", help="export a session as a trajectory bundle")
    p_export.add_argument("session", help="session id prefix or 'latest'")
    p_export.add_argument("--out", default=None, help="output directory for the bundle")

    args = parser.parse_args(argv)

    # 缺省（`nanocode trajectory`）= list。
    if args.cmd in (None, "list"):
        return _run_list()
    if args.cmd == "show":
        return _run_show(args.session)
    if args.cmd == "export":
        return _run_export(args.session, args.out)
    parser.print_help()
    return 1


def _render_session_list(sessions: list[dict]) -> str:
    if not sessions:
        return "No sessions found under ~/.nanocode/sessions/."
    lines = [f"{'SESSION':18}  {'WHEN':19}  {'AGENTS':>6}  {'EVENTS':>6}  FIRST MESSAGE"]
    for s in sessions:
        when = (s.get("start_ts") or "")[:19].replace("T", " ")
        msg = (s.get("first_user_msg") or "").replace("\n", " ")
        if len(msg) > 50:
            msg = msg[:50] + "…"
        lines.append(
            f"{s['session_id']:18}  {when:19}  {s.get('n_agents', 1):>6}  "
            f"{s.get('n_events', 0):>6}  {msg}"
        )
    return "\n".join(lines)


def _run_list() -> int:
    from ..trajectory._tree_events import list_tree_sessions

    console.print(_render_session_list(list_tree_sessions()))
    return 0


def _resolve(session_arg: str) -> "str | None":
    from ..trajectory._tree_events import resolve_tree_session

    try:
        return resolve_tree_session(session_arg)
    except (FileNotFoundError, ValueError) as e:
        print_error(str(e))
        return None


def _run_show(session_arg: str) -> int:
    from .. import trajectory
    from ..trajectory._tree_events import tree_events

    sid = _resolve(session_arg)
    if sid is None:
        return 1
    events = tree_events(sid)
    steps = trajectory.project_session(sid)

    console.print(f"trajectory {sid} — {len(steps)} step(s)")
    console.print(f"{'STEP':22}  {'TYPE':12}  {'AGENT':10}  {'RISK':6}  ACTION / RESULT")
    for st in steps:
        action = st.action or {}
        act = action.get("tool") or action.get("type") or st.step_type
        detail = action.get("args_summary") or st.result_summary or st.observation_summary or ""
        detail = detail.replace("\n", " ")
        if len(detail) > 48:
            detail = detail[:48] + "…"
        console.print(
            f"{st.step_id:22}  {st.step_type:12}  {st.agent_id:10}  {st.risk_level:6}  "
            f"{act} {('· ' + detail) if detail else ''}"
        )

    m = trajectory.compute_metrics(events, steps)
    console.print("")
    console.print("metrics:")
    console.print(f"  turns={m.get('total_turns', 0)}  "
                  f"tool_calls={m.get('total_tool_calls', 0)}  "
                  f"tool_failures={m.get('tool_failure_count', 0)}  "
                  f"deny={m.get('permission_deny_count', 0)}")
    console.print(f"  tokens in={m.get('total_input_tokens', 0)} "
                  f"out={m.get('total_output_tokens', 0)}  "
                  f"est_cost_usd={m.get('est_cost_usd', 0.0):.4f}")
    console.print(f"  tests run={m.get('tests_run', 0)} "
                  f"passed={m.get('tests_passed', 0)} failed={m.get('tests_failed', 0)}  "
                  f"high_risk={m.get('high_risk_action_count', 0)}")
    return 0


def _run_export(session_arg: str, out_dir: "str | None") -> int:
    from .. import trajectory

    sid = _resolve(session_arg)
    if sid is None:
        return 1
    try:
        path = trajectory.export_bundle(sid, out_dir)
    except Exception as e:  # 导出失败：容忍，print_error + 返回 1
        print_error(f"export failed for '{sid}': {e}")
        return 1
    print_info(f"trajectory bundle → {path}")
    return 0
