"""Host-owned git worktree isolation for subagent runs."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..paths import data_dir


@dataclass(frozen=True)
class WorktreeRecord:
    path: str
    branch: str
    base_ref: str
    base_commit: str


def should_isolate(*, agent_type: str, parallel: bool, requested: str | None) -> str:
    if requested:
        if requested not in {"shared", "worktree"}:
            raise ValueError(f"invalid isolation: {requested}")
        return requested
    if parallel and agent_type in {"coder", "general"}:
        return "worktree"
    return "shared"


def create_worktree(parent_cwd: str, child_session_id: str) -> WorktreeRecord:
    cwd = Path(parent_cwd).resolve()
    probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(cwd),
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if probe.returncode != 0:
        raise RuntimeError("worktree isolation requires a git repository")
    root = Path(probe.stdout.strip()).resolve()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root),
                                     text=True).strip()
    project_hash = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    path = data_dir() / "worktrees" / project_hash / child_session_id
    path.parent.mkdir(parents=True, exist_ok=True)
    branch = "nanocode/" + child_session_id.replace("/", "-").replace(".", "-")
    if not path.exists():
        subprocess.check_call(["git", "worktree", "add", "-b", branch, str(path), commit],
                              cwd=str(root))
    return WorktreeRecord(path=str(path), branch=branch, base_ref="HEAD", base_commit=commit)


def diff_summary(worktree_path: str) -> str:
    out = subprocess.run(["git", "status", "--short"], cwd=worktree_path,
                         text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if out.returncode != 0:
        return out.stderr.strip()
    return out.stdout.strip()
