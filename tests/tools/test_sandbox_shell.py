# tests/tools/test_sandbox_shell.py
import os
import shutil
import subprocess
import pytest

from nanocode.tools import sandbox_shell as ss
from nanocode.tools import sandbox_defaults as sd


def setup_function():
    sd.reset_defaults()


# ---- SCHEMA ----
def test_schema_shape():
    assert ss.SCHEMA["name"] == "sandbox_shell"
    props = ss.SCHEMA["input_schema"]["properties"]
    assert set(["command", "image", "persist", "network", "mount_workspace",
                "deps", "memory_mib", "cpus", "timeout_ms"]).issubset(props.keys())
    assert ss.SCHEMA["input_schema"]["required"] == ["command"]


# ---- 参数校验 ----
def test_positive_int_ok_and_default():
    assert ss._positive_int(None, 7, "x") == 7
    assert ss._positive_int(3, 7, "x") == 3


def test_positive_int_rejects_zero_and_nonint():
    with pytest.raises(ValueError):
        ss._positive_int(0, 1, "cpus")
    with pytest.raises(ValueError):
        ss._positive_int("abc", 1, "cpus")


def test_enum_validators():
    assert ss._network("none") == "none"
    assert ss._network("public") == "public"
    with pytest.raises(ValueError):
        ss._network("wifi")
    assert ss._deps("reuse") == "reuse"
    with pytest.raises(ValueError):
        ss._deps("garbage")


# ---- msb 定位 ----
def test_resolve_msb_prefers_env(monkeypatch, tmp_path):
    # tmp_path（cwd 外）造可执行 msb、basename==msb → _is_trusted_msb True，env 被采用。
    fake = tmp_path / "msb"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("NANOCODE_MSB_BIN", str(fake))
    assert ss._resolve_msb() == str(fake)


def test_resolve_msb_missing_returns_none(monkeypatch):
    # 不走 PATH（shutil.which 已移除）：清空 env + 钉死候选目录全不命中 → None。
    monkeypatch.delenv("NANOCODE_MSB_BIN", raising=False)
    monkeypatch.delenv("MSB_BIN", raising=False)
    monkeypatch.setattr(ss.os.path, "isfile", lambda _: False)
    assert ss._resolve_msb() is None


def test_resolve_msb_rejects_relative_env(monkeypatch, tmp_path):
    # 与 bwrap 的 A 同类：相对 env 值（cwd 可注入 ./msb）必须被拒，不得返回。
    monkeypatch.delenv("MSB_BIN", raising=False)
    monkeypatch.setenv("NANOCODE_MSB_BIN", "msb")  # 相对 → 拒绝
    monkeypatch.setattr(ss.os.path, "isfile", lambda _: False)  # 候选也不命中
    assert ss._resolve_msb() is None


def test_resolve_msb_ignores_cwd_msb(monkeypatch, tmp_path):
    # PATH 含 cwd + 造可执行 ./msb：_resolve_msb 不走 PATH → 不返回它。
    monkeypatch.delenv("NANOCODE_MSB_BIN", raising=False)
    monkeypatch.delenv("MSB_BIN", raising=False)
    monkeypatch.chdir(tmp_path)
    fake = tmp_path / "msb"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    # 钉死可信候选全不命中，证明绝不回退到 cwd/PATH 里的 ./msb。
    monkeypatch.setattr(ss.os.path, "isfile", lambda _: False)
    assert ss._resolve_msb() != str(fake)
    assert ss._resolve_msb() is None


def test_resolve_msb_from_trusted_candidate(monkeypatch, tmp_path):
    # 可信候选目录命中（~/.microsandbox/bin/msb 等）：绝对路径返回。
    monkeypatch.delenv("NANOCODE_MSB_BIN", raising=False)
    monkeypatch.delenv("MSB_BIN", raising=False)
    fake = tmp_path / "msb"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(ss.os.path, "isfile", lambda p: p == str(fake))
    monkeypatch.setattr(ss.os, "access", lambda p, m: p == str(fake))
    # 钉死可信目录元组，使候选目录的 msb 落到 fake 路径上。
    monkeypatch.setattr(ss, "_TRUSTED_MSB_DIRS", (str(tmp_path),))
    assert ss._resolve_msb() == str(fake)


# ---- 启动器劫持收口（round-4）：env msb 严格校验 ----
def test_is_trusted_msb_rejects_non_msb_basename(monkeypatch, tmp_path):
    # /bin/sh 等非 msb 启动器：basename != msb → 拒（核心 RCE 收口）。
    sh = tmp_path / "sh"
    sh.write_text("#!/bin/sh\n")
    sh.chmod(0o755)
    assert ss._is_trusted_msb(str(sh)) is False


def test_is_trusted_msb_rejects_relative(monkeypatch):
    assert ss._is_trusted_msb("msb") is False
    assert ss._is_trusted_msb("./msb") is False
    assert ss._is_trusted_msb("") is False


def test_is_trusted_msb_rejects_nonexistent(monkeypatch, tmp_path):
    assert ss._is_trusted_msb(str(tmp_path / "msb")) is False  # 不存在


def test_is_trusted_msb_rejects_non_executable(monkeypatch, tmp_path):
    f = tmp_path / "msb"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o644)  # 不可执行
    assert ss._is_trusted_msb(str(f)) is False


def test_is_trusted_msb_rejects_inside_cwd(monkeypatch, tmp_path):
    # cwd 内造可执行 msb：指向 repo 文件 → 拒（拒 .env 把 msb 指回 repo 脚本）。
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "msb"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o755)
    assert ss._is_trusted_msb(str(f)) is False


def test_is_trusted_msb_accepts_valid_outside_cwd(monkeypatch, tmp_path):
    # 绝对、存在、可执行、basename==msb、cwd 外 → True。
    outside = tmp_path / "bin"
    outside.mkdir()
    f = outside / "msb"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o755)
    # cwd 设到另一个不含 f 的目录，确保 f 在 cwd 外。
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    assert ss._is_trusted_msb(str(f)) is True


def test_resolve_msb_rejects_bin_sh_launcher_hijack(monkeypatch):
    # 核心载荷：NANOCODE_MSB_BIN=/bin/sh → 绝不返回 /bin/sh（落回候选或 None）。
    monkeypatch.delenv("MSB_BIN", raising=False)
    monkeypatch.setenv("NANOCODE_MSB_BIN", "/bin/sh")
    assert ss._resolve_msb() != "/bin/sh"


def test_resolve_msb_rejects_cwd_msb_env(monkeypatch, tmp_path):
    # cwd 内造可执行 msb + env 指过去 → 被拒（不返回 cwd 内的 msb）。
    monkeypatch.delenv("MSB_BIN", raising=False)
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "msb"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o755)
    monkeypatch.setenv("NANOCODE_MSB_BIN", str(f))
    # 钉死可信目录全不命中，证明 cwd 内的 msb 绝不被采用。
    monkeypatch.setattr(ss, "_TRUSTED_MSB_DIRS", ())
    assert ss._resolve_msb() != str(f)
    assert ss._resolve_msb() is None


def test_resolve_msb_accepts_valid_env_outside_cwd(monkeypatch, tmp_path):
    # tmp_path（cwd 外）造合法 msb + env 指过去 → 命中返回。
    monkeypatch.delenv("MSB_BIN", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    f = tmp_path / "msb"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o755)
    monkeypatch.setenv("NANOCODE_MSB_BIN", str(f))
    assert ss._resolve_msb() == str(f)


def _run_cmd(inp):
    return ss.build_msb_command({**inp, "persist": False}, msb="msb")


def test_run_basic_offline_default(monkeypatch):
    cmd = _run_cmd({"command": "echo hi", "deps": "none"})
    assert cmd[:2] == ["msb", "run"]
    assert "-q" in cmd
    assert "--no-net" in cmd                       # 实测：默认联网，必须主动断
    assert "--memory" in cmd and "1024M" in cmd     # 实测：带单位
    assert "--cpus" in cmd and "1" in cmd
    # 结构：... <image> -- /bin/sh -c <command>
    i = cmd.index("--")
    assert cmd[i:i+3] == ["--", "/bin/sh", "-c"]
    assert cmd[i+3] == "echo hi"
    assert cmd[i-1] == "python:3.12"


def test_run_public_network_omits_no_net(monkeypatch):
    monkeypatch.setenv("NANOCODE_MSB_BIN", "msb")
    cmd = _run_cmd({"command": "x", "network": "public", "deps": "none"})
    assert "--no-net" not in cmd


def test_run_deps_reuse_readonly_with_pythonpath(monkeypatch):
    monkeypatch.setenv("NANOCODE_MSB_BIN", "msb")
    cmd = _run_cmd({"command": "x", "deps": "reuse"})
    assert "--volume" in cmd
    assert f"{ss.DEPS_VOLUME}:{ss.DEPS_MOUNT}:ro" in cmd
    assert "-e" in cmd and f"PYTHONPATH={ss.DEPS_MOUNT}" in cmd   # 实测：-e PYTHONPATH=/deps 可用


def test_run_deps_install_readwrite(monkeypatch):
    monkeypatch.setenv("NANOCODE_MSB_BIN", "msb")
    cmd = _run_cmd({"command": "x", "deps": "install"})
    assert f"{ss.DEPS_VOLUME}:{ss.DEPS_MOUNT}:rw" in cmd


def test_run_deps_install_injects_pip_target(monkeypatch):
    # deps=install 时自动注入 PIP_TARGET=/deps，裸 pip install 也会装进卷（实测可用）
    monkeypatch.setenv("NANOCODE_MSB_BIN", "msb")
    cmd = _run_cmd({"command": "pip install rich", "deps": "install"})
    assert "-e" in cmd and f"PIP_TARGET={ss.DEPS_MOUNT}" in cmd


def test_run_deps_reuse_no_pip_target(monkeypatch):
    # reuse 是只读，不应注入 PIP_TARGET（否则误写只读卷会失败）
    monkeypatch.setenv("NANOCODE_MSB_BIN", "msb")
    cmd = _run_cmd({"command": "x", "deps": "reuse"})
    assert f"PIP_TARGET={ss.DEPS_MOUNT}" not in cmd


def test_run_mount_workspace_rw_and_workdir(monkeypatch, tmp_path):
    monkeypatch.setenv("NANOCODE_MSB_BIN", "msb")
    monkeypatch.chdir(tmp_path)
    cmd = _run_cmd({"command": "pytest", "mount_workspace": True, "deps": "none"})
    assert f"{tmp_path.resolve()}:{ss.WORKSPACE_MOUNT}:rw" in cmd
    assert "--workdir" in cmd and ss.WORKSPACE_MOUNT in cmd


def test_run_timeout_ms_to_seconds(monkeypatch):
    monkeypatch.setenv("NANOCODE_MSB_BIN", "msb")
    cmd = _run_cmd({"command": "x", "timeout_ms": 5000, "deps": "none"})
    assert "--timeout" in cmd
    assert "5s" in cmd


def test_run_session_default_applied(monkeypatch):
    monkeypatch.setenv("NANOCODE_MSB_BIN", "msb")
    sd.set_default("network", "public")
    cmd = _run_cmd({"command": "x", "deps": "none"})   # network 未显式传 → 取会话默认 public
    assert "--no-net" not in cmd


def test_explicit_overrides_session_default(monkeypatch):
    monkeypatch.setenv("NANOCODE_MSB_BIN", "msb")
    sd.set_default("network", "public")
    cmd = _run_cmd({"command": "x", "network": "none", "deps": "none"})  # 显式 > 会话
    assert "--no-net" in cmd


def test_build_create_command_has_resources(monkeypatch):
    cmd = ss.build_create_command(ss._merge_params({"command": "x", "persist": True, "deps": "none"}),
                                  "msb", "nanocode-sbx-abc")
    assert cmd[:2] == ["msb", "create"]
    assert "--name" in cmd and "nanocode-sbx-abc" in cmd
    assert "--no-net" in cmd
    assert cmd[-1] == "python:3.12"          # create 末尾是 image，无 -- command


def test_build_exec_command_wraps_with_sentinel(monkeypatch):
    p = ss._merge_params({"command": "pytest; exit 5", "persist": True})
    cmd = ss.build_exec_command(p, "msb", "nanocode-sbx-abc")
    assert cmd[:2] == ["msb", "exec"]
    assert "nanocode-sbx-abc" in cmd
    i = cmd.index("--")
    assert cmd[i:i+3] == ["--", "/bin/sh", "-c"]
    script = cmd[i+3]
    assert "pytest; exit 5" in script
    assert '__NCEC__=$?' in script           # 实测：exec 不透传退出码，需哨兵


def test_exec_workdir_when_mount(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    p = ss._merge_params({"command": "x", "persist": True, "mount_workspace": True})
    cmd = ss.build_exec_command(p, "msb", "nanocode-sbx-abc")
    assert "--workdir" in cmd and ss.WORKSPACE_MOUNT in cmd


def test_build_msb_command_persist_returns_exec(monkeypatch):
    cmd = ss.build_msb_command({"command": "x", "persist": True, "deps": "none"}, msb="msb")
    assert cmd[:2] == ["msb", "exec"]


def test_run_msb_missing_returns_graceful(monkeypatch):
    monkeypatch.setattr(ss, "_resolve_msb", lambda: None)
    out = ss.run({"command": "echo hi"})
    assert "Microsandbox" in out and "run_shell" in out


def test_run_formats_success(monkeypatch):
    monkeypatch.setattr(ss, "_resolve_msb", lambda: "msb")
    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 0, stdout="hello\n", stderr="")
    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    out = ss.run({"command": "echo hello", "deps": "none"})
    assert out.strip() == "hello"


def test_run_formats_nonzero(monkeypatch):
    monkeypatch.setattr(ss, "_resolve_msb", lambda: "msb")
    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 7, stdout="out\n", stderr="err\n")
    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    out = ss.run({"command": "exit 7", "deps": "none"})
    assert "exit code 7" in out
    assert "out" in out and "err" in out


def _capture_calls(monkeypatch):
    calls = []
    def fake_run(args, **kw):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")
    monkeypatch.setattr(ss, "_resolve_msb", lambda: "msb")
    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    return calls


def test_run_deps_reuse_ensures_volume_exists(monkeypatch):
    # 默认 deps=reuse 不能假设卷已存在：run() 必须幂等建卷，否则空环境/卷被删时挂载失败
    calls = _capture_calls(monkeypatch)
    ss.run({"command": "echo hi", "network": "none", "deps": "reuse"})
    assert [ss._resolve_msb() or "msb", "volume", "create", ss.DEPS_VOLUME] in calls


def test_run_deps_install_ensures_volume_exists(monkeypatch):
    calls = _capture_calls(monkeypatch)
    ss.run({"command": "pip install x", "network": "public", "deps": "install"})
    assert [ss._resolve_msb() or "msb", "volume", "create", ss.DEPS_VOLUME] in calls


def test_run_deps_none_skips_volume_create(monkeypatch):
    calls = _capture_calls(monkeypatch)
    ss.run({"command": "echo hi", "network": "none", "deps": "none"})
    assert not any(c[:3] == ["msb", "volume", "create"] for c in calls)


def test_run_timeout(monkeypatch):
    monkeypatch.setattr(ss, "_resolve_msb", lambda: "msb")
    def fake_run(args, **kw):
        raise subprocess.TimeoutExpired(args, 5)
    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    out = ss.run({"command": "sleep 99", "timeout_ms": 5000, "deps": "none"})
    assert "timed out" in out.lower()


def test_run_invalid_param_returns_error(monkeypatch):
    monkeypatch.setattr(ss, "_resolve_msb", lambda: "msb")
    out = ss.run({"command": "x", "network": "wifi"})
    assert out.startswith("Error:")


def test_persist_exec_parses_sentinel(monkeypatch):
    monkeypatch.setattr(ss, "_resolve_msb", lambda: "msb")
    monkeypatch.setattr(ss, "_ensure_persist_sandbox", lambda name, p, msb: (None, None))  # 假装已就绪
    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 0, stdout="real-output\n__NCEC__=5\n", stderr="")
    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    out = ss.run({"command": "exit 5", "persist": True, "deps": "none"})
    assert "exit code 5" in out
    assert "real-output" in out
    assert "__NCEC__" not in out          # 哨兵行已剥离


def _msb_available():
    return ss._resolve_msb() is not None


# ---- 配置指纹：用于判断 persist 沙箱是否需重建 ----
def test_config_fingerprint_changes_with_network():
    a = ss._persist_fingerprint(ss._merge_params({"command": "x", "persist": True, "network": "none"}))
    b = ss._persist_fingerprint(ss._merge_params({"command": "x", "persist": True, "network": "public"}))
    assert a != b


def test_config_fingerprint_ignores_command():
    a = ss._persist_fingerprint(ss._merge_params({"command": "echo 1", "persist": True}))
    b = ss._persist_fingerprint(ss._merge_params({"command": "echo 2", "persist": True}))
    assert a == b


# ---- 真沙箱集成测试（仅本机有 msb 时跑）----
@pytest.mark.skipif(not _msb_available(), reason="msb not installed")
def test_integration_echo_offline():
    out = ss.run({"command": "echo hello-sandbox", "network": "none", "deps": "none"})
    assert "hello-sandbox" in out


@pytest.mark.skipif(not _msb_available(), reason="msb not installed")
def test_integration_exit_code_passthrough_run():
    out = ss.run({"command": "exit 7", "network": "none", "deps": "none"})
    assert "exit code 7" in out


@pytest.mark.skipif(not _msb_available(), reason="msb not installed")
def test_integration_deps_readonly_blocks_write():
    out = ss.run({"command": "echo x > /deps/should_fail.txt", "deps": "reuse", "network": "none"})
    assert "Read-only" in out or "failed" in out or "cannot" in out.lower()


def test_persist_rebuild_emits_notice(monkeypatch):
    # persist 沙箱因配置变更被重建时，应返回提示（避免用户以为状态还在）
    name = "nanocode-sbx-rebuild-test"
    ss._persist_fingerprints[name] = "OLD-FP"   # 预置旧指纹 → 与新配置不符
    calls = []
    def fake_run(args, **kw):
        calls.append(list(args))
        if len(args) > 1 and args[1] == "list":
            return subprocess.CompletedProcess(args, 0, stdout=name + "\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    p = ss._merge_params({"command": "x", "persist": True, "network": "public", "deps": "none"})
    err, notice = ss._ensure_persist_sandbox(name, p, "msb")
    assert err is None
    assert notice and "rebuilt" in notice.lower()
    assert any(c[1] == "stop" for c in calls) and any(c[1] == "rm" for c in calls)
    ss._persist_fingerprints.pop(name, None)


def test_persist_reuse_no_notice(monkeypatch):
    name = "nanocode-sbx-reuse-test"
    p = ss._merge_params({"command": "x", "persist": True, "network": "none", "deps": "none"})
    ss._persist_fingerprints[name] = ss._persist_fingerprint(p)   # 指纹一致 → 复用
    def fake_run(args, **kw):
        if len(args) > 1 and args[1] == "list":
            return subprocess.CompletedProcess(args, 0, stdout=name + "\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    err, notice = ss._ensure_persist_sandbox(name, p, "msb")
    assert err is None and notice is None
    ss._persist_fingerprints.pop(name, None)


def test_persist_rebuild_notice_surfaces_in_run(monkeypatch):
    # 端到端：重建提示应出现在 run() 返回结果的开头
    monkeypatch.setattr(ss, "_resolve_msb", lambda: "msb")
    monkeypatch.setattr(ss, "_ensure_persist_sandbox",
                        lambda name, p, msb: (None, "⚠ persist sandbox rebuilt (config changed); prior in-sandbox state discarded"))
    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 0, stdout="hi\n__NCEC__=0\n", stderr="")
    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    out = ss.run({"command": "echo hi", "persist": True, "deps": "none"})
    assert "rebuilt" in out.lower()
    assert "hi" in out


def test_schema_has_trace():
    assert "trace" in ss.SCHEMA["input_schema"]["properties"]


def test_merge_params_trace_default_false():
    p = ss._merge_params({"command": "x"})
    assert p["trace"] is False
    assert p["trace_tag"] is None


def test_merge_params_trace_tag_persist(monkeypatch):
    p = ss._merge_params({"command": "x", "trace": True, "persist": True, "_session_id": "ABC"})
    assert p["trace"] is True
    assert p["trace_tag"] == "nanocode-sbx-ABC"


def test_merge_params_trace_tag_ephemeral(monkeypatch):
    monkeypatch.setenv("NANOCODE_SESSION_ID", "ABC")
    p1 = ss._merge_params({"command": "x", "trace": True, "persist": False})
    p2 = ss._merge_params({"command": "x", "trace": True, "persist": False})
    assert p1["trace_tag"].startswith("eph-")
    assert p1["trace_tag"] != p2["trace_tag"]   # 计数器递增，不互相覆盖


def test_trace_flags_mount_and_env(monkeypatch):
    monkeypatch.setenv("NANOCODE_MSB_BIN", "msb")
    monkeypatch.setenv("NANOCODE_TRACE_DIR", "")  # 用默认
    cmd = ss.build_msb_command({"command": "x", "trace": True, "deps": "none", "_session_id": "ABC"})
    assert any(f":{ss.TRACE_MOUNT}:rw" in a for a in cmd)         # 挂了 /trace 卷
    assert "NANOCODE_TRACE=1" in cmd
    assert f"NANOCODE_TRACE_DIR={ss.TRACE_MOUNT}" in cmd
    assert "NANOCODE_TRACE_PARENT=ABC" in cmd


def test_no_trace_no_trace_flags(monkeypatch):
    monkeypatch.setenv("NANOCODE_MSB_BIN", "msb")
    cmd = ss.build_msb_command({"command": "x", "deps": "none"})
    assert not any("/trace" in a for a in cmd)
    assert not any("NANOCODE_TRACE" in a for a in cmd)


def test_run_trace_creates_dir_and_marks_result(monkeypatch, tmp_path):
    monkeypatch.setenv("NANOCODE_MSB_BIN", "msb")
    monkeypatch.setenv("NANOCODE_TRACE_DIR", str(tmp_path))
    monkeypatch.setattr(ss, "_resolve_msb", lambda: "msb")
    def fake_run(args, **kw):
        return subprocess.CompletedProcess(args, 0, stdout="hi\n", stderr="")
    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    out = ss.run({"command": "echo hi", "trace": True, "deps": "none", "_session_id": "ABC"})
    # 子目录被创建
    sandbox_root = tmp_path / "ABC" / "sandbox"
    assert sandbox_root.is_dir()
    # 结果带 [sandbox-trace] 标记行
    assert "[sandbox-trace]" in out
    assert "hi" in out


@pytest.mark.skipif(not _msb_available(), reason="msb not installed")
def test_integration_trace_bridge_collects_child(monkeypatch, tmp_path):
    monkeypatch.setenv("NANOCODE_TRACE_DIR", str(tmp_path))
    # 沙箱内用 sh 模拟 agent 写一行 trace 到 /trace（_session_id 显式注入，env 回退已删）
    out = ss.run({
        "command": "echo '{\"seq\":0,\"type\":\"tool_call\",\"tool\":\"x\"}' >> /trace/child.jsonl && echo done",
        "trace": True, "network": "none", "deps": "none", "_session_id": "itest",
    })
    assert "done" in out
    # 宿主子目录应出现 child.jsonl，内容完整
    found = list((tmp_path / "itest" / "sandbox").rglob("child.jsonl"))
    assert found, "child trace not collected on host"
    assert "tool_call" in found[0].read_text()
