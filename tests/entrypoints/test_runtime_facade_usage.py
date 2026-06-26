"""docs/23 Phase 0：entrypoints/** 与 tui/** 不得 raw 访问 RuntimeThread 的 `.agent` /
`.session` 句柄（docs/23 §4.3 / §8.1）。

外部层只能经 facade 操作 thread；唯一允许的是 facade 方法（`.agent_definitions()` /
`.agents_overview()` / `.agent_detail()` 等——它们因 ``\\b`` 词界自然不匹配 ``\\.agent\\b``）。
本测试静态扫描源码，断言无 `thread.agent` / `_thread.agent` / `current_thread.agent`
（及 `.session` 同形）的 raw 句柄穿透。
"""

import pathlib
import re

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "nanocode"
_DIRS = ("entrypoints", "tui")

# raw 句柄访问：thread/_thread/current_thread（含 `ctx.thread` / `host.current_thread`
# 链式形态）后接 `.agent` / `.session`。``\\b`` 词界使 `.agent_definitions` /
# `.agents_overview` / `.session_id` / `.session_stats` 等 facade 方法/属性自然豁免。
_RAW_THREAD_ACCESS = re.compile(r"\b(?:thread|_thread|current_thread)\.(?:agent|session)\b")


def _py_files():
    for name in _DIRS:
        yield from (_SRC / name).rglob("*.py")


def test_entrypoints_and_tui_do_not_touch_raw_thread_agent_or_session():
    violations = []
    for path in _py_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("import ", "from ", "#")):
                continue
            if _RAW_THREAD_ACCESS.search(line):
                violations.append(f"{path.relative_to(_SRC)}:{lineno}: {stripped}")
    assert not violations, (
        "raw RuntimeThread agent/session access leaked into the external layer:\n"
        + "\n".join(violations)
    )
