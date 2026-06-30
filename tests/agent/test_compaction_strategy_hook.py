"""docs/26 G4：内核 compact() 的 before_compact 钩子路径 + agent/session→extensions 边界。

钩子只替换"产摘要"这一步；format / record_event(CompactionRequested) / cut / fold / restore
仍内核独占。注入的 `agent._compaction_strategy` 是 callable(request)->CompactionOutcome
（白盒直接注入，等价于 runtime/facade 在 host 注册策略时装上的 lambda）。
"""
import ast
import asyncio
from pathlib import Path

from nanocode.agent.compaction import CompactionOutcome
from nanocode.agent.engine import Agent
from nanocode.session import tree
from nanocode.session.manager import SessionManager


def _agent(sid):
    a = Agent(api_key="test", session_id=sid, permission_mode="bypassPermissions")
    a._mcp_initialized = True
    a.model = "claude-x"
    return a


def _seed(mgr):
    mgr.append_message(tree.user_message("old question " * 50))
    mgr.append_message(tree.assistant_message(
        [tree.text_block("a1 " * 20)], provider="anthropic", api="anthropic",
        model="claude-x", stop_reason="stop"))
    mgr.append_message(tree.user_message("recent"))


def _prep(sid, monkeypatch):
    a = _agent(sid)
    mgr = SessionManager.create(sid)
    a._session_mgr = mgr
    _seed(mgr)
    # 小 keep 预算 → cut 在最近 user 之前，prefix 非空（summarizer/strategy 有内容可摘）。
    monkeypatch.setattr(a.agent_session, "keep_recent_tokens", lambda: 5)
    return a, mgr


def _compactions(mgr):
    return [e for e in mgr.entries() if e.type == tree.COMPACTION]


def test_strategy_summary_used_builtin_not_called(monkeypatch):
    a, mgr = _prep("cs_summary", monkeypatch)
    called = []

    async def _builtin(messages=None, instructions=None):
        called.append(True)
        return "BUILTIN"

    monkeypatch.setattr(a, "_compact_anthropic", _builtin)

    async def strat(request):
        return CompactionOutcome(summary="STRATEGY_SUMMARY")

    a._compaction_strategy = strat
    asyncio.run(a.agent_session.compact())

    comp = _compactions(mgr)
    assert len(comp) == 1
    assert comp[0].data["summary"] == "STRATEGY_SUMMARY"
    assert called == []                                  # 内置 summarizer 从未被调
    assert comp[0].data["details"]["retryCount"] == 0


def test_strategy_cancel_writes_no_entry(monkeypatch):
    a, mgr = _prep("cs_cancel", monkeypatch)

    async def _builtin(messages=None, instructions=None):
        raise AssertionError("built-in must not run on cancel")

    monkeypatch.setattr(a, "_compact_anthropic", _builtin)

    async def strat(request):
        return CompactionOutcome(cancel=True)

    a._compaction_strategy = strat
    asyncio.run(a.agent_session.compact())

    assert _compactions(mgr) == []                        # 取消 → 不写 COMPACTION entry
    assert a._compacting is False                         # finally 仍复位


def test_strategy_error_falls_back_to_builtin(monkeypatch):
    a, mgr = _prep("cs_error", monkeypatch)

    async def _builtin(messages=None, instructions=None):
        return "BUILTIN_SUMMARY"

    monkeypatch.setattr(a, "_compact_anthropic", _builtin)

    async def strat(request):
        raise RuntimeError("strategy blew up")

    a._compaction_strategy = strat
    asyncio.run(a.agent_session.compact())

    comp = _compactions(mgr)
    assert len(comp) == 1
    assert comp[0].data["summary"] == "BUILTIN_SUMMARY"   # 抛错 → 回退内置


def test_strategy_abstain_falls_back_to_builtin(monkeypatch):
    a, mgr = _prep("cs_abstain", monkeypatch)

    async def _builtin(messages=None, instructions=None):
        return "BUILTIN_SUMMARY"

    monkeypatch.setattr(a, "_compact_anthropic", _builtin)

    async def strat(request):
        return CompactionOutcome(summary=None, cancel=False)  # 弃权

    a._compaction_strategy = strat
    asyncio.run(a.agent_session.compact())

    assert _compactions(mgr)[0].data["summary"] == "BUILTIN_SUMMARY"


def test_request_carries_curated_scalars_not_raw_state(monkeypatch):
    a, mgr = _prep("cs_request", monkeypatch)
    a.last_input_token_count = 777
    a._files_read.add("/repo/foo.py")
    a._files_modified.add("/repo/bar.py")
    seen = {}

    async def strat(request):
        seen["req"] = request
        return CompactionOutcome(summary="S")

    a._compaction_strategy = strat
    asyncio.run(a.agent_session.compact("focus on X"))    # manual + 自定义指令

    req = seen["req"]
    assert req.trigger == "manual"
    assert req.instructions == "focus on X"
    assert req.tokens_before == 777                       # 标量快照，非 raw agent 状态
    assert req.file_ops == {"read": ["/repo/foo.py"], "modified": ["/repo/bar.py"]}
    assert isinstance(req.messages, list) and req.messages  # provider-shaped 中性投影
    assert req.previous_summary is None
    # curated：绝不递 raw mgr/agent/tree Entry。
    assert not hasattr(req, "manager") and not hasattr(req, "agent")


def test_default_no_strategy_uses_builtin(monkeypatch):
    # _compaction_strategy 默认 None → 行为与改造前逐字一致（内置 summarizer）。
    a, mgr = _prep("cs_none", monkeypatch)

    async def _builtin(messages=None, instructions=None):
        return "BUILTIN"

    monkeypatch.setattr(a, "_compact_anthropic", _builtin)
    assert a._compaction_strategy is None
    asyncio.run(a.agent_session.compact())
    assert _compactions(mgr)[0].data["summary"] == "BUILTIN"


# ─── 边界：agent/ 与 session/ 不 import extensions/（AST 静扫）────────────────────

def _imported_modules(py_file: Path, src_root: Path):
    """yield 该文件每条 import 的**绝对**目标模块名（相对 import 已解析）。"""
    pkg_parts = py_file.parent.relative_to(src_root).parts  # 含该模块的包（dir）点路径
    tree_ = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    for node in ast.walk(tree_):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                yield node.module or ""
            else:
                base = list(pkg_parts[: len(pkg_parts) - (node.level - 1)])
                yield ".".join(base + ([node.module] if node.module else []))


def test_agent_and_session_do_not_import_extensions():
    src_root = Path(__file__).resolve().parents[2] / "src"
    offenders = []
    for sub in ("nanocode/agent", "nanocode/session"):
        for py_file in (src_root / sub).rglob("*.py"):
            for mod in _imported_modules(py_file, src_root):
                if mod == "nanocode.extensions" or mod.startswith("nanocode.extensions."):
                    offenders.append(f"{py_file.relative_to(src_root)} → {mod}")
    assert offenders == [], (
        "agent/ and session/ must not import extensions/ (G4 boundary): " + "; ".join(offenders))
