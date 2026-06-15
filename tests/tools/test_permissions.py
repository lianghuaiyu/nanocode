from nanocode.tools import check_permission, is_dangerous
from nanocode.tools import permissions


def test_read_tools_allow():
    assert check_permission("read_file", {"file_path": "x"}, "default")["action"] == "allow"


def test_bypass_allows_anything():
    assert check_permission("run_shell", {"command": "rm -rf /"}, "bypassPermissions")["action"] == "allow"


def test_plan_mode_blocks_non_plan_edit():
    r = check_permission("edit_file", {"file_path": "/x"}, "plan", plan_file_path="/plan.md")
    assert r["action"] == "deny"


def test_plan_mode_allows_plan_file():
    r = check_permission("edit_file", {"file_path": "/plan.md"}, "plan", plan_file_path="/plan.md")
    assert r["action"] == "allow"


def test_plan_mode_blocks_shell():
    assert check_permission("run_shell", {"command": "ls"}, "plan")["action"] == "deny"


def test_dangerous_shell_confirm():
    assert check_permission("run_shell", {"command": "rm file"}, "default")["action"] == "confirm"


def test_dontask_denies_confirm():
    assert check_permission("run_shell", {"command": "rm file"}, "dontAsk")["action"] == "deny"


def test_accept_edits_allows_existing_edit(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("x")
    assert check_permission("edit_file", {"file_path": str(p)}, "acceptEdits")["action"] == "allow"


def test_is_dangerous():
    assert is_dangerous("sudo reboot") is True
    assert is_dangerous("git push origin main") is True
    assert is_dangerous("echo hi") is False


def test_parse_rule():
    assert permissions._parse_rule("run_shell(ls *)") == {"tool": "run_shell", "pattern": "ls *"}
    assert permissions._parse_rule("read_file") == {"tool": "read_file", "pattern": None}


def test_plan_mode_blocks_run_shell():
    assert check_permission("run_shell", {"command": "ls"}, "plan")["action"] == "deny"


# docs/19：sandbox 默认常开，escalate 永远需明确审批（替代旧 sandbox_shell 参数确认）。
def test_escalate_confirms_default():
    r = check_permission("run_shell", {"command": "git status", "escalate": True}, "default")
    assert r["action"] == "confirm" and "escalate" in r["message"] and "host" in r["message"]


def test_escalate_confirms_under_bypass():
    r = check_permission("run_shell", {"command": "x", "escalate": True}, "bypassPermissions")
    assert r["action"] == "confirm"          # bypass 越不过 escalate 边界


def test_escalate_dontask_denies():
    r = check_permission("run_shell", {"command": "x", "escalate": True}, "dontAsk")
    assert r["action"] == "deny"


def test_non_escalate_normal_command_allowed():
    assert check_permission("run_shell", {"command": "echo hi"}, "default")["action"] == "allow"


def test_dangerous_run_shell_confirms():
    r = check_permission("run_shell", {"command": "rm -rf build"}, "default")
    assert r["action"] == "confirm"


def test_parse_rule_run_shell():
    assert permissions._parse_rule("run_shell(pytest *)") == {"tool": "run_shell", "pattern": "pytest *"}
