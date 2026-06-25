"""context/providers.py — ContextProvider 协议 + 具体 provider（docs/15 §8 / §2.3）。

把今天烤进 build_system_prompt 的 10 个动态来源（cwd/date/platform/shell/git/项目指令/memory/
skills/agent 描述/deferred tools）+ backend loop 里的注入（memory recall/skill listing/task reminder）
逐个封装成 ContextProvider,产出结构化 ContextPack。每个 provider 包裹既有源逻辑（prompt.py /
memory / skills.listing / tasks.inject），不重写其内容计算——只改「如何被组装/记账/注入」。

§9.2：RepoMapProvider 也是 ContextProvider（Phase 4 落地,这里先留协议）。
provider.collect 是 async（未来 repo map / memory recall 可并发 I/O）;当前同步源直接返回。
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from .packs import ContextPack


@dataclass
class ContextRequest:
    """一次上下文组装的输入（任务/已读文件/提及符号/profile context 开关）。

    Phase 3 用 cwd + include_* 开关;Phase 4 repo map 用 files_*/mentioned_* 个性化（§9.1 RepoQuery）。
    """

    cwd: str = ""
    is_sub_agent: bool = False
    include_env: bool = True
    include_git: bool = True
    include_project_instructions: bool = True
    include_memory: bool = True
    include_skills: bool = True
    include_agents: bool = True
    include_deferred_tools: bool = True
    include_repo_map: bool = False
    # Phase 4 个性化（§9.1）
    user_prompt: str = ""                  # 当前用户输入（repo map 提及提取；纯数据，不含历史）
    files_read: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    mentioned_files: list[str] = field(default_factory=list)
    mentioned_identifiers: list[str] = field(default_factory=list)
    map_tokens: int = 1024
    context_window_tokens: int = 0         # 模型上下文窗（repo map no-files 放大的封顶基准）
    map_refresh: str = "auto"              # aider refresh 档（map 结果缓存策略）
    map_multiplier_no_files: float = 2.0
    # docs/24 Phase 4a：DeferredToolsProvider 据此读 per-agent overlay 的激活集（tool_search
    # 激活落在 agent registry 上）。None → 全局 REGISTRY（行为等价）。
    tool_registry: object | None = None


@dataclass(frozen=True)
class ContextSources:
    """Injectable context data sources owned by runtime/services."""

    git: Callable[[ContextRequest], str] | None = None
    project_instructions: Callable[[ContextRequest], str] | None = None
    memory_static: Callable[[ContextRequest], str] | None = None


@runtime_checkable
class ContextProvider(Protocol):
    id: str
    enable_attr: str
    async def collect(self, request: ContextRequest) -> "ContextPack | None": ...


# ─── 具体 provider（包裹既有源逻辑）──────────────────────────────────────────
class EnvProvider:
    id = "env"
    enable_attr = "include_env"

    async def collect(self, request):
        today = date.today().isoformat()
        plat = f"{platform.system()} {platform.machine()}"
        shell = ((os.environ.get("ComSpec") or "cmd.exe") if sys.platform == "win32"
                 else os.environ.get("SHELL", "/bin/sh"))
        content = (f"cwd: {request.cwd or os.getcwd()}\ndate: {today}\n"
                   f"platform: {plat}\nshell: {shell}")
        return ContextPack(id="env", kind="env", content=content, lifecycle="turn",
                           cache_policy="volatile_tail", persist_policy="none", priority=50,
                           provenance={"source": "EnvProvider"})


class GitSnapshotProvider:
    id = "git"
    enable_attr = "include_git"

    def __init__(self, source: Callable[[ContextRequest], str] | None = None) -> None:
        self.source = source or _default_git_source

    async def collect(self, request):
        text = self.source(request)
        if not text.strip():
            return None
        return ContextPack(id="git", kind="git", content=text, lifecycle="turn",
                           cache_policy="volatile_tail", persist_policy="none", priority=40,
                           provenance={"source": "GitSnapshotProvider"})


class ProjectInstructionsProvider:
    id = "project_instructions"
    enable_attr = "include_project_instructions"

    def __init__(self, source: Callable[[ContextRequest], str] | None = None) -> None:
        self.source = source or _default_project_instructions_source

    async def collect(self, request):
        text = self.source(request)
        if not text.strip():
            return None
        # session 生命周期 + survives compaction（§8.4：root 项目指令 compaction 后重载为 session pack）。
        return ContextPack(id="project_instructions", kind="project_instructions", content=text,
                           lifecycle="session", cache_policy="append_only", persist_policy="custom_message",
                           priority=90, provenance={"source": "ProjectInstructionsProvider"})


class MemoryStaticProvider:
    id = "memory_static"
    enable_attr = "include_memory"

    def __init__(self, source: Callable[[ContextRequest], str] | None = None) -> None:
        self.source = source or _default_memory_static_source

    async def collect(self, request):
        text = self.source(request)
        if not text.strip():
            return None
        return ContextPack(id="memory_static", kind="memory_static", content=text,
                           lifecycle="session", cache_policy="append_only", persist_policy="custom_message",
                           priority=70, provenance={"source": "MemoryStaticProvider"})


class SkillGuidanceProvider:
    id = "skills"
    enable_attr = "include_skills"

    async def collect(self, request):
        from ..skills.listing import SKILL_PROMPT_GUIDANCE
        if not SKILL_PROMPT_GUIDANCE.strip():
            return None
        return ContextPack(id="skills", kind="skill_guidance", content=SKILL_PROMPT_GUIDANCE,
                           lifecycle="session", cache_policy="stable_prefix", persist_policy="custom_message",
                           priority=60, provenance={"source": "SkillGuidanceProvider"})


class AgentDescriptionsProvider:
    id = "agents"
    enable_attr = "include_agents"

    async def collect(self, request):
        from ..agents.registry import build_agent_descriptions
        text = build_agent_descriptions()
        if not text.strip():
            return None
        return ContextPack(id="agents", kind="agent_descriptions", content=text,
                           lifecycle="session", cache_policy="append_only", persist_policy="custom_message",
                           priority=55, provenance={"source": "AgentDescriptionsProvider"})


class DeferredToolsProvider:
    id = "deferred_tools"
    enable_attr = "include_deferred_tools"

    async def collect(self, request):
        from ..tools import get_deferred_tool_names
        names = get_deferred_tool_names(registry=getattr(request, "tool_registry", None))
        if not names:
            return None
        text = ("The following deferred tools are available via tool_search: "
                f"{', '.join(names)}. Use tool_search to fetch their full schemas when needed.")
        return ContextPack(id="deferred_tools", kind="deferred_tools", content=text,
                           lifecycle="session", cache_policy="append_only", persist_policy="custom_message",
                           priority=45, provenance={"source": "DeferredToolsProvider"})


class RepoMapProvider:
    """Aider-style repo map 作 ContextProvider（§9.2）：按 RepoQuery 个性化 + 预算封顶,never 整文件。

    经 codeintel.get_service（进程级 per-root 缓存索引，跨 turn 复用——不再每次重建）。
    aider 语义：personal 文件（已读/已改）是排名**种子、不渲染**；无 personal 文件（首 turn）
    按 map_multiplier_no_files 放大。lifecycle=turn,不入树(persist=none)。
    """

    id = "repo_map"
    enable_attr = "include_repo_map"

    NO_FILES_BUDGET_MULTIPLIER = 2.0        # aider CLI --map-multiplier-no-files default
    CONTEXT_WINDOW_PADDING = 4096           # aider get_repo_map 的 padding

    async def collect(self, request):
        import os
        budget = request.map_tokens
        if budget <= 0:
            return None
        repo = self._repo_files(request.cwd or os.getcwd())
        if repo is None:
            return None
        repo_root, _tracked_files = repo
        from ..codeintel import RepoQuery, get_service
        svc = get_service(str(repo_root))
        # 提及提取（aider get_ident_mentions/get_file_mentions 同款）：当前用户输入分词 ∩
        # 已知 def 名/文件名——×10 ident 加权与文件 personalization 的主要触发器。
        m_idents, m_files = svc.extract_mentions(request.user_prompt)
        query = RepoQuery(
            files_read=request.files_read, files_modified=request.files_modified,
            mentioned_files=list(request.mentioned_files) + m_files,
            mentioned_identifiers=list(request.mentioned_identifiers) + m_idents)
        # aider get_repo_map:120-132 语义：无 chat（personal）文件即放大——mentions 不影响；
        # 须知道上下文窗才放大，且按 window − padding 封顶。
        if not (request.files_read or request.files_modified) and request.context_window_tokens:
            multiplier = request.map_multiplier_no_files or self.NO_FILES_BUDGET_MULTIPLIER
            target = min(int(budget * multiplier),
                         request.context_window_tokens - self.CONTEXT_WINDOW_PADDING)
            if target > 0:
                budget = target
        result = svc.repo_map(query, budget_tokens=budget, refresh=request.map_refresh)
        if not result.text:
            return None
        prov = {"source": "RepoMapProvider", "files": result.files}
        if result.truncated:
            prov["index_truncated"] = True              # 覆盖不全不静默（/context 可见）
        return ContextPack(id="repo_map", kind="repo_map", content=result.text, lifecycle="turn",
                           cache_policy="volatile_tail", persist_policy="none", priority=30,
                           provenance=prov)

    @staticmethod
    def _repo_files(cwd: str) -> "tuple[Path, set[Path]] | None":
        import subprocess
        try:
            top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                                 capture_output=True, timeout=10)
            if top.returncode != 0:
                return None
            root = Path(top.stdout.decode().strip()).resolve()
            files = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                                   capture_output=True, timeout=15)
            if files.returncode != 0:
                return None
        except Exception:
            return None
        tracked = {
            (root / rel).resolve()
            for rel in files.stdout.decode("utf-8", errors="replace").split("\0")
            if rel
        }
        return (root, tracked) if tracked else None

    @staticmethod
    def _personal_abs_files(repo_root: Path, request: ContextRequest) -> set[Path]:
        out: set[Path] = set()
        for name in request.files_read + request.files_modified:
            try:
                p = Path(name)
                out.add((p if p.is_absolute() else repo_root / p).resolve())
            except Exception:
                continue
        return out


def _default_git_source(request: ContextRequest) -> str:
    from ..prompt import get_git_context
    return get_git_context()


def _default_project_instructions_source(request: ContextRequest) -> str:
    from ..prompt import load_project_instructions
    return load_project_instructions()


def _default_memory_static_source(request: ContextRequest) -> str:
    return ""


def default_providers(sources: ContextSources | None = None) -> list:
    """build_system_prompt 今天烤进的 10 个动态来源,逐个 provider 化（顺序 = 优先级高→低组装）。"""
    sources = sources or ContextSources()
    return [
        ProjectInstructionsProvider(sources.project_instructions),
        MemoryStaticProvider(sources.memory_static),
        SkillGuidanceProvider(),
        AgentDescriptionsProvider(),
        EnvProvider(),
        GitSnapshotProvider(sources.git),
        DeferredToolsProvider(),
        RepoMapProvider(),
    ]


# ─── turn-loop providers（docs/16 #6：四个手写注入器 provider 化）──────────────
#
# 与上面的 async provider 不同,这些在**模型循环边界内**运行（每次请求前 / 工具后）,同步、无 I/O，
# 由 AgentSession 直接调用而非经 ContextRuntime（不参与预算 eviction——注入语义是"有则必注"，
# 真正的体积控制在各源内部,如 skill_listing_delta 的 char 预算）。共同契约：
# - collect() 纯：只读 agent 状态、产 pack，绝不写树/推进 dedup；
# - commit() 在 AgentSession **写树成功后**调用——dedup/injected 标记只在此推进
#   （docs/14 P3 review #7：写失败不得静默丢注入 + 不得误推进 dedup）。

class SkillListingProvider:
    """skill 渐进披露清单（lifecycle=until_compact：清单 entry 被压缩折叠丢弃后,
    AgentSession.compact 复位 _sent_skill_names → 下轮重新播报）。"""

    id = "skill_listing"

    def __init__(self, agent) -> None:
        self.agent = agent
        self._new_names: set = set()

    def collect(self) -> "ContextPack | None":
        from ..skills.listing import skill_listing_delta
        a = self.agent
        budget_chars = max(2000, int(a.effective_window * 0.04))
        text, new_names = skill_listing_delta(a._sent_skill_names, a._activated_path_skills, budget_chars)
        if not text:
            return None
        self._new_names = new_names
        return ContextPack(id="skill_listing", kind="skill_listing", content=text,
                           lifecycle="until_compact", cache_policy="append_only",
                           persist_policy="custom_message", priority=35,
                           provenance={"source": "SkillListingProvider",
                                       "new_skills": sorted(new_names)})

    def commit(self) -> None:
        self.agent._sent_skill_names.update(self._new_names)


class FinishedTasksProvider:
    """终态且未注入的后台任务提醒（lifecycle=turn）。commit 把任务标 injected。"""

    id = "finished_tasks"

    def __init__(self, agent) -> None:
        self.agent = agent
        self._pending: list = []

    def collect(self) -> "ContextPack | None":
        from ..tasks.inject import collect_pending_injections, render_task_reminder
        a = self.agent
        pending = collect_pending_injections(a.task_manager)
        if not pending:
            return None
        self._pending = pending
        text = "\n\n".join(render_task_reminder(t) for t in pending)
        return ContextPack(id="finished_tasks", kind="finished_tasks", content=text,
                           lifecycle="turn", cache_policy="append_only",
                           persist_policy="custom_message", priority=38,
                           provenance={"source": "FinishedTasksProvider",
                                       "task_ids": [t.id for t in pending]})

    def commit(self) -> None:
        for t in self._pending:
            self.agent.task_manager.update_task(t.id, injected=True)


class MemoryRecallProvider:
    """settled 记忆预取的注入（lifecycle=turn）。commit 推进 _already_surfaced + 字节预算。"""

    id = "memory"

    def __init__(self, agent, memories: list) -> None:
        self.agent = agent
        self.memories = memories

    def collect(self) -> "ContextPack | None":
        if not self.memories:
            return None
        from ..memory import format_memories_for_injection
        text = format_memories_for_injection(self.memories)
        return ContextPack(id="memory", kind="memory", content=text,
                           lifecycle="turn", cache_policy="append_only",
                           persist_policy="custom_message", priority=45,
                           provenance={"source": "MemoryRecallProvider",
                                       "paths": [m.path for m in self.memories]})

    def commit(self) -> None:
        a = self.agent
        for m in self.memories:
            a._already_surfaced_memories.add(m.path)
            a._session_memory_bytes += len(m.content.encode())


def skill_body_pack(name: str, body: str) -> ContextPack:
    """skill body → pack（lifecycle=one_shot：指令注入一次,失败由调用方 requeue——无 dedup 状态,
    故无 provider 类/commit）。"""
    from ..skills.listing import render_skill_body_message
    msg = render_skill_body_message(name, body)
    return ContextPack(id=f"skill_body:{name}", kind="skill_body", content=msg.get("content", ""),
                       lifecycle="one_shot", cache_policy="append_only",
                       persist_policy="custom_message", priority=80,
                       provenance={"source": "skill_body_pack", "skill": name})
