"""首批内置 slash 命令的 handler + registry 构造（CMD-P0，见 docs/11）。

领域 helper（list_memories / discover_skills / tasks_tool）用 call-time
import，使测试可在各自 source 模块打桩拦截；会话状态经 ctx.thread（RuntimeThread 稳定命令面，docs/17 B-list）。
"""

from __future__ import annotations

from .registry import Registry
from .types import Command, CommandContext, CommandSpec, Control, Local


def _error(text: str) -> Local:
    return Local(output=f"Error: {text}")


# ─── /memory eval 渲染 ───────────────────────────────────────

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
    from ...memory import eval_store

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
    ctx.thread.clear_history()
    return Local()


async def _plan(ctx: CommandContext, args: str) -> Local:
    ctx.thread.toggle_plan_mode()
    return Local()


async def _cost(ctx: CommandContext, args: str) -> Local:
    ctx.thread.show_cost()
    return Local()


async def _context(ctx: CommandContext, args: str) -> Local:
    """/context —— 展示 ContextRuntime 组装的上下文 packs + token 预算 + survival matrix（docs/15 §8.2）。"""
    from pathlib import Path
    from ...context import BudgetPolicy, ContextRequest, ContextRuntime
    from ...context.model_policy import model_uses_repo_map
    from ...tools.permissions import load_context_config
    from ...runtime import _push_cwd
    budget = BudgetPolicy.for_window(ctx.thread.effective_window)
    cwd = getattr(ctx, "cwd", Path.cwd())
    with _push_cwd(str(cwd)):
        cfg = load_context_config()
    configured_map_tokens = cfg["map_tokens"]
    model = getattr(ctx.thread, "model", "")
    map_tokens = (1024 if configured_map_tokens is None and model_uses_repo_map(model)
                  else (configured_map_tokens or 0))
    req = ContextRequest(cwd=str(cwd), is_sub_agent=ctx.thread.is_sub_agent,
                         include_repo_map=map_tokens > 0,
                         map_tokens=map_tokens,
                         map_refresh=cfg["map_refresh"],
                         map_multiplier_no_files=cfg["map_multiplier_no_files"],
                         tool_registry=ctx.thread.tool_registry)
    services = getattr(ctx.thread, "services", None)
    plan = await ContextRuntime(budget=budget, sources=(services.context_sources if services is not None else None)).collect(req)
    return Local(output=plan.ledger.render_summary())


async def _compact(ctx: CommandContext, args: str) -> Local:
    try:
        await ctx.thread.compact(args.strip() or None)
    except Exception as e:
        return _error(str(e))
    return Local()


async def _memory_consolidate(ctx: CommandContext, args: str) -> Local:
    return Local(output=await ctx.thread.spawn_memory_consolidate())


# docs/22: /memory optimize and /memory eval generate are registered by the
# memory-evolution system extension. They are intentionally not builtin
# handlers, so there is no second optimize truth source.


async def _memory_generate(ctx: CommandContext, args: str) -> Local:
    force = args.strip().lower() in ("--force", "-f", "force")
    return Local(output=await ctx.thread.generate_memory(force=force))


async def _memory_eval(ctx: CommandContext, args: str) -> Local:
    return Local(output=handle_eval_command(args))


async def _memory(ctx: CommandContext, args: str) -> Local:
    return Local(output=ctx.thread.memory_overview())


async def _skills(ctx: CommandContext, args: str) -> Local:
    from ...skills import discover_skills
    skills = discover_skills()
    if not skills:
        return Local(output="No skills found. Add skills to .nanocode/skills/<name>/SKILL.md")
    lines = [f"{len(skills)} skills:"]
    for s in skills:
        tag = f"/{s.name}" if s.user_invocable else s.name
        lines.append(f"    {tag} ({s.source}) — {s.description}")
    return Local(output="\n".join(lines))


async def _sandbox(ctx: CommandContext, args: str) -> Local:
    """/sandbox —— 显示当前 sandbox 策略；`/sandbox <profile>` 切换 profile（docs/19）。

    无 module-global 可变默认值——profile 是 runtime/session state，模型无法影响。
    """
    if args.strip():
        try:
            name = ctx.thread.set_sandbox_profile(args.strip())
        except ValueError as e:
            return _error(str(e))
        return Local(output=f"sandbox profile = {name}")
    s = ctx.thread.sandbox_status()
    lines = [
        "Sandbox policy (active session):",
        f"    profile          {s['profile']}",
        f"    engine           {s['engine']}",
        f"    network          {s['network']}",
        f"    writable roots   {', '.join(s['writable_roots']) or '(none / read-only)'}",
        f"    protected roots  {', '.join(s['protected_roots']) or '(none)'}",
        f"    native backend   {'available' if s['native_available'] else 'unavailable'}",
        f"    vm backend       {'available' if s['vm_available'] else 'unavailable'}",
        "Switch with: /sandbox <default|read-only|strict|vm|danger-full-access>",
    ]
    return Local(output="\n".join(lines))


async def _tasks(ctx: CommandContext, args: str) -> Local:
    status = args.split()[0] if args else None
    return Local(output=ctx.thread.task_list(status, None))


async def _task_stop(ctx: CommandContext, args: str) -> Local:
    return Local(output=await ctx.thread.task_stop(args.strip()))


async def _task(ctx: CommandContext, args: str) -> Local:
    return Local(output=ctx.thread.task_output(args.strip()))


async def _agents(ctx: CommandContext, args: str) -> "Control | Local":
    toks = args.split(maxsplit=1)
    sub = toks[0] if toks else ""
    if sub == "":
        if ctx.interactive and ctx.selector_host is not None:
            from ...tui.session_pages.agents import run_agents_page
            res = await run_agents_page(ctx.thread, host=ctx.selector_host)
            if res and res.get("action") == "resume":
                return Control("resume", {"sessionId": res["session_id"]})
            return Local()
        return Local(output=ctx.thread.agents_overview())
    elif sub == "available":
        if ctx.interactive and ctx.selector_host is not None:
            from ...tui.session_pages.agents import view_agent_text
            await view_agent_text(ctx.selector_host, "Agent types", ctx.thread.agent_definitions())
            return Local()
        return Local(output=ctx.thread.agent_definitions())
    elif sub == "running":
        if ctx.interactive and ctx.selector_host is not None:
            from ...tui.session_pages.agents import run_agent_runs
            res = await run_agent_runs(ctx.thread, host=ctx.selector_host)
            if res and res.get("action") == "resume":
                return Control("resume", {"sessionId": res["session_id"]})
            return Local()
        return Local(output=ctx.thread.subagents())
    elif sub == "show":
        arg = toks[1].strip() if len(toks) > 1 else ""
        if not arg:
            return _error("Usage: /agents show <name|id>")
        else:
            return Local(output=ctx.thread.agent_detail(arg))
    else:
        return _error("Usage: /agents [available|running|show <name|id>]")


async def _agent(ctx: CommandContext, args: str) -> "Control | Local":
    """/agent <id> —— 若 id 对应一个 child session（docs/14 §6b）则导航进入（Control resume）；否则打印
    子 agent 详情。`/agent next|prev` 在兄弟 child session 间循环；从父 session 上 `next` 进入首个 child。"""
    from ...session.manager import SessionManager, children, parent_of, siblings
    sid = ctx.thread.session_id
    arg = args.strip()
    if not arg:
        return Local(output="Usage: /agent <id|name|next|prev>\nUse /agents for overview.")
    if arg in ("next", "prev"):
        # 当前在父：兄弟集 = children(sid)；当前在 child：兄弟集 = siblings + 自己（同父下）。
        if children(sid):
            ring = children(sid)            # 父视角：进入其 child 环
        else:
            par = parent_of(sid)
            ring = sorted(set(siblings(sid)) | {sid}) if par else [sid]
        if len(ring) <= 1 and sid in ring and not children(sid):
            return Local(output="No sibling sessions to cycle.")
        cur = ring.index(sid) if sid in ring else -1
        nxt = ring[(cur + (1 if arg == "next" else -1)) % len(ring)]
        if nxt == sid:
            return Local(output="Already at the only session in this group.")
        return Control("resume", {"sessionId": nxt})
    if arg:
        child_sid = ctx.thread.child_session_id(arg)
        target = (arg if SessionManager.exists(arg) and parent_of(arg)         # 已是 child sid
                  else child_sid if child_sid and SessionManager.exists(child_sid) else None)
        if target:
            return Control("resume", {"sessionId": target})
    return Local(output=ctx.thread.agent_detail(arg))


async def _tree(ctx: CommandContext, args: str) -> Local:
    """/tree —— 无参:TTY 进交互树选择器(↑↓/←→/Ctrl+O/Shift+L/Shift+T/enter/esc);
    非 TTY 打印文本树。/tree <entry>:把 active leaf 移到该 entry（in-file 导航）。"""
    if args.strip():
        return await _checkout(ctx, args)
    sid = ctx.thread.session_id
    mgr = ctx.thread.readonly_session()
    if mgr is None:
        return Local(output="No canonical session tree yet for this session.")
    if not ctx.interactive:
        from ...session.tree_view import render_tree_text
        name = mgr.name() or "(unnamed)"
        leaf = mgr.get_leaf()
        head = f"session tree [{sid}] {name} — leaf=…{str(leaf)[-8:]}"
        return Local(output=head + "\n" + "\n".join(render_tree_text(mgr.entries(), leaf)))
    from ...tui.session_pages.tree import run_tree
    res = await run_tree(mgr, host=ctx.selector_host, set_label=ctx.thread.set_entry_label)
    if res and res.get("action") == "checkout":
        entry = next((e for e in mgr.entries() if e.id == res["entry_id"]), None)
        if entry is None:
            return _error(f"entry '{res['entry_id']}' not found")
        return await _move_tree_leaf(ctx, entry, resolved_label=f"…{entry.id[-8:]}")
    return Local()


async def _checkout(ctx: CommandContext, args: str) -> Local:
    """把 active leaf 移到树中某 entry 并重载上下文（in-file 导航）。`_tree` 的 <entry> 直达内部复用。"""
    target = args.strip()
    if not target:
        return _error("Usage: /tree <entry_id>  (run /tree to browse entry ids)")
    mgr = ctx.thread.readonly_session()
    if mgr is None:
        return _error("No canonical session tree yet for this session.")
    entry = None
    entries = mgr.entries()
    ids = [e.id for e in entries]
    matches = ([i for i in ids if i == target] or [i for i in ids if i.endswith(target)]
               or [i for i in ids if i.startswith(target)])
    if len(matches) == 1:
        target = matches[0]
        entry = next((e for e in entries if e.id == target), None)
    elif len(matches) > 1:
        return _error(f"ambiguous id '{target}' ({len(matches)} matches) — use a longer suffix")
    if entry is None:
        return _error(f"entry '{target}' not found in session tree; session left unchanged")
    return await _move_tree_leaf(ctx, entry, resolved_label=target[:12])


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


# ─── 会话导航命令族（pi 对齐语义表 = runtime/facade.py AgentRuntime docstring，唯一权威）──
#   /tree      同文件移动 leaf，不新建 session
#   /fork      选 user message，复制其 parent 之前的路径到新 session，prompt 回填编辑器
#   /clone     复制当前 active branch 到当前 leaf → 新 session，编辑器为空
#   /new       新顶层 session（不带 parentSession）

def _is_user_message(e) -> bool:
    """entry 是否为 user MESSAGE（fork 目标 / rewind 锚点 / 候选清单共用的唯一谓词）。"""
    from ...session import tree as T
    return e.type == T.MESSAGE and (e.data.get("message") or {}).get("role") == "user"


def _last_user_message(mgr):
    """branch 上最近一条 user MESSAGE entry（无则 None）。/fork 无参用。"""
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


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _tree_checkout_target(entry):
    """Pi `/tree` selection semantics: user/custom entries are edited from their parent."""
    from ...session import tree as T
    if _is_user_message(entry):
        return entry.parentId, _user_message_text(entry.data.get("message"))
    if entry.type == T.CUSTOM_MESSAGE:
        return entry.parentId, _content_text(entry.data.get("content"))
    return entry.id, None


async def _branch_summary_focus(ctx: CommandContext, target_id: str | None) -> str | None | bool:
    """Return False for no summary, None for default summary, or a custom focus string."""
    if not ctx.interactive or ctx.selector_host is None:
        return False
    if not ctx.thread.branch_summary_available(target_id):
        return False
    from ...tui.selector import ChoiceItem, ChoiceModel

    while True:
        outcome = await ctx.selector_host.run_selector(
            ChoiceModel(
                "Summarize branch?",
                [
                    ChoiceItem("No summary", False),
                    ChoiceItem("Summarize", None),
                    ChoiceItem("Summarize with custom prompt", "custom"),
                ],
            )
        )
        if outcome.kind != "done":
            return False
        choice = getattr(outcome.item, "value", False)
        if choice != "custom":
            return choice
        text = await ctx.selector_host.ask_text("Custom branch summary focus: ")
        if text is None:
            continue
        answer = text.strip()
        return answer if answer else False


async def _move_tree_leaf(ctx: CommandContext, entry, *, resolved_label: str) -> Local:
    target_id, prefill = _tree_checkout_target(entry)
    focus = await _branch_summary_focus(ctx, target_id)
    try:
        if focus is False:
            msgs = ctx.thread.move_to(target_id)
            summary_note = ""
        else:
            msgs = await ctx.thread.move_to_with_branch_summary(
                target_id, focus=(focus if isinstance(focus, str) else None))
            summary_note = " with branch summary"
        if prefill is not None:
            return Local(
                output=f"Checked out before {resolved_label}{summary_note} — "
                       f"context reloaded ({len(msgs)} messages); prompt loaded.",
                refresh_transcript=True,
                prefill=prefill,
            )
        return Local(
            output=f"Checked out {resolved_label}{summary_note} — context reloaded ({len(msgs)} messages).",
            refresh_transcript=True,
        )
    except ValueError as e:
        return _error(str(e))


async def _fork(ctx: CommandContext, args: str) -> "Control | Local":
    """/fork [entry] —— pi 语义：选择一条历史 user 消息，**新建 session** 复制到该消息**之前**，
    并把该 prompt **放回编辑器**（预填下一次输入，可改可发）。原 session 保留（header 记 parentSession）。
    无参 + TTY → 交互选择器挑 user 消息；无参 + 非 TTY → 最近一条 user 消息；/fork <entry> → 指定。"""
    mgr = ctx.thread.readonly_session()
    if mgr is None:
        return Local(output="No active session for this agent.")
    target = args.strip()
    if not target and ctx.interactive:
        # TTY：独立的 user-message 平铺选择器（Pi UserMessageSelectorComponent），不复用 tree 页。
        from ...tui.session_pages.fork import run_fork
        res = await run_fork(mgr, host=ctx.selector_host)
        if not res:
            return Local()
        sel = next((e for e in mgr.entries() if e.id == res["entry_id"]), None)
        if sel is None or not _is_user_message(sel):
            return _error("Fork target must be a user message.")
        return Control("replace_thread", {
            "kind": "fork", "sourceSid": ctx.thread.session_id, "userEntryId": sel.id,
            "prefill": _user_message_text(sel.data.get("message")),
        })
    if target:
        resolved, err = _resolve_entry(mgr, target)
        if resolved is None:
            return _error(err)
        sel = next((e for e in mgr.entries() if e.id == resolved), None)
    else:
        sel = _last_user_message(mgr)              # 无参 = 最近一条 user 消息
    if sel is None or not _is_user_message(sel):
        # pi 双层收窄的 UX 层：候选集只有 user 消息——选错/无效时把候选打印出来让用户挑
        # （runtime.thread_fork 仍独立 fail-closed 校验，不依赖这里）。
        return _error("Fork target must be a user message. Candidates (/fork <id>):\n"
                      + _user_message_candidates(mgr))
    return Control("replace_thread", {
        "kind": "fork", "sourceSid": ctx.thread.session_id, "userEntryId": sel.id,
        "prefill": _user_message_text(sel.data.get("message")),
    })


async def _clone(ctx: CommandContext, args: str) -> "Control | Local":
    """/clone —— pi 语义：**新建 session**，复制当前 active branch 到**当前 leaf** 并切入；
    编辑器为空（docs/14 P4 / §5.5；header 记 parentSession 血缘，原 session 保留）。
    无参数——要在某条 user 消息之前分叉用 /fork，同 session 内移动 leaf 用 /tree <entry>。"""
    if args.strip():
        return _error("/clone takes no arguments (it copies the current branch to the current leaf). "
                      "Use /fork [entry] to branch before a user message.")
    sid = ctx.thread.session_id
    if ctx.thread.readonly_session() is None:
        return Local(output="No canonical session tree yet for this session.")
    return Control("replace_thread", {"kind": "clone", "sourceSid": sid})


async def _name(ctx: CommandContext, args: str) -> Local:
    """/name [text] —— 无参显示当前 session 名；/name <text> 设名（写 session_info，不移动 leaf）；
    /name --clear（或显式空）清空为 tombstone（docs/14 P4 / §5.6）。

    docs/14 SessionLease：设名是写操作（append_session_info），走 RuntimeThread operation；
    缺租约则只读提示，不再 lazy 打开未加锁 mgr。"""
    text = args.strip()
    if not text:
        return Local(output=f"Session name: {ctx.thread.session_name() or '(unnamed)'}")
    try:
        ctx.thread.set_session_name("" if text == "--clear" else text)
    except RuntimeError:
        return _error("No active session writer lease for this session.")
    return Local(output="Session name cleared." if text == "--clear" else f"Session name set to {text!r}.")


def _child_session_ids() -> set:
    """被 spawn 的 **subagent** child session id 集合（docs/26 C2）。/resume 列表隐藏它们、
    且前缀匹配排除它们（只可经 exact id 或 /agents 进入）。fork/clone 不在此——它们是用户可
    前缀解析的正常会话。"""
    from ...session.manager import _scan_headers
    return {sid for sid, sb, _ff in _scan_headers() if sb}


async def _resume(ctx: CommandContext, args: str) -> "Control | Local":
    """/resume [<id>] —— Pi 语义:无参 + TTY 打开交互会话浏览器(按 parentSession 嵌套树 + 右栏详情,
    enter resume / r rename / tab scope);无参 + 非 TTY 列文本;带 id 直达,返回 Control("resume")。"""
    from ...session.manager import SessionManager, _scan_headers
    target = args.strip()
    if target:
        # --fork：目标被占用时显式 clone 成新 session（绝不做第二个 writer，docs/14 §5.2）。
        toks = target.split()
        fork = "--fork" in toks
        toks = [t for t in toks if t != "--fork"]
        target = toks[0] if toks else ""
        if not target:
            return _error("Usage: /resume <id> [--fork]")
        # handler 只做候选 resolve（exact > prefix）；真正切换由 runtime 经 Control 完成（docs/14 §3.4）。
        # 候选只来自 canonical session.jsonl header（docs/16 C-3：legacy flat/v2 发现面已删）。
        known = {sid for sid, _sb, _ff in _scan_headers()}
        cand = ([s for s in known if s == target]
                or [s for s in known if s.startswith(target) and s not in _child_session_ids()])
        resolved = cand[0] if len(cand) == 1 else (target if SessionManager.exists(target) else None)
        if resolved is None:
            return _error(f"unknown session '{target}'. Run /resume to list, or relaunch with "
                          f"`nanocode --session {target}`.")
        if not fork and resolved == ctx.thread.session_id:
            return Local(refresh_transcript=True)
        return Control("resume", {"sessionId": resolved, "fork": fork})
    # 无参 + TTY → 交互浏览器（Pi /resume 打开 session selector UI）。
    if ctx.interactive:
        import os
        sid = ctx.thread.session_id
        mgr = ctx.thread.readonly_session()
        cwd = mgr._cwd() if mgr is not None else os.getcwd()
        from ...tui.session_pages.resume import run_sessions
        res = await run_sessions(current_sid=sid, cwd=cwd, host=ctx.selector_host,
                                 rename_current=ctx.thread.set_session_name)
        if res and res.get("action") == "resume" and res["sid"] != sid:
            return Control("resume", {"sessionId": res["sid"]})
        if res and res.get("action") == "resume" and res["sid"] == sid:
            return Local(refresh_transcript=True)
        return Local()
    # 无参 + 非 TTY → 文本列表（headless/脚本）:按 parentSession 嵌套 + origin,与交互浏览器同源。
    import time as _time
    from ...session import listing as _SM
    infos = _SM.scan_sessions()
    if not infos:
        return Local(output="No sessions found.")
    body = "\n".join(_SM.render_sessions_text(infos, ctx.thread.session_id, _time.time()))
    return Local(output="Resumable sessions (/resume <id> to switch):\n" + body)


async def _session(ctx: CommandContext, args: str) -> Local:
    """/session —— 显示**当前** session 的信息与统计（Pi /session）。跨 session 浏览/切换用 /resume。"""
    from ...session import tree as _tree
    sid = ctx.thread.session_id
    mgr = ctx.thread.readonly_session()
    if mgr is None:
        return Local(output="No active session.")
    msgs = [e for e in mgr.entries() if e.type == _tree.MESSAGE]
    ff = mgr.forked_from()
    origin = "root" if not ff else ("fork" if ff.get("forkedBeforeEntryId") else "clone")
    st = ctx.thread.status()
    lines = [
        f"Session {sid}",
        f"  name     {mgr.name() or '(unnamed)'}",
        f"  cwd      {mgr._cwd()}",
        f"  model    {st['model']}",
        f"  origin   {origin}" + (f"  (parent …{ff['sessionId'][-8:]})" if ff and ff.get('sessionId') else ""),
        f"  entries  {len(msgs)} messages    leaf …{str(mgr.get_leaf())[-8:]}",
        f"  tokens   ↑{st['input_tokens']} ↓{st['output_tokens']}  ${st['cost_usd']:.4f}",
    ]
    return Local(output="\n".join(lines))


async def _help(ctx: CommandContext, args: str) -> Local:
    """列出 REPL 命令（与补全 / --help 共用同一 registry 来源，CMD-P1）。"""
    lines = ["REPL commands:"]
    if ctx.registry is not None:
        for s in ctx.registry.specs():
            if s.is_hidden:
                continue
            left = f"  {s.name}" + (f" {s.arg_hint}" if s.arg_hint else "")
            lines.append(f"{left:<24} {s.description}")
    lines.append(f'{"  /<skill-name>":<24} Invoke a skill (e.g. /commit "fix types")')
    lines.append(f'{"  !<command>":<24} Run a shell command directly (bypasses agent + permissions)')
    return Local(output="\n".join(lines))


# ─── registry 构造 ───────────────────────────────────────────────────────────

# (name, handler, match, description, arg_hint) —— 顺序仅影响同长度 name 的稳定 tie-break；
# 真正的优先级由 Registry 按 name 长度降序保证（most-specific-first）。
_BUILTINS = [
    ("/clear", _clear, "exact", "Clear conversation history", ""),
    ("/plan", _plan, "exact", "Toggle plan mode (read-only)", ""),
    ("/cost", _cost, "exact", "Show token usage and cost", ""),
    ("/context", _context, "exact", "Show context packs, token budget & compaction survival", ""),
    ("/compact", _compact, "exact_or_prefix", "Manually compact the conversation", "[prompt]"),
    ("/memory consolidate", _memory_consolidate, "exact",
     "Run a curator pass to merge/rewrite/archive memories", ""),
    ("/memory generate", _memory_generate, "exact_or_prefix",
     "Extract long-term memory from this session (simplemem backend)", "[--force]"),
    ("/memory eval", _memory_eval, "exact_or_prefix",
     "List/confirm/reject memory eval candidates",
     "[pending|confirmed|rejected | confirm <id> | reject <id>]"),
    ("/memory", _memory, "exact", "List saved memories", ""),
    ("/skills", _skills, "exact", "List available skills", ""),
    ("/sandbox", _sandbox, "exact_or_prefix",
     "Show sandbox policy / switch profile", "[<profile>]"),
    ("/tasks", _tasks, "exact_or_prefix", "List background tasks", "[status]"),
    ("/task-stop", _task_stop, "prefix", "Stop a running background task", "<id>"),
    ("/task", _task, "prefix", "Show a background task's status & log", "<id>"),
    ("/agents", _agents, "exact_or_prefix",
     "Agent definitions + running instances", "[available|running|show <name|id>]"),
    ("/agent", _agent, "exact_or_prefix", "Show one sub-agent instance or definition", "<id|name|next|prev>"),
    ("/tree", _tree, "exact_or_prefix", "Browse the session tree (interactive), or /tree <entry> to checkout", "[entry_id]"),
    ("/new", _new, "exact", "Start a new empty session and switch to it", ""),
    ("/clone", _clone, "exact_or_prefix", "New session: copy the current branch up to the current leaf and switch (editor empty)", ""),
    ("/fork", _fork, "exact_or_prefix", "New session: copy up to BEFORE a user message and put that prompt back in the editor", "[entry_id]"),
    ("/name", _name, "exact_or_prefix", "Show or set this session's name (/name <text> | --clear)", "[text]"),
    ("/resume", _resume, "exact_or_prefix", "Browse sessions (interactive) or /resume <id> to switch", "[id]"),
    ("/session", _session, "exact", "Show current session info & stats", ""),
    ("/help", _help, "exact", "List REPL commands", ""),
]


def build_registry() -> Registry:
    r = Registry()
    for name, handler, match, desc, hint in _BUILTINS:
        r.register(Command(
            CommandSpec(name=name, kind="local", description=desc, arg_hint=hint, match=match),
            handler,
        ))
    from .extension_bridge import merge_system_extension_commands
    merge_system_extension_commands(r)
    return r
