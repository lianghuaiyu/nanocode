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


def test_plan_mode_blocks_sandbox_shell():
    assert check_permission("sandbox_shell", {"command": "ls"}, "plan")["action"] == "deny"


def test_sandbox_mount_workspace_confirm():
    r = check_permission("sandbox_shell", {"command": "pytest", "mount_workspace": True}, "default")
    assert r["action"] == "confirm"
    assert "mount" in r["message"].lower()


def test_sandbox_network_public_confirm():
    r = check_permission("sandbox_shell", {"command": "x", "network": "public"}, "default")
    assert r["action"] == "confirm"
    assert "network" in r["message"].lower()


def test_sandbox_deps_install_confirm():
    r = check_permission("sandbox_shell", {"command": "pip install six", "deps": "install"}, "default")
    assert r["action"] == "confirm"
    assert "dep" in r["message"].lower()


def test_sandbox_default_no_confirm():
    r = check_permission("sandbox_shell", {"command": "echo hi"}, "default")
    assert r["action"] == "allow"


def test_dontask_denies_sandbox_confirm():
    r = check_permission("sandbox_shell", {"command": "x", "network": "public"}, "dontAsk")
    assert r["action"] == "deny"


def test_parse_rule_sandbox_shell():
    assert permissions._parse_rule("sandbox_shell(pytest *)") == {"tool": "sandbox_shell", "pattern": "pytest *"}
