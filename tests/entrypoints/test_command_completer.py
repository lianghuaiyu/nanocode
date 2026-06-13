"""Tests for the slash-command completer (fuzzy match + description menu)."""

import asyncio

from prompt_toolkit.document import Document
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from nanocode.entrypoints import cli


def _complete(text):
    """Run the production fuzzy-wrapped command completer over `text`."""
    comp = cli.FuzzyCompleter(cli._CommandCompleter(), pattern=r"^[a-zA-Z0-9_-]+")
    doc = Document(text, len(text))
    return list(comp.get_completions(doc, None))


def test_bare_slash_lists_all_builtins():
    """A single '/' offers every built-in command."""
    out = _complete("/")
    texts = {c.text for c in out}
    for name, _desc in cli._BUILTIN_COMMANDS:
        assert name in texts


def test_completions_carry_descriptions():
    """Each candidate exposes a non-empty description (display_meta)."""
    out = _complete("/co")
    assert out
    for c in out:
        assert c.display_meta_text  # right-aligned description shown in the menu


def test_fuzzy_subsequence_match():
    """'/cmt' subsequence-matches '/compact' (the Claude Code-style fuzzy behavior)."""
    texts = {c.text for c in _complete("/cmt")}
    assert "/compact" in texts


def test_prefix_still_matches():
    """Prefix queries still work: '/cost' resolves to /cost."""
    texts = {c.text for c in _complete("/cst")}
    assert "/cost" in texts


def test_non_slash_input_yields_nothing():
    """Plain prose (no leading '/') must not trigger the command menu."""
    assert _complete("hello world") == []


def test_space_after_command_disables_menu():
    """Once a command token is complete (has a space), stop offering command names."""
    assert _complete("/cost ") == []


def test_hyphenated_command_narrows():
    """Hyphenated commands narrow correctly (regression: default fuzzy word pattern
    breaks at '-', so '/task-' showed every command and '/task-s' buried /task-stop)."""
    # '/task-' should narrow to the hyphenated command, not list everything.
    dash = [c.text for c in _complete("/task-")]
    assert dash == ["/task-stop"]
    # '/task-s' keeps it as the top (and only) match.
    assert "/task-stop" in {c.text for c in _complete("/task-s")}


def test_menu_auto_pops_while_typing():
    """Typing '/co' auto-pops the completion menu (Claude Code-style), i.e. the
    buffer's complete_state is populated without pressing Tab. docs/18: the completer
    now lives on the TuiApp input buffer (complete_while_typing) instead of PromptSession."""
    from nanocode.tui.app import TuiApp

    async def scenario():
        with create_pipe_input() as pipe:
            comp = cli.FuzzyCompleter(cli._CommandCompleter(), pattern=r"^[a-zA-Z0-9_-]+")
            app = TuiApp(input=pipe, output=DummyOutput(), completer=comp)
            task = asyncio.create_task(app.run(patch=False))
            await asyncio.sleep(0.15)
            pipe.send_text("/co")            # type, do NOT press Tab/Enter
            await asyncio.sleep(0.4)          # let the async completer run
            state = app.input_buffer.complete_state
            n = len(state.completions) if state else 0
            app.request_exit()
            try:
                await asyncio.wait_for(task, timeout=2)
            except Exception:
                pass
            return n

    assert asyncio.run(scenario()) > 0

