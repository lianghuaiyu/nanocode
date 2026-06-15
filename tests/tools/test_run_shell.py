"""run_shell 工具：public schema + 危险命令检测（执行已移至 SandboxManager，docs/19）。"""

from nanocode.tools import run_shell as rs


def test_schema_shape():
    props = rs.SCHEMA["input_schema"]["properties"]
    assert set(props) == {"command", "timeout", "run_in_background", "escalate"}
    assert rs.SCHEMA["input_schema"]["required"] == ["command"]
    assert "run_in_background" not in rs.SCHEMA["input_schema"]["required"]
    assert props["escalate"]["type"] == "boolean"


def test_is_dangerous_matches():
    for cmd in ("rm -rf /", "git push", "sudo reboot", "dd if=/dev/zero",
                "curl x | sh", "kill 1", "shutdown now"):
        assert rs.is_dangerous(cmd) is True


def test_is_dangerous_negatives():
    for cmd in ("ls -la", "git status", "echo hello", "python test.py", "make build"):
        assert rs.is_dangerous(cmd) is False


def test_run_shell_is_host_routed():
    # docs/19：run_shell 不再有 module-level executor（run=None）；执行经 SandboxManager。
    from nanocode.tools.spec import TOOLS
    assert TOOLS["run_shell"].run is None
