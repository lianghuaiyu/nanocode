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
from .types import Command, CommandContext, CommandSpec, Control, Local


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
    ctx.agent.agent_session.clear_history()
    return Local()


async def _plan(ctx: CommandContext, args: str) -> Local:
    ctx.agent.toggle_plan_mode()
    return Local()


async def _cost(ctx: CommandContext, args: str) -> Local:
    ctx.agent.show_cost()
    return Local()


async def _context(ctx: CommandContext, args: str) -> Local:
    """/context —— 展示 ContextRuntime 组装的上下文 packs + token 预算 + survival matrix（docs/15 §8.2）。"""
    import os
    from ...context import BudgetPolicy, ContextRequest, ContextRuntime
    a = ctx.agent
    budget = BudgetPolicy.for_window(getattr(a, "effective_window", 200000))
    req = ContextRequest(cwd=os.getcwd(), is_sub_agent=getattr(a, "is_sub_agent", False),
                         include_repo_map=True)
    plan = await ContextRuntime(budget=budget).collect(req)
    print_info(plan.ledger.render_summary())
    return Local()


async def _compact(ctx: CommandContext, args: str) -> Local:
    try:
        await ctx.agent.agent_session.compact()
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
        # docs/14 §6b：磁盘派生的 child session（经 header parentSession 回指），survives restart，
        # 不依赖 in-process task_manager。可 `/resume <child-sid>` 进入、`/parent` 回来。
        from ...session.manager import children
        kids = children(ctx.agent.session_id)
        if kids:
            print("\nChild sessions (/resume <id> to enter):")
            for k in kids:
                print(f"    {k}")
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


async def _agent(ctx: CommandContext, args: str) -> "Control | Local":
    """/agent <id> —— 若 id 对应一个 child session（docs/14 §6b）则导航进入（Control resume）；否则打印
    子 agent 详情。`/agent next|prev` 在兄弟 child session 间循环；从父 session 上 `next` 进入首个 child。"""
    from ...session.manager import SessionManager, children, parent_of, siblings
    sid = ctx.agent.session_id
    arg = args.strip()
    if arg in ("next", "prev"):
        # 当前在父：兄弟集 = children(sid)；当前在 child：兄弟集 = siblings + 自己（同父下）。
        if children(sid):
            ring = children(sid)            # 父视角：进入其 child 环
        else:
            par = parent_of(sid)
            ring = sorted(set(siblings(sid)) | {sid}) if par else [sid]
        if len(ring) <= 1 and sid in ring and not children(sid):
            print_info("No sibling sessions to cycle.")
            return Local()
        cur = ring.index(sid) if sid in ring else -1
        nxt = ring[(cur + (1 if arg == "next" else -1)) % len(ring)]
        if nxt == sid:
            print_info("Already at the only session in this group.")
            return Local()
        return Control("resume", {"sessionId": nxt})
    if arg:
        child_sid = ctx.agent.child_session_id(arg) if hasattr(ctx.agent, "child_session_id") else None
        target = (arg if SessionManager.exists(arg) and parent_of(arg)         # 已是 child sid
                  else child_sid if child_sid and SessionManager.exists(child_sid) else None)
        if target:
            return Control("resume", {"sessionId": target})
    from ...tools.tasks_tool import subagent_detail_text
    print(subagent_detail_text(ctx.agent.task_manager, arg, sid))
    return Local()


async def _tree(ctx: CommandContext, args: str) -> Local:
    """/tree [entry] —— 无参打印 canonical session 树（entry 结构 + 当前 leaf）；带 entry 则把 active
    leaf 移到该 entry 并重载上下文（in-file 导航，主路径；等同 /checkout，docs/14 §5.1）。"""
    if args.strip():
        return await _checkout(ctx, args)
    from ...session.manager import SessionManager
    sid = ctx.agent.session_id
    if not SessionManager.exists(sid):
        print("No canonical session tree yet for this session.")
        return Local()
    mgr = SessionManager.open(sid)
    leaf = mgr.get_leaf()
    name = mgr.name() or "(unnamed)"
    lines = [f"session tree [{sid}] {name} — {len(mgr.entries())} entries, leaf=…{str(leaf)[-8:]}"]
    for e in mgr.entries():
        label = e.type
        if e.type == "message":
            label = f"message/{(e.data.get('message') or {}).get('role', '?')}"
        elif e.type == "compaction":
            label = "compaction(summary)"
        mark = "  ← leaf" if e.id == leaf else ""
        # uuidv7 是时间有序：同毫秒 id 前缀相同，唯一部分在尾部 → 展示尾 8 位作 handle。
        parent = "root" if not e.parentId else "…" + e.parentId[-8:]
        lines.append(f"  …{e.id[-8:]}  ↰{parent}  {label}{mark}")
    print("\n".join(lines))
    return Local()


async def _checkout(ctx: CommandContext, args: str) -> Local:
    """/checkout <entry_id> —— 把 active leaf 移到树中某 entry 并重载上下文（in-file 导航，docs/13 P6）。"""
    target = args.strip()
    if not target:
        print_error("Usage: /checkout <entry_id>  (run /tree to see entry ids)")
        return Local()
    from ...session.manager import SessionManager
    sid = ctx.agent.session_id
    if SessionManager.exists(sid):  # 解析：exact > suffix(尾部唯一) > prefix
        ids = [e.id for e in SessionManager.open(sid).entries()]
        matches = ([i for i in ids if i == target] or [i for i in ids if i.endswith(target)]
                   or [i for i in ids if i.startswith(target)])
        if len(matches) == 1:
            target = matches[0]
        elif len(matches) > 1:
            print_error(f"ambiguous id '{target}' ({len(matches)} matches) — use a longer suffix")
            return Local()
    try:
        msgs = ctx.session.move_to(target)
        print(f"Checked out {target[:12]} — context reloaded ({len(msgs)} messages).")
    except ValueError as e:
        print_error(str(e))
    return Local()


async def _rewind(ctx: CommandContext, args: str) -> Local:
    """/rewind —— 回到最近一条 user 消息之前（撤销上一轮；后续输入在 in-file 新分支重开，docs/13 P6）。"""
    from ...session.manager import SessionManager
    sid = ctx.agent.session_id
    if not SessionManager.exists(sid):
        print("No canonical session tree yet for this session.")
        return Local()
    target = _last_user_message(SessionManager.open(sid))
    if target is None:
        print("No user message to rewind to.")
        return Local()
    try:
        ctx.session.move_to(target.parentId)         # parentId=None（首条消息）→ 复位到 root（空上下文）
        content = (target.data.get("message") or {}).get("content")
        print(f"Rewound to before your last message (in-file branch). Re-enter it if you like:\n  {content}")
    except ValueError as e:
        print_error(str(e))
    return Local()


async def _new(ctx: CommandContext, args: str) -> Control:
    """/new —— 新建一个空 canonical session 并切入（docs/14 P2）。运行时经 Control → runtime
    replacement（AgentRuntime.thread_new）原子换掉整组 Agent 状态；旧 session 被 finalize、可
    `/resume` 回去。handler 只发信号，不碰 live agent。"""
    return Control("replace_thread", {"kind": "new"})


def _resolve_entry(mgr, target: str):
    """解析 entry id：exact > 尾部唯一 suffix > 前缀 prefix。返回 (resolved_id | None, error | None)。"""
    ids = [e.id for e in mgr.entries()]
    matches = ([i for i in ids if i == target] or [i for i in ids if i.endswith(target)]
               or [i for i in ids if i.startswith(target)])
    if len(matches) == 1:
        return matches[0], None
    return None, f"ambiguous/unknown id '{target}' ({len(matches)} matches)"


# ─── 会话导航命令族（pi 对齐语义表 = agent/runtime.py AgentRuntime docstring，唯一权威）──
#   /tree      同文件移动 leaf，不新建 session
#   /fork      选 user message，复制其 parent 之前的路径到新 session，prompt 回填编辑器
#   /clone     复制当前 active branch 到当前 leaf → 新 session，编辑器为空
#   /new       新顶层 session（不带 parentSession）

def _is_user_message(e) -> bool:
    """entry 是否为 user MESSAGE（fork 目标 / rewind 锚点 / 候选清单共用的唯一谓词）。"""
    from ...session import tree as T
    return e.type == T.MESSAGE and (e.data.get("message") or {}).get("role") == "user"


def _last_user_message(mgr):
    """branch 上最近一条 user MESSAGE entry（无则 None）。/fork 无参与 /rewind 共用。"""
    sel = None
    for e in mgr.get_branch():                     # root-first → 末个命中即最近
        if _is_user_message(e):
            sel = e
    return sel


def _user_message_candidates(mgr, limit: int = 10) -> str:
    """fork 候选清单（pi getUserMessagesForForking 的文本等价）：branch 上的 user MESSAGE，
    近期在前，id 尾缀 + 预览——选择器没有 TUI 时，把候选集打印出来让用户挑。"""
    rows = []
    for e in reversed(mgr.get_branch()):
        if _is_user_message(e):
            text = _user_message_text(e.data.get("message")).strip().replace("\n", " ")
            if len(text) > 60:
                text = text[:57] + "..."
            rows.append(f"    …{e.id[-8:]}  {text}")
            if len(rows) >= limit:
                break
    return "\n".join(rows) if rows else "    (no user messages on this branch)"


def _user_message_text(msg: dict) -> str:
    """中立 user Message 的纯文本（prefill 用）：str 直通；block 列表拼接 text 字段。"""
    c = (msg or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


async def _fork(ctx: CommandContext, args: str) -> "Control | Local":
    """/fork [entry] —— pi 语义：选择一条历史 user 消息，**新建 session** 复制到该消息**之前**，
    并把该 prompt **放回编辑器**（预填下一次输入，可改可发）。无参 = 最近一条 user 消息。
    原 session 保留（header 记 parentSession 血缘）。同 session 内的 leaf 移动用 /tree <entry>。"""
    mgr = ctx.agent._session_mgr
    if mgr is None:
        print_info("No active session for this agent.")
        return Local()
    target = args.strip()
    if target:
        resolved, err = _resolve_entry(mgr, target)
        if resolved is None:
            print_error(err)
            return Local()
        sel = next((e for e in mgr.entries() if e.id == resolved), None)
    else:
        sel = _last_user_message(mgr)              # 无参 = 最近一条 user 消息
    if sel is None or not _is_user_message(sel):
        # pi 双层收窄的 UX 层：候选集只有 user 消息——选错/无效时把候选打印出来让用户挑
        # （runtime.thread_fork 仍独立 fail-closed 校验，不依赖这里）。
        print_error("Fork target must be a user message. Candidates (/fork <id>):\n"
                    + _user_message_candidates(mgr))
        return Local()
    return Control("replace_thread", {
        "kind": "fork", "sourceSid": ctx.agent.session_id, "userEntryId": sel.id,
        "prefill": _user_message_text(sel.data.get("message")),
    })


async def _clone(ctx: CommandContext, args: str) -> "Control | Local":
    """/clone —— pi 语义：**新建 session**，复制当前 active branch 到**当前 leaf** 并切入；
    编辑器为空（docs/14 P4 / §5.5；header 记 parentSession 血缘，原 session 保留）。
    无参数——要在某条 user 消息之前分叉用 /fork，同 session 内移动 leaf 用 /tree <entry>。"""
    from ...session.manager import SessionManager
    if args.strip():
        print_error("/clone takes no arguments (it copies the current branch to the current leaf). "
                    "Use /fork [entry] to branch before a user message.")
        return Local()
    sid = ctx.agent.session_id
    if not SessionManager.exists(sid):
        print_info("No canonical session tree yet for this session.")
        return Local()
    return Control("replace_thread", {"kind": "clone", "sourceSid": sid})


async def _name(ctx: CommandContext, args: str) -> Local:
    """/name [text] —— 无参显示当前 session 名；/name <text> 设名（写 session_info，不移动 leaf）；
    /name --clear（或显式空）清空为 tombstone（docs/14 P4 / §5.6）。

    docs/14 SessionLease：设名是写操作（append_session_info），走 active 写者租约（ctx.agent._session_mgr）；
    缺租约则只读提示，不再 lazy 打开未加锁 mgr。"""
    mgr = ctx.agent._session_mgr
    text = args.strip()
    if not text:
        print_info(f"Session name: {mgr.name() or '(unnamed)'}" if mgr is not None
                   else "No session name (no active session lease).")
        return Local()
    if mgr is None:
        print_error("No active session writer lease for this session.")
        return Local()
    mgr.append_session_info("" if text == "--clear" else text)
    print_info("Session name cleared." if text == "--clear" else f"Session name set to {text!r}.")
    return Local()


def _child_session_ids() -> set:
    """有 parentSession 回指的 child session id 集合（docs/14 §6b）。/resume 列表隐藏它们、
    且前缀匹配排除它们（只可经 exact id 或 /agents 进入），避免 child sid 污染父的短前缀解析。"""
    from ...session.manager import _scan_headers
    return {sid for sid, ps in _scan_headers() if ps}


async def _resume(ctx: CommandContext, args: str) -> "Control | Local":
    """/resume [<id>] —— 无参列出可恢复 session；带 id 返回 Control("resume") 交 runtime 原子切换
    （AgentRuntime.thread_resume：rebind + 重建 thread；canonical 树缺则拒绝，docs/16 C-3）。"""
    from ...session import tree as _tree
    from ...session.manager import SessionManager, _scan_headers
    target = args.strip()
    if target:
        # --fork：目标被占用时显式 clone 成新 session（绝不做第二个 writer，docs/14 §5.2）。
        toks = target.split()
        fork = "--fork" in toks
        toks = [t for t in toks if t != "--fork"]
        target = toks[0] if toks else ""
        if not target:
            print_error("Usage: /resume <id> [--fork]")
            return Local()
        # handler 只做候选 resolve（exact > prefix）；真正切换由 runtime 经 Control 完成（docs/14 §3.4）。
        # 候选只来自 canonical session.jsonl header（docs/16 C-3：legacy flat/v2 发现面已删）。
        known = {sid for sid, _ps in _scan_headers()}
        cand = ([s for s in known if s == target]
                or [s for s in known if s.startswith(target) and s not in _child_session_ids()])
        resolved = cand[0] if len(cand) == 1 else (target if SessionManager.exists(target) else None)
        if resolved is None:
            print_error(f"unknown session '{target}'. Run /resume to list, or relaunch with "
                        f"`nanocode --resume {target}`.")
            return Local()
        return Control("resume", {"sessionId": resolved, "fork": fork})
    headers = _scan_headers()
    if not headers:
        print("No sessions found.")
        return Local()
    # 默认隐藏 child session（有 parentSession header 回指）——它们经 /agents /agent 导航（docs/14 §5.2）。
    child_ids = {sid for sid, ps in headers if ps}
    top = sorted(s for s, _ps in headers if s not in child_ids or s == ctx.agent.session_id)
    lines = ["Resumable sessions (/resume <id> to switch; child sessions hidden — see /agents):"]
    for s in top:
        n = sum(1 for e in SessionManager.open(s).entries() if e.type == _tree.MESSAGE)
        mark = "  ← current" if s == ctx.agent.session_id else ""
        lines.append(f"  {s}  messages={n}{mark}")
    print("\n".join(lines))
    return Local()


async def _parent(ctx: CommandContext, args: str) -> "Control | Local":
    """/parent —— 切到当前 session 的父 session（docs/14 §6b child-session 导航）。顶层 session 无父则提示。
    运行时经 Control → thread_resume（rebind）。"""
    from ...session.manager import SessionManager
    sid = ctx.agent.session_id
    mgr = ctx.agent._session_mgr or (SessionManager.open(sid) if SessionManager.exists(sid) else None)
    ps = mgr.parent_session() if mgr is not None else None
    if not ps or not ps.get("sessionId"):
        print_info("This is a top-level session (no parent).")
        return Local()
    return Control("resume", {"sessionId": ps["sessionId"]})


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
    ("/context", _context, "exact", "Show context packs, token budget & compaction survival", ""),
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
    ("/parent", _parent, "exact", "Switch to this session's parent session (child-session nav)", ""),
    ("/tree", _tree, "exact_or_prefix", "Show the session tree, or /tree <entry> to navigate (move leaf)", "[entry_id]"),
    ("/checkout", _checkout, "prefix", "Move the active leaf to a tree entry (in-file navigation)", "<entry_id>"),
    ("/rewind", _rewind, "exact", "Rewind to before your last message (in-file branch)", ""),
    ("/new", _new, "exact", "Start a new empty session and switch to it", ""),
    ("/clone", _clone, "exact_or_prefix", "New session: copy the current branch up to the current leaf and switch (editor empty)", ""),
    ("/fork", _fork, "exact_or_prefix", "New session: copy up to BEFORE a user message and put that prompt back in the editor", "[entry_id]"),
    ("/name", _name, "exact_or_prefix", "Show or set this session's name (/name <text> | --clear)", "[text]"),
    ("/resume", _resume, "exact_or_prefix", "List resumable sessions, or /resume <id> to switch mid-REPL", "[id]"),
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
