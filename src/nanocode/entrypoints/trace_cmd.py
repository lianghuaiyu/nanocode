"""`nanocode trace` 子命令：查看/汇总已记录的 agent 轨迹（只读）。"""
from __future__ import annotations

import argparse

from ..ui import console, print_error
from ..trace import report


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="nanocode trace",
        description="View or summarize recorded agent traces (./.nanocode/traces/).",
    )
    parser.add_argument("session", nargs="?",
                        help="session id prefix or 'latest' (omit to list all sessions)")
    parser.add_argument("--summary", action="store_true",
                        help="show an aggregate summary instead of the timeline")
    parser.add_argument("--full", action="store_true",
                        help="expand long content (messages / tool results)")
    args = parser.parse_args(argv)

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
