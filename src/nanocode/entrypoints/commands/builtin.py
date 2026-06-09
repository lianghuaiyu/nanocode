"""首批内置 slash 命令的 handler + registry 构造（CMD-P0，见 docs/11）。

逐字镜像 cli.run_repl 现有分支行为（1:1 抽取，不做合并/不改行为）：每个 handler 仍调用
今天的同名函数、保留同样的 print 输出与错误处理。`handle_eval_command` / `_fmt_eval_row`
从 cli 迁入此处（cli 顶部 re-export 以兼容直接调用它们的测试）。

领域 helper（list_memories / discover_skills / sandbox_defaults / tasks_tool）用 call-time
import，使测试可在各自 source 模块打桩拦截；agent/会话状态经 ctx.agent / ctx.session。
"""

from __future__ import annotations

from ...memory import eval_store
from ...ui import print_error, print_info
from .registry import Registry
from .types import Command, CommandContext, CommandSpec, Local


# ─── /memory eval 渲染（自 cli 迁入；cli re-export 保 back-compat）──────────────

def _fmt_eval_row(c) -> str:
    """One-line summary of an eval candidate for REPL listing."""
    q = (c.question or "").strip().replace("\n", " ")
    if len(q) > 70:
        q = q[:67] + "..."
    cat = c.category or "general"
    return f"    {c.id}  [{cat}]  {q}"


def handle_eval_command(rest: str) -> str:
    """Render `/memory eval <rest>` as a string (pure; REPL prints the result).

    Subcommands:
      (empty)|pending|confirmed|rejected   list candidates in that state
      confirm <id>                         confirm a pending candidate (human)
      reject <id>                          reject a pending candidate (human)
    Anything else returns a Usage line.
    """
    parts = (rest or "").split()
    sub = parts[0] if parts else "pending"
    arg = parts[1] if len(parts) > 1 else ""

    usage = ("Usage: /memory eval [pending|confirmed|rejected] | "
             "confirm <id> | reject <id>")

    if sub in ("pending", "confirmed", "rejected"):
        listers = {
            "pending": eval_store.list_pending,
            "confirmed": eval_store.list_confirmed,
            "rejected": eval_store.list_rejected,
        }
        rows = listers[sub]()
        if not rows:
            return f"No {sub} eval candidates."
        lines = [f"{len(rows)} {sub} eval candidate(s):"]
        lines += [_fmt_eval_row(c) for c in rows]
        return "\n".join(lines)

    if sub == "confirm":
        if not arg:
            return usage
        ok = eval_store.confirm(arg)
        return f"Confirmed {arg}." if ok else f"Could not confirm {arg!r} (not a pending candidate)."

    if sub == "reject":
        if not arg:
            return usage
        ok = eval_store.reject(arg)
        return f"Rejected {arg}." if ok else f"Could not reject {arg!r} (not a pending candidate)."

    return usage


# ─── handlers（镜像 cli.run_repl 的分支体）────────────────────────────────────

async def _clear(ctx: CommandContext, args: str) -> Local:
    ctx.agent.clear_history()
    return Local()


async def _plan(ctx: CommandContext, args: str) -> Local:
    ctx.agent.toggle_plan_mode()
    return Local()


async def _cost(ctx: CommandContext, args: str) -> Local:
    ctx.agent.show_cost()
    return Local()


async def _compact(ctx: CommandContext, args: str) -> Local:
    try:
        await ctx.agent.compact()
    except Exception as e:
        print_error(str(e))
    return Local()


async def _memory_consolidate(ctx: CommandContext, args: str) -> Local:
    print_info(await ctx.agent._spawn_memory_consolidate())
    return Local()


async def _memory_eval_generate(ctx: CommandContext, args: str) -> Local:
    print_info(await ctx.agent._spawn_memory_eval())
    return Local()


async def _memory_optimize(ctx: CommandContext, args: str) -> Local:
    print_info(await ctx.agent._spawn_memory_optimize())
    return Local()


async def _memory_eval(ctx: CommandContext, args: str) -> Local:
    print_info(handle_eval_command(args))
    return Local()


async def _memory(ctx: CommandContext, args: str) -> Local:
    from ...memory import list_memories
    memories = list_memories()
    if not memories:
        print_info("No memories saved yet.")
    else:
        print_info(f"{len(memories)} memories:")
        for m in memories:
            print(f"    [{m.type}] {m.name} — {m.description}")
    return Local()


async def _skills(ctx: CommandContext, args: str) -> Local:
    from ...skills import discover_skills
    skills = discover_skills()
    if not skills:
        print_info("No skills found. Add skills to .nanocode/skills/<name>/SKILL.md")
    else:
        print_info(f"{len(skills)} skills:")
        for s in skills:
            tag = f"/{s.name}" if s.user_invocable else s.name
            print(f"    {tag} ({s.source}) — {s.description}")
    return Local()


async def _sandbox(ctx: CommandContext, args: str) -> Local:
    from ...tools import sandbox_defaults
    if not args:
        d = sandbox_defaults.get_defaults()
        print_info("Sandbox session defaults:")
        for k, v in d.items():
            print(f"    {k} = {v}")
        print_info("Set with: /sandbox <persist|network|mount_workspace|deps> <value>")
        return Local()
    toks = args.split()
    if len(toks) == 2:
        try:
            newval = sandbox_defaults.set_default(toks[0], toks[1])
            print_info(f"sandbox {toks[0]} = {newval}")
        except ValueError as e:
            print_error(str(e))
    else:
        print_error("Usage: /sandbox [<key> <value>]")
    return Local()


async def _tasks(ctx: CommandContext, args: str) -> Local:
    from ...tools.tasks_tool import list_tasks_text
    status = args.split()[0] if args else None
    print(list_tasks_text(ctx.agent.task_manager, status, None))
    return Local()


async def _task_stop(ctx: CommandContext, args: str) -> Local:
    from ...tools.tasks_tool import task_stop
    print(await task_stop(ctx.agent.task_manager, ctx.agent._background_tasks, args.strip()))
    return Local()


async def _task(ctx: CommandContext, args: str) -> Local:
    from ...tools.tasks_tool import task_output_text
    print(task_output_text(ctx.agent.task_manager, args.strip()))
    return Local()


async def _agents(ctx: CommandContext, args: str) -> Local:
    from ...tools.tasks_tool import (
        agents_overview_text, list_agent_definitions_text,
        list_subagents_text, agent_definition_detail_text,
        subagent_detail_text,
    )
    toks = args.split(maxsplit=1)
    sub = toks[0] if toks else ""
    if sub == "":
        print(agents_overview_text(ctx.agent.task_manager))
    elif sub == "available":
        print(list_agent_definitions_text(ctx.agent.task_manager))
    elif sub == "running":
        print(list_subagents_text(ctx.agent.task_manager))
    elif sub == "show":
        arg = toks[1].strip() if len(toks) > 1 else ""
        if not arg:
            print_error("Usage: /agents show <name|id>")
        else:
            detail = agent_definition_detail_text(arg)
            print(detail if detail is not None
                  else subagent_detail_text(ctx.agent.task_manager, arg, ctx.agent.session_id))
    else:
        print_error("Usage: /agents [available|running|show <name|id>]")
    return Local()


async def _agent(ctx: CommandContext, args: str) -> Local:
    from ...tools.tasks_tool import subagent_detail_text
    print(subagent_detail_text(ctx.agent.task_manager, args.strip(), ctx.agent.session_id))
    return Local()


async def _trace(ctx: CommandContext, args: str) -> Local:
    """`/trace ...` —— 复用 entrypoints.trace_cmd.run（CMD-P2）。

    隔离两类会杀掉 REPL 的异常：shlex 的引号 ValueError，和 argparse 对 -h / 坏参 raise 的
    SystemExit。trace_cmd.run 是同步的、输出直达 console，故无 await、无字符串捕获。
    """
    import shlex
    from ..trace_cmd import run as run_trace
    try:
        argv = shlex.split(args)
    except ValueError as e:
        print_error(f"trace: {e}")
        return Local()
    try:
        run_trace(argv)
    except SystemExit:
        pass  # -h / 坏参 → SystemExit；吞掉，绝不终止 REPL 进程
    return Local()


async def _help(ctx: CommandContext, args: str) -> Local:
    """列出 REPL 命令（与补全 / --help 共用同一 registry 来源，CMD-P1）。"""
    print_info("REPL commands:")
    if ctx.registry is not None:
        for s in ctx.registry.specs():
            if s.is_hidden:
                continue
            left = f"  {s.name}" + (f" {s.arg_hint}" if s.arg_hint else "")
            print(f"{left:<24} {s.description}")
    print(f'{"  /<skill-name>":<24} Invoke a skill (e.g. /commit "fix types")')
    print(f'{"  !<command>":<24} Run a shell command directly (bypasses agent + permissions)')
    return Local()


# ─── registry 构造 ───────────────────────────────────────────────────────────

# (name, handler, match, description, arg_hint) —— 顺序仅影响同长度 name 的稳定 tie-break；
# 真正的优先级由 Registry 按 name 长度降序保证（most-specific-first）。
_BUILTINS = [
    ("/clear", _clear, "exact", "Clear conversation history", ""),
    ("/plan", _plan, "exact", "Toggle plan mode (read-only)", ""),
    ("/cost", _cost, "exact", "Show token usage and cost", ""),
    ("/compact", _compact, "exact", "Manually compact the conversation", ""),
    ("/memory consolidate", _memory_consolidate, "exact",
     "Run a curator pass to merge/rewrite/archive memories", ""),
    ("/memory eval generate", _memory_eval_generate, "exact",
     "Run an EVAL-mode curator pass to propose pending eval candidates", ""),
    ("/memory optimize", _memory_optimize, "exact",
     "Run EvolveMem on confirmed eval candidates to tune retrieval config", ""),
    ("/memory eval", _memory_eval, "exact_or_prefix",
     "List/confirm/reject memory eval candidates",
     "[pending|confirmed|rejected | confirm <id> | reject <id>]"),
    ("/memory", _memory, "exact", "List saved memories", ""),
    ("/skills", _skills, "exact", "List available skills", ""),
    ("/sandbox", _sandbox, "exact_or_prefix",
     "Show/set sandbox session defaults", "[<key> <value>]"),
    ("/tasks", _tasks, "exact_or_prefix", "List background tasks", "[status]"),
    ("/task-stop", _task_stop, "prefix", "Stop a running background task", "<id>"),
    ("/task", _task, "prefix", "Show a background task's status & log", "<id>"),
    ("/agents", _agents, "exact_or_prefix",
     "Agent definitions + running instances", "[available|running|show <name|id>]"),
    ("/agent", _agent, "prefix", "Show a sub-agent instance's details", "<id>"),
    ("/trace", _trace, "exact_or_prefix",
     "View/summarize recorded agent traces", "[<id>] [--summary|--wire|--tree|--full]"),
    ("/help", _help, "exact", "List REPL commands", ""),
]


def build_registry() -> Registry:
    r = Registry()
    for name, handler, match, desc, hint in _BUILTINS:
        r.register(Command(
            CommandSpec(name=name, kind="local", description=desc, arg_hint=hint, match=match),
            handler,
        ))
    return r
