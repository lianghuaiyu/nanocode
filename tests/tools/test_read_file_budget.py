"""docs/15 Phase 4 §9.3：read_file 工具边界控量（line/byte cap + offset/limit + 截断标记）。"""

from nanocode.tools import read_file
from nanocode.tools.context import default_tool_context

_CTX = default_tool_context()


def test_small_file_unchanged(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("l1\nl2\nl3")
    out = read_file.run(_CTX, {"file_path": str(p)})
    assert out == "   1 | l1\n   2 | l2\n   3 | l3"     # 小文件:逐行,无截断标记


def test_line_cap_truncates_with_marker(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("\n".join(f"line{i}" for i in range(1, 5001)))   # 5000 行
    out = read_file.run(_CTX, {"file_path": str(p)})
    assert "   1 | line1" in out
    assert "line2000" in out
    assert "line2001" not in out                                  # 默认 2000 行封顶
    assert "truncated: showing lines 1-2000 of 5000" in out


def test_offset_limit_window(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("\n".join(f"line{i}" for i in range(1, 101)))    # 100 行
    out = read_file.run(_CTX, {"file_path": str(p), "offset": 50, "limit": 10})
    assert "  50 | line50" in out
    assert "  59 | line59" in out
    assert "line49" not in out and "line60" not in out
    assert "truncated: showing lines 50-59 of 100" in out


def test_offset_to_end_shows_range_note(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("\n".join(f"l{i}" for i in range(1, 11)))         # 10 行
    out = read_file.run(_CTX, {"file_path": str(p), "offset": 8})
    assert "   8 | l8" in out and "  10 | l10" in out
    assert "showing lines 8-10 of 10" in out


def test_byte_cap_marks_truncation(tmp_path):
    p = tmp_path / "huge.txt"
    p.write_text("x" * (read_file.MAX_BYTES + 5000))              # 单行超字节上限
    out = read_file.run(_CTX, {"file_path": str(p)})
    assert "exceeded" in out and "bytes" in out


def test_missing_file_errors(tmp_path):
    out = read_file.run(_CTX, {"file_path": str(tmp_path / "nope.txt")})
    assert out.startswith("Error reading file:")


def test_invalid_offset_limit_fall_back_to_defaults(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("a\nb\nc")
    out = read_file.run(_CTX, {"file_path": str(p), "offset": -3, "limit": "bad"})
    assert "   1 | a" in out and "   3 | c" in out                # 非法值回退默认
