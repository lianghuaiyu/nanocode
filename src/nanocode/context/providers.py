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
from typing import Protocol, runtime_checkable

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
    files_read: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    mentioned_files: list[str] = field(default_factory=list)
    mentioned_identifiers: list[str] = field(default_factory=list)
    repo_map_budget_tokens: int = 1024


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

    async def collect(self, request):
        from ..prompt import get_git_context
        text = get_git_context()
        if not text.strip():
            return None
        return ContextPack(id="git", kind="git", content=text, lifecycle="turn",
                           cache_policy="volatile_tail", persist_policy="none", priority=40,
                           provenance={"source": "GitSnapshotProvider"})


class ProjectInstructionsProvider:
    id = "project_instructions"
    enable_attr = "include_project_instructions"

    async def collect(self, request):
        from ..prompt import load_project_instructions
        text = load_project_instructions()
        if not text.strip():
            return None
        # session 生命周期 + survives compaction（§8.4：root 项目指令 compaction 后重载为 session pack）。
        return ContextPack(id="project_instructions", kind="project_instructions", content=text,
                           lifecycle="session", cache_policy="append_only", persist_policy="custom_message",
                           priority=90, provenance={"source": "ProjectInstructionsProvider"})


class MemoryStaticProvider:
    id = "memory_static"
    enable_attr = "include_memory"

    async def collect(self, request):
        from ..memory import build_memory_prompt_section
        text = build_memory_prompt_section()
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
        from ..subagents import build_agent_descriptions
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
        names = get_deferred_tool_names()
        if not names:
            return None
        text = ("The following deferred tools are available via tool_search: "
                f"{', '.join(names)}. Use tool_search to fetch their full schemas when needed.")
        return ContextPack(id="deferred_tools", kind="deferred_tools", content=text,
                           lifecycle="session", cache_policy="append_only", persist_policy="custom_message",
                           priority=45, provenance={"source": "DeferredToolsProvider"})


class RepoMapProvider:
    """Aider-style repo map 作 ContextProvider（§9.2）：按 RepoQuery 个性化 + 预算封顶,never 整文件。

    已读/已改/提及文件优先;若都空则有界扫 repo(首回合也给结构)。词法 fallback(无 tree-sitter)。
    profile 关 codeintel(include_repo_map=False) → 跳过。lifecycle=turn,不入树(persist=none)。
    """

    id = "repo_map"
    enable_attr = "include_repo_map"

    async def collect(self, request):
        import os
        from ..codeintel.index import RepoIndex, RepoQuery
        idx = RepoIndex(request.cwd or os.getcwd())
        touched = list(request.files_read) + list(request.files_modified) + list(request.mentioned_files)
        if touched:
            idx.update(touched)
        else:
            idx.scan_repo()
        ranked = idx.rank(RepoQuery(
            files_read=request.files_read, files_modified=request.files_modified,
            mentioned_files=request.mentioned_files, mentioned_identifiers=request.mentioned_identifiers))
        if not ranked:
            return None
        text = idx.render(ranked, budget_tokens=request.repo_map_budget_tokens)
        return ContextPack(id="repo_map", kind="repo_map", content=text, lifecycle="turn",
                           cache_policy="volatile_tail", persist_policy="none", priority=30,
                           provenance={"source": "RepoMapProvider"})


def default_providers() -> list:
    """build_system_prompt 今天烤进的 10 个动态来源,逐个 provider 化（顺序 = 优先级高→低组装）。"""
    return [
        ProjectInstructionsProvider(),
        MemoryStaticProvider(),
        SkillGuidanceProvider(),
        AgentDescriptionsProvider(),
        EnvProvider(),
        GitSnapshotProvider(),
        DeferredToolsProvider(),
        RepoMapProvider(),
    ]
