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

# 用户边界点名的 runtime 模块（绝不得 import trajectory）。docs/14 Milestone B：trace/ 已删除，
# runtime 事实源现为 engine/backends/session（写 canonical 树）+ runtime_events（内存事件流）。
_RUNTIME_SOURCES = (
    _SRC / "agent" / "engine.py",
    _SRC / "agent" / "anthropic_backend.py",
    _SRC / "agent" / "openai_backend.py",
    _SRC / "agent" / "session.py",
    _SRC / "agent" / "runtime_events.py",
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


# ── (c) canonical 树绝不含 reward / eval_result（三层边界：tree=facts / labels-never-in-tree）──


def test_tree_entries_never_contain_reward_or_eval():
    """docs/14 Milestone B：Tracer/wire 已删——派生标签的事实源边界现守在 **canonical 树写入路径**。

    刻意把 ``reward`` / ``eval_result`` 当 kwarg 误传进 ``_tree_event``，并写一条普通消息，断言落树的
    entry/message 绝不含这两个键（``engine._tree_event`` 防御性 pop，取代原 ``Tracer.emit`` 的同名剥除）。
    reward / eval_result 只存在于 Step（steps.jsonl / evals.jsonl），由 eval 回填——绝不进 session.jsonl。
    """
    from nanocode.agent.engine import Agent
    from nanocode.session import tree as T
    from nanocode.session.manager import SessionManager

    a = Agent(api_key="test", session_id="boundtree", permission_mode="bypassPermissions")
    a._session_mgr = SessionManager.create("boundtree")     # 持写锁（create 默认 lock=True）
    # 注解型遥测 entry：误传派生标签
    a._tree_event(T.PERMISSION_DECISION, tool="run_shell", action="deny",
                  reward=0.5, eval_result={"signal": "should_be_stripped"})
    a._tree_event(T.TURN_END, inputTokens=1, outputTokens=2, turns=1,
                  reward=-1.0, eval_result={"final": "ok"})
    # 普通消息 entry
    a._tree_record({"role": "user", "content": "hi"})
    a._tree_record({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})

    entries = a._session_mgr.entries()
    assert entries, "expected tree entries"
    for e in entries:
        blob = str(e.to_dict())
        assert "reward" not in blob, f"tree entry must NEVER carry 'reward' (derived label): {e.type}"
        assert "eval_result" not in blob, f"tree entry must NEVER carry 'eval_result' (derived label): {e.type}"
