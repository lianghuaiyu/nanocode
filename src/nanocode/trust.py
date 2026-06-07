"""工作区信任层：在加载任何项目侧 .nanocode/ 配置之前确认用户信任当前工作区。

要点：
- 单一前置闸：放在 `Agent()` 构造之前，一道闸覆盖全部项目侧加载点。
- 存储落在 `data_dir()/trust.json`（默认 ~/.nanocode），绝不放进项目 `.nanocode/`。
- 存真实路径（非 sha256），因为祖先 walk 要靠路径比较；`_key_path` 贴 CC：
  优先 git toplevel，否则 `resolve(cwd)`。
- HOME 特例：cwd==Path.home() 仅记内存、不落盘（贴 CC，下次重问）。
- 非交互（one-shot / 非 TTY）→ 隐式信任（贴 CC）。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .paths import trust_file

# 本会话内的内存态信任集合（用于 HOME 特例：信任但不落盘）。
_session_trusted: set[str] = set()


def _git_toplevel(cwd: Path) -> Path | None:
    """返回 cwd 所在 git 仓库的根目录；非 git 或失败时返回 None。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
    except (OSError, ValueError):
        return None
    if out.returncode != 0:
        return None
    top = out.stdout.strip()
    if not top:
        return None
    return Path(top)


def _key_path(cwd: Path) -> str:
    """归一化的工作区身份：git root（同 repo 共享身份）否则 resolve(cwd)。"""
    root = _git_toplevel(cwd) or cwd.resolve()
    return str(root.resolve())


def _load_store() -> dict:
    """读取 trust.json（{path: true}）；不存在/损坏→空 dict。"""
    tf = trust_file()
    try:
        return json.loads(tf.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save_store(store: dict) -> None:
    """落盘 trust.json。"""
    tf = trust_file()
    tf.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def is_trusted(cwd: Path) -> bool:
    """祖先 walk：从 _key_path(cwd) 向上逐级查 store / 本会话内存，任一命中即信任。"""
    store = _load_store()
    p = Path(_key_path(cwd))
    for ancestor in [p, *p.parents]:
        key = str(ancestor)
        if store.get(key) is True or key in _session_trusted:
            return True
    return False


def record_trust(cwd: Path) -> None:
    """记住对 cwd 的信任。cwd.resolve()==Path.home() 时仅记内存（不落盘，贴 CC）。"""
    key = _key_path(cwd)
    if cwd.resolve() == Path.home():
        _session_trusted.add(key)
        return
    store = _load_store()
    store[key] = True
    _save_store(store)


def ensure_workspace_trust(cwd: Path, *, interactive: bool, input_fn=input) -> bool:
    """工作区信任闸：返回 True 表示可继续构造 Agent，否则 raise SystemExit(1)。

    - 已信任 → True（不弹对话）。
    - 不信任 + 非交互 → True（隐式信任，贴 CC -p/SDK/非 TTY）。
    - 不信任 + 交互 → 弹 y/n：y 则 record_trust 并返回 True，否则 raise SystemExit(1)。
    """
    if is_trusted(cwd):
        return True
    if not interactive:
        return True

    print(
        "⚠ 工作区信任确认\n"
        f"  当前目录：{cwd}\n"
        "  这是你创建或信任的项目吗？nanocode 将加载此目录下的 .nanocode/ 配置\n"
        "  （权限规则、MCP server、技能、子 agent）并可读改/执行其中文件。\n"
        "  若非你的项目，请先退出审查内容。\n"
    )
    try:
        answer = input_fn("  信任此工作区并继续？[y]es / [N]o（退出）：")
    except EOFError:
        answer = ""
    if str(answer).strip().lower() in ("y", "yes"):
        record_trust(cwd)
        return True
    raise SystemExit(1)
