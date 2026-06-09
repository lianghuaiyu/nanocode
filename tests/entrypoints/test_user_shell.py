import asyncio
from nanocode.entrypoints import cli


def _run(cmd):
    return asyncio.run(cli._run_user_shell(cmd))


def test_echo_stdout():
    out = _run("echo hello-bang")
    assert "$ echo hello-bang" in out
    assert "hello-bang" in out


def test_nonzero_exit_shown():
    out = _run("sh -c 'echo oops 1>&2; exit 3'")
    assert "oops" in out
    assert "(exit 3)" in out


def test_true_no_output():
    out = _run("true")
    assert "$ true" in out


def test_help_lists_bang():
    # !<command> shell escape 必须在 REPL 帮助里有文档；CMD-P1 后帮助由 _repl_commands_help() 生成
    # （--help 与 /help 共用），故断言渲染输出而非 main() 源码。
    assert "!<command>" in cli._repl_commands_help()
