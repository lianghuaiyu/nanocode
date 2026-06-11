"""docs/15 Phase 6 §11.1：typed ResultEnvelope（host-derived files + 有界 render）。"""

from nanocode.agents.result import ResultEnvelope


class _FakeSub:
    """最小子 agent 替身：暴露 _files_read/_files_modified/_tree_session_id（宿主派生事实）。"""
    def __init__(self):
        self._files_read = {"/a.py", "/b.py"}
        self._files_modified = {"/c.py"}
        self._tree_session_id = "parent.agent-1"


def test_build_uses_host_derived_files_not_model_claims():
    env = ResultEnvelope.build(_FakeSub(), "did the thing", {"input": 10, "output": 5}, "/tmp/result.md")
    assert env.files_read == ["/a.py", "/b.py"]          # 宿主派生,排序
    assert env.files_modified == ["/c.py"]
    assert env.child_session_id == "parent.agent-1"
    assert env.tokens == {"input": 10, "output": 5}
    assert env.status == "completed"


def test_render_small_text_passthrough():
    env = ResultEnvelope(summary="ignored when raw small", result_path="/tmp/r.md",
                         files_modified=["/c.py"], tokens={"input": 1, "output": 2})
    out = env.render("short deliverable")
    assert "short deliverable" in out                    # <4KB 直通
    assert "Files modified:" in out and "/c.py" in out
    assert "Result: /tmp/r.md" in out


def test_render_large_text_truncates_with_pointer():
    env = ResultEnvelope(summary="THE SUMMARY", result_path="/tmp/big.md")
    out = env.render("x" * 5000)                          # >4KB
    assert "THE SUMMARY" in out
    assert "truncated" in out and "/tmp/big.md" in out


def test_findings_and_files_bounded():
    env = ResultEnvelope(summary="s", findings=[f"f{i}" for i in range(20)],
                         files_modified=[f"/f{i}" for i in range(20)], result_path="/r")
    out = env.render("body")
    assert "(+10 more)" in out                            # findings cap 10
    assert out.count("/f") <= 11                          # files cap 10 + overflow note


def test_from_result_dict_and_status():
    d = {"summary": "s", "findings": ["x"], "files_read": ["/r"], "files_modified": [],
         "tokens": {"input": 3, "output": 1}, "result_path": "/p", "childSessionId": "c1"}
    env = ResultEnvelope.from_result_dict(d, status="timed_out", error="slow")
    assert env.status == "timed_out" and env.error == "slow"
    assert env.child_session_id == "c1"
    assert env.to_result_dict()["childSessionId"] == "c1"   # round-trip
