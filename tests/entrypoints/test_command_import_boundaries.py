import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_builtin_commands_import_without_agent_or_memory_dependencies():
    src = Path(__file__).resolve().parents[2] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")
    code = textwrap.dedent(
        """
        import importlib.abc
        import sys

        class Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                blocked = (
                    fullname in {"anthropic", "yaml"}
                    or fullname == "nanocode.agent"
                    or fullname.startswith("nanocode.agent.")
                    or fullname == "nanocode.memory"
                    or fullname.startswith("nanocode.memory.")
                )
                if blocked:
                    raise AssertionError(f"blocked import: {fullname}")
                return None

        sys.meta_path.insert(0, Blocker())
        import nanocode.entrypoints.commands.builtin
        """
    )
    proc = subprocess.run([sys.executable, "-c", code], env=env, text=True,
                          capture_output=True)
    assert proc.returncode == 0, proc.stderr


def test_agent_and_agent_profile_public_imports_are_light():
    src = Path(__file__).resolve().parents[2] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")
    code = textwrap.dedent(
        """
        import importlib.abc
        import sys

        class Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                blocked = fullname in {"anthropic", "openai", "yaml"}
                if blocked:
                    raise AssertionError(f"blocked import: {fullname}")
                return None

        sys.meta_path.insert(0, Blocker())
        import nanocode.agent
        from nanocode.agent import AgentConfig, AgentRuntime
        import nanocode.runtime
        from nanocode.runtime import AgentRuntime as RuntimeAgentRuntime
        import nanocode.session
        from nanocode.session import AgentSession
        import nanocode.agents
        from nanocode.agents import AgentProfile
        """
    )
    proc = subprocess.run([sys.executable, "-c", code], env=env, text=True,
                          capture_output=True)
    assert proc.returncode == 0, proc.stderr


def test_cli_help_lists_runtime_shell_escape():
    from nanocode.entrypoints import cli

    assert "!<command>" in cli._repl_commands_help()
