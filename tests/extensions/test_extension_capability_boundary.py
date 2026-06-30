"""docs/26 G6 收口：扩展 ④ 拿不到 raw RuntimeThread —— 无 `.thread` 逃逸口。

参照 pi/codex/opencode 的共同结论（"don't hand over the symbol"）：扩展上下文
**不存在**一个值是 RuntimeThread 的字段；运行时控制面（exec/spawn/set_sandbox）只
能经 narrow、逐个绑定的能力视图（spawn / memory_evolution / …）触达。本测试两路把关：

1. 运行时：`ExtensionContext` / `ExtensionCommandContext` 无 `.thread` 属性，且**没有任何
   公开属性的值是 raw thread**（对象图被私有字段藏住）；新增的 curated 能力如实委托。
2. 静态（AST）：`extensions/**` 源码里不存在 `ctx.thread` / `context.thread` 取值
   （AST 自然忽略注释与 docstring，故文档里的说明性提及不误报）。
"""
import ast
import asyncio
import pathlib

import pytest

from nanocode.extensions import ExtensionHost
from nanocode.extensions.context import MemoryEvolutionCap, SpawnCap
from nanocode.extensions.errors import ExtensionRuntimeError

_EXT_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "nanocode" / "extensions"


class _SentinelThread:
    """Stand-in RuntimeThread; identity-checked to prove it never leaks publicly."""

    def __init__(self):
        self._agent = None
        self.model = "claude-opus"
        self.aborted = False
        self.calls = []

    def readonly_session(self):
        return None

    def is_turn_aborted(self) -> bool:
        return self.aborted

    async def run_extension_task(self, kind, payload):
        self.calls.append(("run_extension_task", kind, payload))
        return "task-started"

    async def spawn_memory_eval(self):
        self.calls.append(("spawn_memory_eval",))
        return "eval-done"


def _bound_system_host():
    host = ExtensionHost.load_system_extensions().activate_all()
    thread = _SentinelThread()
    host.bind_runtime(thread, None)
    return host, thread


# ── 1. runtime: no `.thread`, no public attribute equals the raw thread ──────────

_MEMORY_EVOLUTION = "nanocode.memory_evolution"
_ORCHESTRATION = "nanocode.orchestration"


def test_context_has_no_thread_escape_hatch():
    host, _thread = _bound_system_host()
    ctx = host.create_context(_MEMORY_EVOLUTION)
    cctx = host.create_command_context(_MEMORY_EVOLUTION)
    assert not hasattr(ctx, "thread")
    assert not hasattr(cctx, "thread")


def test_no_public_attribute_leaks_the_raw_thread():
    host, thread = _bound_system_host()
    objs = [host.create_context(ext) for ext in (_MEMORY_EVOLUTION, _ORCHESTRATION)]
    objs.append(host.create_command_context(_MEMORY_EVOLUTION))
    for obj in objs:
        for name in dir(obj):
            if name.startswith("_"):
                continue
            assert getattr(obj, name) is not thread, (
                f"{type(obj).__name__}.{name} leaks the raw RuntimeThread "
                f"(docs/26 G6: the context must not expose the runtime object)")


# ── curated caps faithfully delegate (no raw thread needed) ──────────────────────

def test_spawn_cap_is_aborted_delegates():
    host, thread = _bound_system_host()
    ctx = host.create_context(_ORCHESTRATION)  # is_aborted is orchestrate-gated
    assert isinstance(ctx.spawn, SpawnCap)
    assert ctx.spawn.is_aborted() is False
    thread.aborted = True
    assert ctx.spawn.is_aborted() is True


def test_memory_evolution_cap_delegates():
    host, thread = _bound_system_host()
    ctx = host.create_context(_MEMORY_EVOLUTION)
    assert isinstance(ctx.memory_evolution, MemoryEvolutionCap)
    assert asyncio.run(ctx.memory_evolution.run_optimization(diagnose=True)) == "task-started"
    assert ("run_extension_task", "memory_optimize", {"diagnose": True}) in thread.calls
    assert asyncio.run(ctx.memory_evolution.eval_generate()) == "eval-done"
    assert ("spawn_memory_eval",) in thread.calls


def test_memory_evolution_cap_absent_without_capability():
    host = ExtensionHost([]).activate_all()
    host.bind_runtime(_SentinelThread(), None)
    ctx = host.create_context("nonexistent.extension")
    assert ctx.memory_evolution is None


def test_stale_caps_fail_loud():
    host, _thread = _bound_system_host()
    orc = host.create_context(_ORCHESTRATION)
    mem = host.create_context(_MEMORY_EVOLUTION)
    host.invalidate("dispose")
    with pytest.raises(ExtensionRuntimeError):
        orc.spawn.is_aborted()
    with pytest.raises(ExtensionRuntimeError):
        asyncio.run(mem.memory_evolution.run_optimization())


# ── 2. static AST: no `ctx.thread` / `context.thread` access in extensions/ ──────

def test_no_ctx_thread_access_anywhere_in_extensions():
    violations = []
    for path in sorted(_EXT_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == "thread"
                    and isinstance(node.value, ast.Name)
                    and node.value.id in {"ctx", "context", "c"}):
                violations.append(f"{path.relative_to(_EXT_SRC)}:{node.lineno}")
    assert not violations, (
        "extension code reaches the raw RuntimeThread via ctx.thread "
        "(docs/26 G6 — route through a curated capability view instead):\n"
        + "\n".join(violations))
