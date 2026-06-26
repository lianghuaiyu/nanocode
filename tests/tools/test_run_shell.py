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
    # docs/24 Phase 3：run_shell 现有自包含 run（前台经 ctx.exec→SandboxManager，
    # 后台经 ctx.tasks.spawn_shell）；仍 host-routed（执行经能力把手，不经 execute.py 通用 handler）。
    from nanocode.tools import REGISTRY
    assert REGISTRY.get("run_shell").run is not None
