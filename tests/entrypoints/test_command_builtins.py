"""内置命令的行为测试：/help 列表（CMD-P1）。

docs/14 Milestone B：`/trace` REPL 命令随 Tracer/wire 退役一并删除——本文件原 /trace 失败隔离
用例已移除。"""

import asyncio

import pytest

from nanocode.entrypoints.commands.builtin import build_registry, _help
from nanocode.entrypoints.commands.types import CommandContext, Local


def _ctx():
    # /help 只用 args / ctx.registry，不碰 agent/session，故传 None 即可。
    return CommandContext(agent=None, session=None, out=None, registry=build_registry())


def test_help_lists_commands_and_escapes(capsys):
    """/help 输出含若干 registry 命令 + skill/shell 两条（命令行经 bare print，capsys 可捕获）。"""
    res = asyncio.run(_help(_ctx(), ""))
    assert isinstance(res, Local)
    out = capsys.readouterr().out
    assert "/clear" in out
    assert "/help" in out
    assert "/<skill-name>" in out
    assert "!<command>" in out
