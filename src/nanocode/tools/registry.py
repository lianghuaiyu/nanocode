"""tools/registry.py — ToolRegistry：工具真相源的唯一门面（docs/24 Phase 1）。

把原先散在三处的工具真相源（spec._ALL/TOOLS + registry.tool_definitions + execute._HANDLERS）
合一为一个 `ToolRegistry`：name→Tool 映射 + schema 聚合 + deferred 工具激活状态。模块级单例
`REGISTRY = ToolRegistry.from_builtins(spec._ALL)` 是所有调用点的入口。

零行为变更（docs/24 §6 Commit 1）：`schemas()` 与旧 `tool_definitions`/`get_active_tool_definitions`
逐项等值（顺序 + 内容，含/去 deferred 键的语义一致）；`get(name)` 等价旧 `TOOLS.get(name)`；
deferred 激活语义不变。dup fail-loud 沿用旧 TOOLS 构造期检查。
"""

from __future__ import annotations

from .spec import _ALL, Tool  # noqa: F401 — Tool re-export 供下游类型引用
from .types import ToolSource, Trust

ToolDef = dict  # Anthropic tool schema dict

# 外部来源 → 强制 namespace 前缀(docs/24 §4.5)。内置(BUILTIN)无前缀、占保留名。
_NAMESPACE_PREFIX: dict[ToolSource, str] = {
    ToolSource.MCP: "mcp__",
    ToolSource.EXT: "ext__",
    ToolSource.EMBEDDER: "embedder__",
}


class ToolRegistry:
    """name → Tool 的单一注册表 + deferred 激活状态。

    schemas(active=None) 等价旧 tool_definitions / get_active_tool_definitions：
      - active is None：返回全表 schema（原序，**保留 deferred 键**）。
      - active 非 None：按旧 get_active_tool_definitions 语义——剔除未激活的 deferred 工具，
        并 strip 掉 'deferred' 键（不发给 API）。
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._activated: set[str] = set()

    @classmethod
    def from_builtins(cls, tools: list[Tool]) -> "ToolRegistry":
        reg = cls()
        for t in tools:
            reg.register(t)
        return reg

    def register(self, tool: Tool) -> None:
        """注册一个工具(docs/24 §4.3 / §4.5)。

        内置路径(source=BUILTIN、无前缀、_ALL 一次性注册)原样通过。外部注册(Phase 4)
        受三条 fail-loud 规则约束：

        - forced-namespace：外部来源(MCP/EXT/EMBEDDER)的 name 必须带对应前缀
          (mcp__ / ext__ / embedder__)，否则拒。
        - reserved-builtin：既有 tool.source is BUILTIN 而 incoming 非 BUILTIN → 拒
          (外部不得 shadow 内置)。
        - override-only-TRUSTED：name 已存在(且非上述内置被外部撞)时，仅 incoming.trust
          is TRUSTED 才允许覆盖(替换)；否则保持 dup fail-loud。
        """
        # forced-namespace：外部来源强制带 namespace 前缀(本阶段尚无外部注册，此刻 inert)。
        prefix = _NAMESPACE_PREFIX.get(tool.source)
        if prefix is not None and not tool.name.startswith(prefix):
            raise RuntimeError(
                f"external tool {tool.name!r} (source={tool.source.value}) "
                f"must use namespace prefix {prefix!r}"
            )

        existing = self._tools.get(tool.name)
        if existing is not None:
            # reserved-builtin：外部不得 shadow 内置。
            if existing.source is ToolSource.BUILTIN and tool.source is not ToolSource.BUILTIN:
                raise RuntimeError(
                    f"reserved builtin tool name cannot be shadowed by "
                    f"{tool.source.value} tool: {tool.name}"
                )
            # override-only-TRUSTED + same-source（docs/24 Phase 4b nit 收紧）：重名仅当 incoming
            # 是 TRUSTED **且**与既有同源才允许覆盖——防跨源 last-writer-wins（如某 EXT 覆盖另一
            # MCP 同名工具），否则沿用旧 dup fail-loud。
            if not (tool.trust is Trust.TRUSTED and tool.source is existing.source):
                raise RuntimeError(f"duplicate tool name in registry: {tool.name}")
        self._tools[tool.name] = tool

    def overlay(self, extra: list[Tool]) -> "ToolRegistry":
        """返回「本表内置 + extra」的**新** registry（docs/24 §4.3 / Phase 4a）。

        外部工具（MCP 每会话 / 扩展每 runtime / 嵌入者每 config）绝不能写进全局 REGISTRY
        （跨会话泄漏 + 重复注册炸）——每 agent 持自己 overlay 的 registry。extra 逐个走
        `register()`（Phase 5 forced-namespace / reserved-builtin / override-only-TRUSTED
        规则全程生效）。activation/deferred 状态**per-registry**（新 `_activated` 集，
        不共享全局可变态）。extra 为空 → 与本表（builtins）逐一等价（零行为变更）。"""
        reg = ToolRegistry()
        for t in self._tools.values():
            reg._tools[t.name] = t        # 内置基底原样拷入（已通过本表 register 校验，不重判）
        for t in extra:
            reg.register(t)
        return reg

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def set_schema(self, name: str, schema: dict) -> None:
        """替换**本 registry** 里某既有工具的模型可见 schema（docs/26 G7）。

        仅供 per-agent overlay 用（绝不在全局 REGISTRY 上调）——`overlay()` 已把每个 Tool 拷进
        本表自己的 `_tools` dict，`dataclasses.replace` 造**新** frozen Tool 并重指本表 slot，
        全局 REGISTRY 的 dict 仍指向旧 Tool，不受影响。schema 经 `_closed` 收口（与 builtins 一致：
        additionalProperties=false），其余字段（run/concurrency_safe/needs/source/trust）保持。
        未注册的 name → fail-loud（编排 overlay 只在主 agent registry 上调，`agent` 必存在）。

        用例：编排扩展激活时把常驻 builtin 的 slim `agent` schema 换成含 steps/tasks/accept/
        plan_fanout 的 ORCHESTRATION_SCHEMA——「编排实现可卸 ⟹ 模型可见编排词汇随之可卸」。"""
        import dataclasses

        from .spec import _closed
        tool = self._tools.get(name)
        if tool is None:
            raise RuntimeError(f"cannot set schema for unregistered tool: {name}")
        self._tools[name] = dataclasses.replace(tool, schema=_closed(schema))

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self, active: set[str] | None = None) -> list[ToolDef]:
        """工具 schema 列表（原序）。

        active is None → 全表 schema（保留 deferred 键）。
        active 非 None → 剔未激活 deferred + strip 'deferred' 键。注：传入显式 active 集时仍以
        「该 schema 自身是否 deferred 且名在 active 中」判定。
        """
        if active is None:
            return [t.schema for t in self._tools.values()]
        return [
            {k: v for k, v in t.schema.items() if k != "deferred"}
            for t in self._tools.values()
            if not t.deferred or t.name in active
        ]

    # ─── deferred 工具激活状态（旧 registry._activated_tools 的内聚版）─────────

    def reset_activated(self) -> None:
        self._activated.clear()

    def activate(self, name: str) -> None:
        self._activated.add(name)

    def active_schemas(self) -> list[ToolDef]:
        """以当前激活集过滤全表。"""
        return self.schemas(active=self._activated)

    def deferred_names(self) -> list[str]:
        """尚未激活的 deferred 工具名（原序）。"""
        return [t.name for t in self._tools.values()
                if t.deferred and t.name not in self._activated]


REGISTRY = ToolRegistry.from_builtins(_ALL)


# ─── 激活状态 helper ─────────────────────────────────────────────

def reset_activated_tools() -> None:
    REGISTRY.reset_activated()


def get_active_tool_definitions(all_tools: list[ToolDef] | None = None,
                                *, registry: "ToolRegistry") -> list[ToolDef]:
    """剔除未激活 deferred + strip 'deferred' 键。

    docs/24 Phase 4a：schema 查表与 deferred 激活状态必须来自同一个 registry（通常是
    per-agent overlay）；不在 helper 内回退全局表，避免混读两个工具真相源。
    """
    reg = registry
    if all_tools is None:
        return reg.active_schemas()
    activated = reg._activated
    return [
        {k: v for k, v in t.items() if k != "deferred"}
        for t in all_tools
        if not t.get("deferred") or t["name"] in activated
    ]


def get_deferred_tool_names(all_tools: list[ToolDef] | None = None,
                            *, registry: "ToolRegistry") -> list[str]:
    """未激活 deferred 工具名。all_tools 非 None 时按传入 schema 列表算。

    docs/24 Phase 4a：`registry` 显式指定，确保读取 per-agent overlay 激活集。
    """
    reg = registry
    if all_tools is None:
        return reg.deferred_names()
    activated = reg._activated
    return [t["name"] for t in all_tools if t.get("deferred") and t["name"] not in activated]
