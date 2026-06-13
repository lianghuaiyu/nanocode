"""docs/15 Phase 3：/context 命令展示 ContextRuntime 组装的 packs + 预算 + survival matrix。"""

import asyncio

from nanocode.entrypoints.commands.builtin import _context
from nanocode.entrypoints.commands.types import Local


class _FakeThread:
    effective_window = 200000
    is_sub_agent = False


class _FakeCtx:
    thread = _FakeThread()


def test_context_command_returns_local_and_renders(capsys):
    res = asyncio.run(_context(_FakeCtx(), ""))
    assert isinstance(res, Local)
    out = capsys.readouterr().out
    # ledger summary header（token 计 + budget）出现
    assert "Context ledger" in out
    assert "tokens" in out


def test_context_command_registered():
    from nanocode.entrypoints.commands.builtin import _BUILTINS
    names = {c[0] for c in _BUILTINS}
    assert "/context" in names
