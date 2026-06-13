"""command 层的类型契约（CMD-P0，见 docs/11-command-layer-refactor.md）。

设计要点（与早期草案的差异）：

1. CommandResult 是**判别联合**，不是七字段平铺。
   早期草案的 `handled + output + message_to_agent + should_query + exit_repl +
   replace_agent + next_input` 能表达矛盾态（message_to_agent 置位而 should_query=False
   之类）。这里学 Claude Code 的 prompt|local|local-jsx 判别式：「是否产生模型可见消息」
   与「是否要 query」是**同一个事实**，由 variant 决定，矛盾态不可表示。
     · Local   —— 纯本地副作用，从不 query（第一批几乎所有命令）
     · Prompt  —— 产生一个 turn；**必须**经 AgentSession.run_turn / RuntimeThread.run
                  驱动，复用既有 TurnResult 与 cancel/_aborted/approval 不可回归契约
                  （agent/runtime.py），绝不自行实现 turn/取消/审批
     · Control —— CMD-P3+ 的 lifecycle 信号（replace_thread/switch_runtime/resume/fork），
                  P0/P1 不产生

2. 没有 `handled` 字段。
   nanocode 必须保留「未知 /foo 当普通文本发给模型」（cli.py:694→716）。这个「落空」
   语义放在 **dispatch 边界**：Registry.lookup() 返回 None 即「不是命令」，runner 把原始
   行交给 session.run_turn。结果结构里不需要 handled。

3. CommandSpec 加 `kind` 判别式（最 load-bearing），并砍掉草案里没有机制支撑的元数据
   （available_during_turn —— REPL 严格串行，无并发输入通道；requires_agent —— 恒真；
   mutates_session —— 语义模糊；supports_inline_args —— 把 per-variant 能力拍平）。
   需要时再加，不预投机。

4. per-command 的 model/effort/allowedTools override、输出通道（user/model/hidden）是
   Claude Code 有而本设计 P0 暂缺的槽位 —— 显式不做，留待对应 variant 扩展。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:  # 仅类型；避免 import 期耦合（agent 包较重）
    from ...agent import RuntimeThread
    from .registry import Registry


# ─── 结果：判别联合 ──────────────────────────────────────────────

@dataclass(frozen=True)
class Local:
    """纯本地副作用，从不 query。

    覆盖第一批几乎所有命令：/clear /plan /cost /compact /memory* /skills /sandbox
    /tasks /task /task-stop /agents /agent /trace /help。

    output 为可选的、已渲染好的文本（runner 经 ctx.out 呈现；None 表示 handler 自行打印
    或无输出）。exit_repl=True 仅用于退出循环（裸词 exit/quit 由 runner 在 slash 分发前
    特判，但若做成命令也走这里）。
    """
    output: str | None = None
    exit_repl: bool = False


@dataclass(frozen=True)
class Prompt:
    """产生一个 turn —— 把 prompt 交给模型。

    runner 经 AgentSession.run_turn(prompt) 驱动（CMD-P2.5 起经 RuntimeThread.run → TurnResult）。
    skill 调用与「未知 /foo 落空→chat」在 runner 层都落到这条路径，但**落空不是命令**，
    见模块 docstring 第 2 点：落空由 Registry.lookup()==None 表达，不构造 Prompt。
    """
    prompt: str


@dataclass(frozen=True)
class Control:
    """CMD-P3+ lifecycle 控制流：没有 TurnResult 对应物的会话级操作。

    action 决定 runner 如何重建/切换底层句柄；payload 携带参数（如 from_event_id）。
    P0/P1 的 handler 不返回此 variant —— 列在此处仅为让 should_query/分发逻辑的 variant
    轴从一开始就完整。落地时必须经既有 AgentSession.fork_to / restore_session / AgentRuntime，
    不得另起平行实现。
    """
    action: Literal["replace_thread", "switch_runtime", "resume", "fork"]
    payload: dict = field(default_factory=dict)


# 一个命令 handler 的返回值
CommandResult = Local | Prompt | Control


def produces_turn(result: CommandResult) -> bool:
    """是否需要把结果交给模型跑一个 turn。从 variant 推导，**绝不**另设独立 should_query 字段。"""
    return isinstance(result, Prompt)


def should_exit(result: CommandResult) -> bool:
    """该命令是否要求退出 REPL。"""
    return isinstance(result, Local) and result.exit_repl


# ─── 命令元数据 ──────────────────────────────────────────────────

@dataclass(frozen=True)
class CommandSpec:
    """一个 slash 命令的静态元数据（喂 dispatch + 补全 display_meta + --help，单一来源）。"""
    name: str                                              # 含前导 '/'，如 "/clear"
    kind: Literal["local", "prompt", "control"]            # 最 load-bearing 的判别式
    description: str                                       # 一行；补全菜单与 --help 共用
    aliases: tuple[str, ...] = ()
    arg_hint: str = ""                                     # 如 "<id>" / "[status]"
    match: Literal["exact", "prefix", "exact_or_prefix"] = "exact_or_prefix"
    #   exact          仅 line == name（如 /clear、/memory）
    #   exact_or_prefix line == name 或 line.startswith(name+" ")（如 /sandbox、/tasks）
    #   prefix         仅 line.startswith(name+" ")，无裸形（如 /task、/agent）
    #   most-specific-first 由 Registry 按 name 长度降序保证（/memory eval generate 先于 /memory eval 先于 /memory）
    is_hidden: bool = False                                # 不进补全菜单（仍可用），如别名
    is_enabled: bool = True                                # P0 静态布尔；需要时升级为 Callable[[], bool]
    source: Literal["builtin", "user", "project"] = "builtin"  # 仅当配套 loader 时才有意义


# handler：吃 (上下文, 命令后的剩余 raw 文本)，产出 CommandResult
Handler = Callable[["CommandContext", str], Awaitable[CommandResult]]


@dataclass(frozen=True)
class Command:
    """registry 项：元数据 + handler。"""
    spec: CommandSpec
    run: Handler


# ─── 执行上下文 ──────────────────────────────────────────────────

@dataclass
class CommandContext:
    """handler 执行上下文。

    docs/17 B-list：handler 经 `thread`（RuntimeThread，面向 client 的稳定句柄）操作会话——
    clear/compact/plan/cost/tasks/sessions 全走 thread 的稳定方法面，**不再 reach 进 Agent 私有面**
    （_session_mgr / _spawn_* / _background_tasks / task_manager / agent_session）。导航类命令
    （new/resume/fork/clone）返回 Control，由 host 经 AgentRuntime 完成。

    `thread` 在每次 dispatch 由 host 重新绑定为 current_thread——lifecycle 替换（/new /resume…）后
    handler 无需缓存任何句柄。
    """
    thread: "RuntimeThread"
    cwd: Path = field(default_factory=Path.cwd)
    registry: "Registry | None" = None   # /help 等命令用它自省命令表（CMD-P1）
    interactive: bool = False            # 真 TTY?决定 /tree /fork /sessions 走交互选择器还是文本回退。
                                         # 默认 False:测试/headless/rpc 自动走文本(选择器需真终端);
                                         # 仅真 REPL 经 RuntimeHost(interactive=isatty()) 置 True。
    selector_host: object = None         # docs/18：SelectorHost（TuiApp）——交互选择器经它开 in-app
                                         # overlay（run_selector / ask_text）。仅 interactive 时使用；
                                         # 非交互/测试为 None（走文本回退，不触达它）。


# ─── 查找契约 ────────────────────────────────────────────────────

@runtime_checkable
class Registry(Protocol):
    """命令查找契约。实现见 registry.py（CMD-P0 落地）。

    不变量（必须等价于今天 cli.py:562-721 的 if/startswith 链）：
    - most-specific-first：求值顺序等于源码顺序；'/memory eval generate' 绝不被
      '/memory eval' 命中，'/task-stop' 在 '/task' 与 '/' catch-all 之前。
    - 落空→chat：lookup() 对「不是已知命令」的输入返回 None；runner 据此把原始行交给
      session.run_turn（保留未知 /foo 当文本发给模型的行为）。
    - 全角 ／→/ 归一化（cli.py:558-559）发生在 runner 调 lookup() **之前**，属命令解析。
    - 裸词 exit/quit 与 !shell 由 runner 在 slash 分发前特判，不入 registry 的 /-命名空间；
      !shell 保留其权限绕过语义，不得静默并入 PermissionEngine 门。
    """

    def lookup(self, line: str) -> "Command | None": ...

    def specs(self) -> "list[CommandSpec]": ...
