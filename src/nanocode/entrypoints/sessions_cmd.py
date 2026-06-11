"""`nanocode sessions` 子命令：legacy → canonical 树迁移 + 盘点（docs/13 §10 / P7）。

  migrate <id>   —— 把一个 legacy session 导入 canonical `session.jsonl` 树。
  migrate all    —— 迁移所有 legacy session（已有树的自动跳过）。
  inspect <id>   —— 盘点：树 / legacy 是否存在、消息数。

只 append/新建，**不删** legacy 文件；幂等（已有树则跳过）。
"""
from __future__ import annotations

import argparse
import json

from ..session.migration import inspect_session, migrate_session
from ..ui import print_error, print_info


def _all_session_ids() -> list[str]:
    from ..paths import sessions_dir
    d = sessions_dir()
    out: set[str] = set()
    if d.exists():
        for f in d.glob("*.json"):
            out.add(f.stem)
        for e in d.iterdir():
            if e.is_dir():
                out.add(e.name)
    return sorted(out)


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="nanocode sessions",
        description="Migrate/inspect sessions (legacy flat/v2 → canonical session tree).",
    )
    sub = parser.add_subparsers(dest="cmd")
    p_mig = sub.add_parser("migrate", help="import legacy session(s) into the canonical tree")
    p_mig.add_argument("session", help="session id, or 'all'")
    p_ins = sub.add_parser("inspect", help="inspect tree/legacy state of a session")
    p_ins.add_argument("session", help="session id")
    args = parser.parse_args(argv)

    if args.cmd == "migrate":
        ids = _all_session_ids() if args.session == "all" else [args.session]
        if not ids:
            print_error("no sessions found")
            return 1
        for sid in ids:
            rep = migrate_session(sid)
            extra = f" ({rep['messages']} msgs)" if rep.get("status") == "migrated" else ""
            print_info(f"{sid}: {rep['status']}{extra}")
        return 0

    if args.cmd == "inspect":
        print_info(json.dumps(inspect_session(args.session), indent=2))
        return 0

    parser.print_help()
    return 1
