from nanocode.skills.hooks import normalize_hooks, hook_matches, build_hook_event


def test_normalize_basic():
    raw = {"pre-tool-use": [{"matcher": "edit_file", "command": "lint", "timeout": 5000}],
           "post-tool-use": [{"matcher": ["write_file", "edit_file"], "command": "pytest"}]}
    n = normalize_hooks(raw)
    assert n["pre-tool-use"][0] == {"matcher": ["edit_file"], "command": "lint", "timeout_ms": 5000}
    assert n["post-tool-use"][0]["matcher"] == ["write_file", "edit_file"]
    assert n["post-tool-use"][0]["timeout_ms"] == 30000   # 默认


def test_normalize_skips_invalid():
    assert normalize_hooks({"pre-tool-use": [{"matcher": "x"}]}) is None   # 缺 command
    assert normalize_hooks({"unknown-event": [{"command": "x"}]}) is None  # 未知事件
    assert normalize_hooks("nope") is None
    assert normalize_hooks(None) is None


def test_normalize_matcher_default_star():
    n = normalize_hooks({"pre-tool-use": [{"command": "c"}]})
    assert n["pre-tool-use"][0]["matcher"] == ["*"]


def test_hook_matches():
    assert hook_matches(["*"], "anything") is True
    assert hook_matches(["edit_file"], "edit_file") is True
    assert hook_matches(["write_file"], "edit_file") is False


def test_build_event():
    e = build_hook_event("post-tool-use", "sk", "edit_file", {"file_path": "a"}, "RES", "/cwd", "sid")
    assert e["event"] == "post-tool-use" and e["skill"] == "sk" and e["tool"] == "edit_file"
    assert e["input"] == {"file_path": "a"} and e["result"] == "RES"
    assert e["cwd"] == "/cwd" and e["session_id"] == "sid"
