"""`nanocode trace` 子命令：查看/汇总已记录的 agent 轨迹（只读）。"""
from __future__ import annotations

import argparse

from ..ui import console, print_error
from ..trace import report


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="nanocode trace",
        description="View or summarize recorded agent traces.",
    )
    parser.add_argument("session", nargs="?",
                        help="session id prefix or 'latest' (omit to list all sessions)")
    parser.add_argument("--summary", action="store_true",
                        help="show an aggregate summary instead of the timeline")
    parser.add_argument("--full", action="store_true",
                        help="expand long content (messages / tool results)")
    parser.add_argument("--wire", action="store_true",
                        help="read the always-on per-agent event tree "
                             "(~/.nanocode/sessions/<id>/agents/*/wire.jsonl) instead of "
                             "the opt-in ./.nanocode/traces/ debug lane")
    args = parser.parse_args(argv)

    if args.wire:
        return _run_wire(args)

    if not args.session:
        console.print(report.render_session_list(report.list_sessions()))
        return 0

    try:
        path = report.resolve_session(args.session)
    except (FileNotFoundError, ValueError) as e:
        print_error(str(e))
        return 1

    events = report.load_session_events(path)
    if args.summary:
        console.print(report.render_summary(events))
    else:
        console.print(report.render_timeline(events, full=args.full))
    return 0


def _run_wire(args) -> int:
    """`nanocode trace --wire`：读 always-on per-agent wire 流（读时 merge）。"""
    from ..events import reader

    if not args.session:
        console.print(report.render_wire_session_list(reader.list_wire_sessions()))
        return 0
    try:
        sid = reader.resolve_wire_session(args.session)
    except (FileNotFoundError, ValueError) as e:
        print_error(str(e))
        return 1
    events = reader.merge_session_events(sid)
    if args.summary:
        console.print(report.render_wire_summary(events, full=args.full))
    else:
        console.print(report.render_wire_timeline(events, full=args.full))
    return 0
