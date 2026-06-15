"""Registry-level dispatch table (CMD-P0).

直接断言 `build_registry().lookup(line)` 的 most-specific-first 解析与「非命令→None」语义，
与 cli.run_repl 驱动的 characterization 测试互补：前者锁 registry 本身的分发决策（不依赖
handler 行为/打桩），后者锁 run_repl 端到端落点。两者共同构成 CMD-P0 的 no-regression gate。
"""

import pytest

from nanocode.entrypoints.commands.builtin import _compact, build_registry
from nanocode.entrypoints.commands.types import CommandContext

_REG = build_registry()


def _name(line):
    cmd = _REG.lookup(line)
    return cmd.spec.name if cmd else None


@pytest.mark.parametrize("line, expected", [
    # 精确命令
    ("/clear", "/clear"),
    ("/plan", "/plan"),
    ("/cost", "/cost"),
    ("/compact", "/compact"),
    ("/compact focus on API decisions", "/compact"),
    ("/memory", "/memory"),
    ("/skills", "/skills"),
    # most-specific-first：更具体的多词命令先于较短前缀
    ("/memory consolidate", "/memory consolidate"),
    ("/memory optimize", "/memory optimize"),
    ("/memory eval generate", "/memory eval generate"),
    ("/memory eval", "/memory eval"),
    ("/memory eval pending", "/memory eval"),
    ("/memory eval confirm abc", "/memory eval"),
    # exact_or_prefix
    ("/sandbox", "/sandbox"),
    ("/sandbox network on", "/sandbox"),
    ("/tasks", "/tasks"),
    ("/tasks running", "/tasks"),
    ("/agents", "/agents"),
    ("/agents show x", "/agents"),
    # prefix-only（无裸形）
    ("/task abc", "/task"),
    ("/task-stop abc", "/task-stop"),
    ("/agent agent-001", "/agent"),
])
def test_lookup_resolves_expected_command(line, expected):
    assert _name(line) == expected


@pytest.mark.parametrize("line", [
    "",                       # 空
    "hello world",            # 普通 chat
    "/foo",                   # 未知斜杠 → 非命令（loop 再试 skill，否则 chat）
    "/clear extra",           # exact 命令带尾随参数 → 不匹配（保留旧行为）
    "/memoryx",               # 非 /memory
    "/task",                  # prefix 命令裸形（无参）→ 不匹配
    "/task-stop",             # 同上
    "/agent",                 # 同上
    "!ls",                    # shell（loop 处理）
    "exit",                   # 裸词（loop 处理）
])
def test_lookup_returns_none_for_non_commands(line):
    assert _REG.lookup(line) is None


def test_most_specific_first_generate_not_eaten_by_eval():
    # '/memory eval generate' 必须解析为自身，绝不被 '/memory eval' 前缀吞掉。
    assert _name("/memory eval generate") == "/memory eval generate"
    assert _name("/memory eval generated-typo") == "/memory eval"  # 非 generate 子命令 → eval 前缀


def test_specs_cover_first_batch():
    names = {s.name for s in _REG.specs()}
    assert {"/clear", "/plan", "/cost", "/compact", "/memory", "/memory eval",
            "/skills", "/sandbox", "/tasks", "/task", "/task-stop",
            "/agents", "/agent", "/resume", "/new", "/name", "/session",
            "/tree", "/fork", "/clone"} <= names
    assert "/export" not in names and "/share" not in names


def test_compact_accepts_optional_prompt_hint():
    spec = next(s for s in _REG.specs() if s.name == "/compact")
    assert spec.arg_hint == "[prompt]"


def test_compact_handler_passes_optional_prompt():
    import asyncio

    class _Thread:
        def __init__(self):
            self.instructions = "unset"

        async def compact(self, instructions=None):
            self.instructions = instructions

    thread = _Thread()
    asyncio.run(_compact(CommandContext(thread=thread), "focus on API decisions"))

    assert thread.instructions == "focus on API decisions"
