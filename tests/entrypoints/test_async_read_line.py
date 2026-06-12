import asyncio
import signal

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from nanocode.entrypoints import cli


def test_async_read_line_returns_input():
    """The real prompt_toolkit path returns the typed line (no builtins.input)."""
    async def scenario():
        with create_pipe_input() as pipe:
            pipe.send_text("hello world\n")
            return await cli._async_read_line(input=pipe, output=DummyOutput())

    out = asyncio.run(scenario())
    assert out == "hello world"


def test_async_read_line_passes_prompt():
    """Prompt string is accepted and the line is read back through the session."""
    async def scenario():
        with create_pipe_input() as pipe:
            pipe.send_text("ok\n")
            return await cli._async_read_line("  > ", input=pipe, output=DummyOutput())

    out = asyncio.run(scenario())
    assert out == "ok"


def test_async_read_line_eof_returns_sentinel():
    """Ctrl-D (\\x04) on an empty buffer -> EOFError -> EOF sentinel."""
    async def scenario():
        with create_pipe_input() as pipe:
            pipe.send_text("\x04")
            return await cli._async_read_line(input=pipe, output=DummyOutput())

    out = asyncio.run(scenario())
    assert out is cli.EOF


def test_async_read_line_ctrl_c_returns_cancel():
    """Ctrl-C (\\x03) at the prompt -> KeyboardInterrupt -> CANCEL sentinel.

    The REPL loop relies on CANCEL (distinct from EOF) to clear the current line
    and, on a second consecutive press, to exit."""
    async def scenario():
        with create_pipe_input() as pipe:
            pipe.send_text("\x03")
            return await cli._async_read_line(input=pipe, output=DummyOutput())

    out = asyncio.run(scenario())
    assert out is cli.CANCEL


def test_event_loop_not_blocked_while_reading():
    """prompt_async cooperates with the loop: a concurrent ticker keeps running
    while the reader is parked waiting for input (proves no blocking call)."""
    async def scenario():
        progressed = []

        async def ticker():
            for _ in range(5):
                await asyncio.sleep(0.02)
                progressed.append(1)

        with create_pipe_input() as pipe:
            reader = asyncio.create_task(
                cli._async_read_line(input=pipe, output=DummyOutput())
            )
            tick = asyncio.create_task(ticker())
            # Let the reader park and the ticker make progress before sending input.
            await asyncio.sleep(0.2)
            pipe.send_text("done\n")
            line = await reader
            await tick
            return line, len(progressed)

    line, ticks = asyncio.run(scenario())
    assert line == "done"
    assert ticks == 5


def test_up_arrow_recalls_newest_first():
    """UP at a fresh prompt recalls the MOST RECENT history entry first, not the
    oldest. Guards the _make_prime_buffer ordering (regression: oldest-first
    iteration made UP surface the oldest command)."""
    from nanocode.paths import history_file

    # FileHistory format: each entry is "\n# <ts>\n+<line>\n"; newest is last.
    history_file().write_text("\n# t1\n+older\n\n# t2\n+newer\n", encoding="utf-8")

    async def scenario():
        with create_pipe_input() as pipe:
            pipe.send_text("\x1b[A\n")  # UP then ENTER
            return await cli._async_read_line(input=pipe, output=DummyOutput())

    out = asyncio.run(scenario())
    assert out == "newer"  # most recent, not "older"


def test_non_persistent_prompt_does_not_write_history():
    """Transient prompts (persistent=False, e.g. y/n confirmations) must not be
    written to the on-disk REPL history."""
    from nanocode.paths import history_file

    hf = history_file()

    async def scenario():
        with create_pipe_input() as pipe:
            pipe.send_text("y\n")
            return await cli._async_read_line(
                "Allow? ", persistent=False, input=pipe, output=DummyOutput()
            )

    out = asyncio.run(scenario())
    assert out == "y"
    assert (not hf.exists()) or ("+y" not in hf.read_text())


def test_sigint_handler_restored_after_read():
    """_async_read_line restores the caller's SIGINT handler on return, so Ctrl-C
    during agent processing still reaches the REPL handler (regression: older
    prompt_toolkit leaves SIGINT at SIG_DFL after prompt_async)."""
    sentinel = lambda *a: None  # noqa: E731 — stand-in for handle_sigint
    prev = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, sentinel)

        async def scenario():
            with create_pipe_input() as pipe:
                pipe.send_text("hi\n")
                await cli._async_read_line(input=pipe, output=DummyOutput())

        asyncio.run(scenario())
        assert signal.getsignal(signal.SIGINT) is sentinel
    finally:
        signal.signal(signal.SIGINT, prev)




def test_async_read_line_default_prefills_editor():
    """docs/16 pi /fork：default= 预填编辑器——直接回车则原样发回，也可先编辑。"""
    async def scenario():
        with create_pipe_input() as pipe:
            pipe.send_text("\n")                              # 直接接受预填内容
            return await cli._async_read_line(input=pipe, output=DummyOutput(),
                                              default="forked prompt")

    out = asyncio.run(scenario())
    assert out == "forked prompt"


def test_async_read_line_default_is_editable():
    async def scenario():
        with create_pipe_input() as pipe:
            pipe.send_text(" edited\n")                       # 在预填文本（光标在尾）后追加
            return await cli._async_read_line(input=pipe, output=DummyOutput(),
                                              default="forked prompt")

    out = asyncio.run(scenario())
    assert out == "forked prompt edited"
