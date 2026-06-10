"""trajectory 硬边界不变式测试（用户强制的架构边界，永不可违反）。

这些测试把「wire = execution fact / 事实源」与「trajectory = DERIVED 只读投影」的边界
编码为可执行不变式（CI 守门），覆盖三条：

(a) runtime 模块绝不 import trajectory 包——静态读取每个 runtime 源文件、解析 import，
    断言无任何对 ``nanocode.trajectory`` 的 import（含 ``from ..trajectory`` / ``from .trajectory``
    / ``import trajectory`` 形态）。注意 runtime 里合法出现的 ``trajectory_id`` /
    ``trajectory_level`` / ``trajectory_enabled`` 是 **wire 信封键 / Tracer 开关**，并非 import，
    故用 AST 精确判定 import 节点、并对源文本剔注释/字符串后再做子串兜底，避免误报。

(b) import nanocode.trajectory 绝不连带拉起 runtime——在干净子解释器里 import 该包，
    断言 ``nanocode.agent.engine`` 未被装载（投影层零运行时耦合）。

(c) Tracer 的 wire（FULL 与 SUMMARY 两级）绝不含派生标签 ``reward`` / ``eval_result``——
    通过 trajectory-enabled Tracer 把各类事件 emit 到内存 sink，断言所有 emit 出的事件
    dict 都不含这两个键（reward / eval 只该落 metrics.json / evals.jsonl，绝不污染 wire）。
"""
from __future__ import annotations

import ast
import subprocess
import sys
import tokenize
from io import StringIO
from pathlib import Path

# 仓库根 = tests/trajectory/../.. ；src 布局下 runtime 源在 src/nanocode/...。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "nanocode"

# 用户边界点名的 runtime 模块（绝不得 import trajectory）。
_RUNTIME_SOURCES = (
    _SRC / "agent" / "engine.py",
    _SRC / "agent" / "anthropic_backend.py",
    _SRC / "agent" / "openai_backend.py",
    _SRC / "agent" / "context_builder.py",
    _SRC / "agent" / "session.py",
    _SRC / "trace" / "tracer.py",
    _SRC / "trace" / "redaction.py",
)

# 任务点名的「import trajectory」文本形态（兜底子串扫描用）。
_IMPORT_SUBSTRINGS = (
    "nanocode.trajectory",
    "from ..trajectory",
    "from .trajectory",
    "import trajectory",
)


def _imports_trajectory_via_ast(source: str) -> bool:
    """AST 精确判定：源码里是否存在对 trajectory 包的 import 节点。

    覆盖 ``import nanocode.trajectory[...]`` / ``import trajectory`` /
    ``from nanocode.trajectory import ...`` / ``from ..trajectory import ...``
    （相对 import：module 末段名为 'trajectory'，或 from 中的别名引入 trajectory 子模块）。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name or ""
                # import trajectory / import nanocode.trajectory / import x.trajectory.y
                parts = name.split(".")
                if name == "trajectory" or "trajectory" in parts:
                    return True
                if name.startswith("nanocode.trajectory"):
                    return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            # from nanocode.trajectory import X / from x.trajectory import X
            if mod == "trajectory" or "trajectory" in mod.split("."):
                return True
            if mod.startswith("nanocode.trajectory"):
                return True
            # from .. import trajectory / from . import trajectory（相对包内引入子模块）
            if mod == "" or node.level:
                for alias in node.names:
                    if (alias.name or "") == "trajectory":
                        return True
    return False


def _strip_comments_and_strings(source: str) -> str:
    """用 tokenize 剔除注释与字符串字面量，剩余 token 还原为文本。

    目的：兜底子串扫描时，不把 docstring / 注释里出现的 'nanocode.trajectory' 误判为 import
    （runtime 里 redaction.py / tracer.py 的注释确实提到 trajectory 概念）。
    """
    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(StringIO(source).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except Exception:
        # tokenize 失败时退回原文（保守：宁可让子串扫描更严格）。
        return source
    return " ".join(out)


# ── (a) runtime 绝不 import trajectory ─────────────────────────────


def test_runtime_does_not_import_trajectory():
    offenders: list[str] = []
    for path in _RUNTIME_SOURCES:
        assert path.exists(), f"runtime source missing: {path}"
        source = path.read_text(encoding="utf-8")

        # 主断言：AST 精确判定无 trajectory import 节点。
        if _imports_trajectory_via_ast(source):
            offenders.append(f"{path} (ast import node)")
            continue

        # 兜底：剔注释/字符串后做任务点名的子串扫描，捕捉任何漏网的 import 写法。
        code_only = _strip_comments_and_strings(source)
        for needle in _IMPORT_SUBSTRINGS:
            if needle in code_only:
                offenders.append(f"{path} (substring {needle!r})")
                break

    assert not offenders, (
        "runtime modules must NEVER import nanocode.trajectory (derived projection "
        "must not couple into runtime); offenders: " + ", ".join(offenders)
    )


# ── (b) import trajectory 绝不连带拉起 runtime ─────────────────────


def test_importing_trajectory_does_not_pull_runtime():
    """干净子解释器里 import nanocode.trajectory，断言未连带装载 runtime engine。"""
    code = (
        "import sys; import nanocode.trajectory; "
        "assert 'nanocode.agent.engine' not in sys.modules, "
        "'importing trajectory pulled in nanocode.agent.engine'; "
        "print('OK')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert proc.returncode == 0, (
        "import nanocode.trajectory must not pull runtime engine.\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "OK" in proc.stdout


# ── (c) Tracer wire 绝不含 reward / eval_result ────────────────────


class _MemSink:
    """内存 sink：收集 emit 出的事件 dict 供断言。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def write(self, event: dict) -> None:
        self.events.append(event)

    def close(self) -> None:  # noqa: D401 - sink 协议
        pass


def _emit_variety(level: str) -> list[dict]:
    """通过一个 trajectory-enabled Tracer（指定 level）emit 各类事件，返回收集到的事件。

    刻意把 ``reward`` / ``eval_result`` 作为 payload kwarg 传进去——验证即便上游误传，
    wire emit 路径也不应把它们留在事实源里。注：当前 Tracer.emit 不主动注入 reward/eval，
    本测试既验证「emit 不注入」，也作为防御回归（若未来误加注入即红）。
    """
    from nanocode.trace.tracer import Tracer

    sink = _MemSink()
    tr = Tracer(
        "sess-boundary",
        [sink],
        agent_id="main",
        trajectory_enabled=True,
        trajectory_level=level,
    )
    tr.begin_turn("turn-1")
    tr.emit("llm_request", model="m", message_count=1,
            messages=[{"role": "user", "content": "hi" * 2000}])
    # 刻意把派生标签作为 payload kwarg 误传进 emit —— 验证 wire 守卫无条件剥除它们
    # （Tracer.emit pop reward/eval_result），即「即便上游误传也绝不进事实源」。
    tr.emit("assistant_message", text="ok", tool_uses=[{"id": "t1", "name": "read_file"}],
            reward=0.5, eval_result={"signal": "should_be_stripped"})
    tr.emit("llm_response", input_tokens=10, output_tokens=5)
    tr.emit("tool_call", tool="read_file", tool_use_id="t1", input={"file_path": "/x"})
    tr.emit("tool_result", tool="read_file", tool_use_id="t1", result="big result " * 500,
            reward=-1.0, eval_result={"signal": "tool_error"})
    tr.emit("permission_decision", tool="run_shell", action="deny")
    tr.emit("compaction", kind="auto", message_count_before=10, message_count_after=3)
    tr.emit("turn_end", final_status="completed", reward=1.0)
    tr.emit("session_end", final_status="completed", eval_result={"final": "ok"})
    tr.close()
    return sink.events


def test_tracer_wire_never_contains_reward_or_eval():
    for level in ("full", "summary"):
        events = _emit_variety(level)
        assert events, f"expected emitted events at level={level}"
        for ev in events:
            assert "reward" not in ev, (
                f"wire event must NEVER carry 'reward' (derived label) "
                f"at level={level}: {ev.get('type')}"
            )
            assert "eval_result" not in ev, (
                f"wire event must NEVER carry 'eval_result' (derived label) "
                f"at level={level}: {ev.get('type')}"
            )
        # 同时确认 trajectory 信封键确实在（说明确为 trajectory-enabled wire，而非空跑）。
        assert all(ev.get("trajectory") is True for ev in events)
