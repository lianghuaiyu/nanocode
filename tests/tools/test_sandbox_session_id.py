# tests/tools/test_sandbox_session_id.py
"""Task 1: sandbox_shell 显式 session_id（_session_id）优先，env 回退。"""

from nanocode.tools import sandbox_shell as ss
from nanocode.tools import sandbox_defaults as sd


def setup_function():
    sd.reset_defaults()


# ---- _session_id_of ----
def test_session_id_of_prefers_explicit(monkeypatch):
    monkeypatch.setenv("NANOCODE_SESSION_ID", "ENV")
    assert ss._session_id_of({"_session_id": "EXPLICIT"}) == "EXPLICIT"


def test_session_id_of_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("NANOCODE_SESSION_ID", "ENV")
    assert ss._session_id_of({}) == "ENV"


def test_session_id_of_default_when_no_env(monkeypatch):
    monkeypatch.delenv("NANOCODE_SESSION_ID", raising=False)
    assert ss._session_id_of({}) == "default"


# ---- _sandbox_name_for ----
def test_sandbox_name_for_uses_explicit(monkeypatch):
    monkeypatch.setenv("NANOCODE_SESSION_ID", "ENV")
    assert ss._sandbox_name_for({"_session_id": "EXPLICIT"}) == "nanocode-sbx-EXPLICIT"


def test_sandbox_name_for_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("NANOCODE_SESSION_ID", "ENV")
    assert ss._sandbox_name_for({}) == "nanocode-sbx-ENV"


# ---- _trace_host_dir_for ----
def test_trace_host_dir_for_uses_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("NANOCODE_TRACE_DIR", str(tmp_path))
    monkeypatch.setenv("NANOCODE_SESSION_ID", "ENV")
    d = ss._trace_host_dir_for({"_session_id": "EXPLICIT"}, "tag1")
    assert "EXPLICIT" in d
    assert "ENV" not in d
    assert d.endswith("sandbox/tag1")


def test_trace_host_dir_for_falls_back_to_env(monkeypatch, tmp_path):
    monkeypatch.setenv("NANOCODE_TRACE_DIR", str(tmp_path))
    monkeypatch.setenv("NANOCODE_SESSION_ID", "ENV")
    d = ss._trace_host_dir_for({}, "tag1")
    assert "ENV" in d


# ---- _merge_params 透传 _session_id ----
def test_merge_params_passes_through_session_id():
    p = ss._merge_params({"command": "x", "_session_id": "EXPLICIT"})
    assert p["_session_id"] == "EXPLICIT"


def test_merge_params_trace_tag_uses_explicit_session_id():
    p = ss._merge_params({"command": "x", "trace": True, "persist": True,
                          "_session_id": "EXPLICIT"})
    assert p["trace_tag"] == "nanocode-sbx-EXPLICIT"


def test_build_msb_command_persist_uses_explicit_session_id(monkeypatch):
    monkeypatch.setenv("NANOCODE_MSB_BIN", "msb")
    cmd = ss.build_msb_command({"command": "x", "persist": True, "deps": "none",
                                "_session_id": "EXPLICIT"})
    assert "nanocode-sbx-EXPLICIT" in cmd


# ---- env 回退兼容（旧无参函数保留）----
def test_legacy_sandbox_name_still_env(monkeypatch):
    monkeypatch.setenv("NANOCODE_SESSION_ID", "ENV")
    assert ss._sandbox_name() == "nanocode-sbx-ENV"
