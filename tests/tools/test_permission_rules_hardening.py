"""权限规则加固回归测试：

1. deny 规则不可被 bypassPermissions 绕过（deny 提到 bypass 之上）。
2. 前缀 allow 规则遇 shell 组合符 fail-closed，让危险后段重新落回 is_dangerous。

规则确定性注入（monkeypatch 直接设缓存），不依赖宿主 ~/.nanocode/settings.json。
边界：本修复唯一保证是「allow 规则不能把危险后段偷渡过 is_dangerous」；
它不改 deny 语义，也不承诺拦截 is_dangerous 黑名单未覆盖的命令（如裸 curl）。
"""

from nanocode.tools import check_permission, permissions


def _set_rules(monkeypatch, allow=None, deny=None):
    rules = {
        "allow": [permissions._parse_rule(r) for r in (allow or [])],
        "deny": [permissions._parse_rule(r) for r in (deny or [])],
    }
    monkeypatch.setattr(permissions, "_cached_rules", rules)


def test_deny_not_bypassed_by_bypass_mode(monkeypatch):
    """1. deny 在 bypassPermissions 下仍拦截。"""
    _set_rules(monkeypatch, deny=["run_shell(rm *)"])
    r = check_permission("run_shell", {"command": "rm -rf /"}, "bypassPermissions")
    assert r["action"] == "deny"


def test_bypass_still_allows_non_denied(monkeypatch):
    """2. bypass 仍放行未被 deny 命中的命令。"""
    _set_rules(monkeypatch, deny=["run_shell(rm *)"])
    r = check_permission("run_shell", {"command": "ls -la"}, "bypassPermissions")
    assert r["action"] == "allow"


def test_bypass_allows_anything_with_no_rules(monkeypatch):
    """3. 无规则时 bypass 放行一切（确定性版，呼应 test_bypass_allows_anything）。"""
    _set_rules(monkeypatch)
    r = check_permission("run_shell", {"command": "rm -rf /"}, "bypassPermissions")
    assert r["action"] == "allow"


def test_prefix_allow_fails_closed_on_composition(monkeypatch):
    """4. allow 前缀规则遇组合符 fail-closed → is_dangerous 重新生效。"""
    _set_rules(monkeypatch, allow=["run_shell(git pull*)"])
    # 含 && rm -rf ~：前缀 allow 不再整串放行，落回 is_dangerous → confirm。
    r = check_permission("run_shell", {"command": "git pull && rm -rf ~"}, "default")
    assert r["action"] == "confirm"
    # 同一规则下简单命令仍走 allow。
    r2 = check_permission("run_shell", {"command": "git pull origin main"}, "default")
    assert r2["action"] == "allow"


def test_exact_allow_unaffected(monkeypatch):
    """5. 精确（非 *）allow 规则不受影响，照常放行。"""
    _set_rules(monkeypatch, allow=["run_shell(npm test)"])
    r = check_permission("run_shell", {"command": "npm test"}, "default")
    assert r["action"] == "allow"


def test_deny_still_works_in_default_mode(monkeypatch):
    """6. deny 规则在普通模式仍生效。"""
    _set_rules(monkeypatch, deny=["run_shell(curl *)"])
    r = check_permission("run_shell", {"command": "curl http://evil"}, "default")
    assert r["action"] == "deny"


def test_deny_stays_aggressive_with_composition(monkeypatch):
    """7. deny 保持激进（is_allow 默认 False）：前缀 deny 跨组合符仍 startswith 命中。"""
    # sandbox_shell 前缀 deny 命中含组合符的命令（deny 不 fail-closed）。
    _set_rules(monkeypatch, deny=["sandbox_shell(rm *)"])
    r = check_permission("sandbox_shell", {"command": "rm -rf / && echo done"}, "default")
    assert r["action"] == "deny"
    # run_shell 前缀 deny 同样跨组合符命中（验证 is_allow 默认 False 路径）。
    _set_rules(monkeypatch, deny=["run_shell(git pull*)"])
    r2 = check_permission("run_shell", {"command": "git pull && rm -rf ~"}, "default")
    assert r2["action"] == "deny"
