"""round-2 spec-2：单一 planner `run_shell.plan_shell(inp, *, context)` 决策矩阵。

confinement 是统一的 routing 决策——前台/后台/hook 三个 shell 入口共用同一个 planner。
本文件用 monkeypatch 钉死 classify / resolve_native_backend / _resolve_msb / shell_sandbox_mode，
逐格验证 (context × mode) → (kind, info)，不依赖宿主真实沙盒可用性。

矩阵（kind）：
  off / 只读 / escalate                         → host（任何 context）
  hook + 有原生后端（任何沙盒档）                  → sandbox（原生 OS 沙盒；不进 microVM、不裸跑）
  hook + 无原生后端（任何沙盒档）                  → blocked（fail-closed）
  seatbelt + 有后端                              → sandbox（任何 context）
  seatbelt + 无后端                              → blocked（fail-closed，带 escalate 指引）
  auto + 无 msb（foreground/background）           → blocked（C 残留：不再静默裸跑）
  auto + 有 msb，foreground                       → microvm
  auto + 有 msb，background                        → blocked（microVM 无法异步后台）
"""

import nanocode.tools.sandbox_backends as sb
from nanocode.tools import permissions, run_shell, sandbox_shell


class _FakeBackend:
    @staticmethod
    def build_argv(command, *, posture, cwd):
        return ["ARGV", posture, command]

    @staticmethod
    def run_structured(inp, *, posture="workspace-write", cwd=None):
        return {"exit_code": 0, "stdout": "OK", "stderr": "", "timed_out": False, "error": None}


def _force(monkeypatch, *, mode, classify="sandbox", backend=_FakeBackend, msb="/fake/msb"):
    """钉死 planner 的所有外部依赖，隔离决策逻辑。"""
    monkeypatch.setattr(permissions, "shell_sandbox_mode", lambda: mode)
    monkeypatch.setattr(permissions, "classify_shell_runtime", lambda cmd: classify)
    monkeypatch.setattr(sb, "resolve_native_backend", lambda: backend)
    monkeypatch.setattr(sandbox_shell, "_resolve_msb", lambda: msb)


CONTEXTS = ("foreground", "background", "hook")


# ─── escalate / off / 只读 → host（任何 context） ───────────────────────────


def test_escalate_returns_host_any_context(monkeypatch):
    _force(monkeypatch, mode="seatbelt")
    for ctx in CONTEXTS:
        assert run_shell.plan_shell({"command": "rm -rf x", "escalate": True}, context=ctx) == ("host", None)


def test_off_returns_host_any_context(monkeypatch):
    # off：classify 归类为 host（mode 走不到沙盒分支）
    _force(monkeypatch, mode="off", classify="host")
    for ctx in CONTEXTS:
        assert run_shell.plan_shell({"command": "python x.py"}, context=ctx) == ("host", None)


def test_readonly_returns_host_any_context(monkeypatch):
    _force(monkeypatch, mode="seatbelt", classify="host")
    for ctx in CONTEXTS:
        assert run_shell.plan_shell({"command": "git status"}, context=ctx) == ("host", None)


# ─── seatbelt：有后端→sandbox（含后端模块）；无后端→blocked ───────────────────


def test_seatbelt_backend_returns_sandbox_any_context(monkeypatch):
    _force(monkeypatch, mode="seatbelt")
    for ctx in CONTEXTS:
        kind, info = run_shell.plan_shell({"command": "make build"}, context=ctx)
        assert kind == "sandbox"
        assert info is _FakeBackend  # caller 用 backend.run_structured / build_argv


def test_seatbelt_no_backend_blocks_any_context(monkeypatch):
    _force(monkeypatch, mode="seatbelt", backend=None)
    for ctx in CONTEXTS:
        kind, reason = run_shell.plan_shell({"command": "make build"}, context=ctx)
        assert kind == "blocked"
        assert "escalate=true" in reason


# ─── auto（microVM）：foreground/background 敏感 ─────────────────────────────


def test_auto_no_msb_blocks_foreground_and_background(monkeypatch):
    # C 残留：auto + 无 msb 不再静默裸跑宿主 → blocked（hook 不依赖 msb，单独测）。
    _force(monkeypatch, mode="auto", msb=None)
    for ctx in ("foreground", "background"):
        kind, reason = run_shell.plan_shell({"command": "make build"}, context=ctx)
        assert kind == "blocked"
        assert "escalate=true" in reason


def test_auto_msb_foreground_returns_microvm(monkeypatch):
    _force(monkeypatch, mode="auto")
    assert run_shell.plan_shell({"command": "make build"}, context="foreground") == ("microvm", None)


def test_auto_msb_background_blocks(monkeypatch):
    # microVM 无法异步后台包裹 → blocked。
    _force(monkeypatch, mode="auto")
    kind, reason = run_shell.plan_shell({"command": "make build"}, context="background")
    assert kind == "blocked"
    assert "microVM" in reason or "auto" in reason


# ─── hook：任何沙盒档都用原生 OS 沙盒受限；无原生后端则 blocked；绝不 microVM/裸跑 ─────


def test_hook_auto_with_backend_returns_sandbox(monkeypatch):
    # 修复 2：auto 档 hook 不再裸跑宿主，改走原生后端受限（不进 microVM）。
    _force(monkeypatch, mode="auto")
    kind, info = run_shell.plan_shell({"command": "make build"}, context="hook")
    assert kind == "sandbox"
    assert info is _FakeBackend


def test_hook_seatbelt_with_backend_returns_sandbox(monkeypatch):
    _force(monkeypatch, mode="seatbelt")
    kind, info = run_shell.plan_shell({"command": "make build"}, context="hook")
    assert kind == "sandbox"
    assert info is _FakeBackend


def test_hook_no_backend_blocks_any_sandbox_mode(monkeypatch):
    # 无原生后端 → hook blocked（fail-closed），即便 auto 档有 msb 也不退化为 microVM/host。
    for mode in ("auto", "seatbelt"):
        _force(monkeypatch, mode=mode, backend=None)
        kind, reason = run_shell.plan_shell({"command": "make build"}, context="hook")
        assert kind == "blocked"
        assert "escalate=true" in reason


def test_hook_off_returns_host(monkeypatch):
    # off 档：classify→host 拦在 hook 分支之前 → 宿主（off=不沙盒，正确）。
    _force(monkeypatch, mode="off", classify="host")
    assert run_shell.plan_shell({"command": "python x.py"}, context="hook") == ("host", None)


def test_hook_escalate_returns_host(monkeypatch):
    _force(monkeypatch, mode="auto")
    assert run_shell.plan_shell(
        {"command": "make build", "escalate": True}, context="hook"
    ) == ("host", None)


# ─── plan_background 是 plan_shell(context="background") 的薄包装 ────────────


def test_plan_background_is_background_context(monkeypatch):
    _force(monkeypatch, mode="seatbelt")
    assert run_shell.plan_background({"command": "make build"}) == run_shell.plan_shell(
        {"command": "make build"}, context="background"
    )
