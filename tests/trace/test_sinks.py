import json
from nanocode.trace.sinks import JsonlSink


def test_jsonl_sink_writes_valid_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    s = JsonlSink(path)
    s.write({"type": "a", "n": 1})
    s.write({"type": "b", "txt": "中文"})
    s.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "a"
    assert json.loads(lines[1])["txt"] == "中文"


def test_jsonl_sink_lazy_and_appends(tmp_path):
    path = tmp_path / "sub" / "t.jsonl"   # 父目录不存在，应自动建
    s = JsonlSink(path)
    s.write({"i": 1})
    s2 = JsonlSink(path)                    # 第二个 sink 追加，不覆盖
    s2.write({"i": 2})
    s.close(); s2.close()
    assert len(path.read_text().splitlines()) == 2


def test_jsonl_sink_write_failure_is_swallowed(tmp_path):
    s = JsonlSink(tmp_path / "x.jsonl")
    # 非 JSON 可序列化对象 + default=str 兜底；即便失败也不抛
    s.write({"obj": object()})
    s.close()  # 不抛异常即通过
