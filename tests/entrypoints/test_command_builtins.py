"""新增内置命令的行为测试：/trace 失败隔离（CMD-P2）、/help 列表（CMD-P1）。"""

import asyncio

import pytest

from nanocode.entrypoints.commands.builtin import build_registry, _trace, _help
from nanocode.entrypoints.commands.types import CommandContext, Local


def _ctx():
    # /trace、/help 只用 args / ctx.registry，不碰 agent/session，故传 None 即可。
    return CommandContext(agent=None, session=None, out=None, registry=build_registry())


@pytest.mark.parametrize("args", [
    "-h",              # argparse help → SystemExit(0)
    "--help",          # 同上
    "--bogus-flag",    # 未知 flag → argparse error → SystemExit(2)
    "'unterminated",   # shlex 未闭合引号 → ValueError
    '"also bad',
])
def test_trace_isolates_systemexit_and_shlex(args):
    """/trace 绝不能因 argparse 的 -h/坏参（SystemExit）或 shlex 引号错误（ValueError）杀掉 REPL。"""
    res = asyncio.run(_trace(_ctx(), args))   # 不抛即通过
    assert isinstance(res, Local)


def test_trace_is_registered_exact_or_prefix():
    r = build_registry()
    assert r.lookup("/trace") is not None          # 裸形列出 sessions
    assert r.lookup("/trace latest --wire") is not None
    assert r.lookup("/tracex") is None              # 非 /trace


def test_help_lists_commands_and_escapes(capsys):
    """/help 输出含若干 registry 命令 + skill/shell 两条（命令行经 bare print，capsys 可捕获）。"""
    res = asyncio.run(_help(_ctx(), ""))
    assert isinstance(res, Local)
    out = capsys.readouterr().out
    assert "/clear" in out
    assert "/trace" in out
    assert "/help" in out
    assert "/<skill-name>" in out
    assert "!<command>" in out
