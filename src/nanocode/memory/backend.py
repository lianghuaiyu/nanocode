"""记忆后端抽象：Markdown（现状）/ SimpleMem（vendored）/ Off。

retrieve 同步（SimpleMem 内部同步），调用方用 asyncio.to_thread 丢线程池；
注入数据类型复用 recall.RelevantMemory，走现有 prefetch 注入路径。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import time

from .recall import RelevantMemory, memory_age, memory_freshness_warning
from .store import list_memories, get_memory_dir
from .maintenance import _simplemem_dir


class ImportResult:
    __slots__ = ("imported", "skipped", "errors")

    def __init__(self, imported: int = 0, skipped: int = 0, errors: list[str] | None = None):
        self.imported = imported
        self.skipped = skipped
        self.errors = errors if errors is not None else []


class MemoryBackend:
    """同步检索接口。retrieve 返回 RelevantMemory 列表（可直接注入）。"""

    name: str = "base"

    def retrieve(self, query: str, *, limit: int = 5, token_budget: int = 0) -> list[RelevantMemory]:
        raise NotImplementedError

    def record_observation(self, speaker: str, content: str, timestamp: str | None = None) -> None:
        raise NotImplementedError

    def import_markdown_memories(self) -> ImportResult:
        raise NotImplementedError

    def stats(self) -> dict:
        return {"backend": self.name}


def _score_entry(query: str, entry) -> int:
    """确定性关键词打分：name x3 + description x2 + content x1（与 memory_tool 一致）。"""
    q = query.lower().split()
    if not q:
        return 0
    name = (entry.name or "").lower()
    desc = (entry.description or "").lower()
    body = (entry.content or "").lower()
    score = 0
    for w in q:
        score += 3 * name.count(w) + 2 * desc.count(w) + body.count(w)
    return score


class MarkdownMemoryBackend(MemoryBackend):
    name = "markdown"

    def retrieve(self, query: str, *, limit: int = 5, token_budget: int = 0) -> list[RelevantMemory]:
        if not query.strip():
            return []
        entries = list_memories()
        if not entries:
            return []
        scored = [(e, _score_entry(query, e)) for e in entries]
        hits = sorted((p for p in scored if p[1] > 0), key=lambda p: p[1], reverse=True)
        out: list[RelevantMemory] = []
        d = get_memory_dir()
        for e, _s in hits[:limit]:
            fp = d / e.filename
            try:
                mtime_ms = fp.stat().st_mtime * 1000
            except OSError:
                mtime_ms = time.time() * 1000
            freshness = memory_freshness_warning(mtime_ms)
            header = (
                f"{freshness}\n\nMemory: {fp}:" if freshness
                else f"Memory (saved {memory_age(mtime_ms)}): {fp}:"
            )
            out.append(RelevantMemory(
                path=str(fp), content=e.content, mtime_ms=mtime_ms, header=header,
            ))
        return out

    def record_observation(self, speaker: str, content: str, timestamp: str | None = None) -> None:
        return None  # markdown 写入走 save_memory 工具，不在本后端范围

    def import_markdown_memories(self) -> ImportResult:
        return ImportResult()  # markdown 后端自身即 markdown，无需导入

    def stats(self) -> dict:
        return {"backend": self.name, "count": len(list_memories())}


class OffMemoryBackend(MemoryBackend):
    name = "off"

    def retrieve(self, query: str, *, limit: int = 5, token_budget: int = 0) -> list[RelevantMemory]:
        return []

    def record_observation(self, speaker: str, content: str, timestamp: str | None = None) -> None:
        return None

    def import_markdown_memories(self) -> ImportResult:
        return ImportResult()

    def stats(self) -> dict:
        return {"backend": self.name}


def build_embed_callable_from_env():
    """读 NANOCODE_MEMORY_EMBED_* env。齐全返回 (embed_fn, dim)，否则 None。"""
    base = os.environ.get("NANOCODE_MEMORY_EMBED_BASE_URL")
    key = os.environ.get("NANOCODE_MEMORY_EMBED_API_KEY")
    model = os.environ.get("NANOCODE_MEMORY_EMBED_MODEL")
    raw_dim = os.environ.get("NANOCODE_MEMORY_EMBED_DIM")
    if not (base and key and model and raw_dim):
        return None
    try:
        dim = int(raw_dim)
    except (TypeError, ValueError):
        return None
    if dim <= 0:
        return None

    def embed_fn(texts: list[str]) -> list[list[float]]:
        from openai import OpenAI
        client = OpenAI(base_url=base, api_key=key)
        resp = client.embeddings.create(model=model, input=list(texts))
        return [d.embedding for d in resp.data]

    return embed_fn, dim


def build_llm_callable_from_env():
    """记忆 LLM：NANOCODE_MEMORY_LLM_* 优先，回退 OPENAI_*。不可用返回 None。"""
    key = os.environ.get("NANOCODE_MEMORY_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    base = os.environ.get("NANOCODE_MEMORY_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    model = os.environ.get("NANOCODE_MEMORY_LLM_MODEL", "gpt-4o-mini")

    def llm_fn(messages: list[dict]) -> str:
        from openai import OpenAI
        client = OpenAI(base_url=base, api_key=key) if base else OpenAI(api_key=key)
        resp = client.chat.completions.create(model=model, messages=messages, temperature=0.2)
        return resp.choices[0].message.content or ""

    return llm_fn


# 人工最终决策覆盖 #4：默认后端 = "auto"（静默降级）。
# auto = 检测到 embed env 齐全才尝试 simplemem，否则静默用 markdown（不打 warning）。
_VALID_BACKENDS = ("auto", "simplemem", "markdown", "off")


def resolve_backend_choice(cli_choice: str | None) -> str:
    """优先级 CLI > env(NANOCODE_MEMORY_BACKEND) > 默认 "auto"；非法值 → "auto"。"""
    for raw in (cli_choice, os.environ.get("NANOCODE_MEMORY_BACKEND")):
        if raw:
            v = raw.strip().lower()
            if v in _VALID_BACKENDS:
                return v
    return "auto"


class SimpleMemBackend(MemoryBackend):
    name = "simplemem"

    def __init__(self, *, system, db_path: str):
        self._system = system          # 已初始化的 SimpleMemSystem
        self._db_path = db_path

    @staticmethod
    def _silence():
        return contextlib.redirect_stdout(io.StringIO())

    def _entry_to_relevant(self, entry) -> RelevantMemory:
        content = getattr(entry, "lossless_restatement", "") or ""
        kws = getattr(entry, "keywords", None) or []
        if kws:
            content = f"{content}\n(keywords: {', '.join(kws)})"
        eid = getattr(entry, "entry_id", "") or ""
        ts = getattr(entry, "timestamp", None)
        mtime_ms = time.time() * 1000
        return RelevantMemory(
            path=f"simplemem://{eid}",
            content=content,
            mtime_ms=mtime_ms,
            header=f"Memory (SimpleMem){f', {ts}' if ts else ''}: ",
        )

    def retrieve(self, query: str, *, limit: int = 5, token_budget: int = 0) -> list[RelevantMemory]:
        if not query.strip():
            return []
        try:
            with self._silence():
                entries = self._system.hybrid_retriever.retrieve(query)
        except Exception:
            return []
        return [self._entry_to_relevant(e) for e in (entries or [])[:limit]]

    def record_observation(self, speaker: str, content: str, timestamp: str | None = None) -> None:
        with self._silence():
            self._system.add_dialogue(speaker, content, timestamp)
            self._system.finalize()

    def import_markdown_memories(self) -> ImportResult:
        return import_markdown_into_simplemem(self, self._db_path)  # 见 Task 5

    def stats(self) -> dict:
        try:
            with self._silence():
                mems = self._system.get_all_memories()
            count = len(mems)
        except Exception:
            count = -1
        return {"backend": self.name, "count": count, "db_path": self._db_path}


def _imported_hashes_path():
    return _simplemem_dir() / "imported_hashes.json"


def _load_imported_hashes() -> dict:
    p = _imported_hashes_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_imported_hashes(data: dict) -> None:
    p = _imported_hashes_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(p)


def import_markdown_into_simplemem(backend, db_path: str) -> ImportResult:
    """扫 markdown 记忆按 sha256 去重，未导入/已变的写进 SimpleMem。幂等。"""
    result = ImportResult()
    d = get_memory_dir()
    seen = _load_imported_hashes()
    changed = False
    for f in sorted(d.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        try:
            raw = f.read_bytes()
        except OSError as e:
            result.errors.append(f"{f.name}: {e}")
            continue
        h = hashlib.sha256(raw).hexdigest()
        if seen.get(f.name) == h:
            result.skipped += 1
            continue
        try:
            body = raw.decode("utf-8", errors="replace")
            backend.record_observation("memory", body, None)
            seen[f.name] = h
            changed = True
            result.imported += 1
        except Exception as e:
            result.errors.append(f"{f.name}: {e}")
    if changed:
        _save_imported_hashes(seen)
    return result


def create_simplemem_backend() -> "SimpleMemBackend":
    """组装 embed+llm callable，初始化 vendored SimpleMem(text)。任一前置缺失则 raise。"""
    embed = build_embed_callable_from_env()
    if embed is None:
        raise RuntimeError(
            "SimpleMem requires an embeddings endpoint. Set "
            "NANOCODE_MEMORY_EMBED_BASE_URL / _API_KEY / _MODEL / _DIM."
        )
    embed_fn, dim = embed
    llm = build_llm_callable_from_env()
    if llm is None:
        raise RuntimeError(
            "SimpleMem requires an LLM endpoint. Set NANOCODE_MEMORY_LLM_API_KEY "
            "(or OPENAI_API_KEY)."
        )
    db_path = str(_simplemem_dir() / "text_index")
    from .._vendor import simplemem
    with contextlib.redirect_stdout(io.StringIO()):
        system = simplemem.create(
            mode="text",
            db_path=db_path,
            llm_callable=llm,
            embed_callable=embed_fn,
            embed_dimension=dim,
            enable_planning=False,     # decision 4：热路径不额外调 LLM
            enable_reflection=False,
        )
    return SimpleMemBackend(system=system, db_path=db_path)


def select_backend(cli_choice: str | None, *, on_warning=None) -> MemoryBackend:
    """按 CLI>env>默认(auto) 选后端。

    人工最终决策覆盖 #4：
    - "off"      → OffMemoryBackend
    - "markdown" → MarkdownMemoryBackend
    - "simplemem"（显式）→ 尝试 create_simplemem_backend；失败降级 markdown + warning
                  （用户明确要了才警告）。
    - "auto"（默认）→ 检测到 embed env 齐全才尝试 simplemem；否则静默 markdown（不打 warning）。
                  尝试失败也静默降级 markdown（auto 不 warning）。
    """
    choice = resolve_backend_choice(cli_choice)
    if choice == "off":
        return OffMemoryBackend()
    if choice == "markdown":
        return MarkdownMemoryBackend()
    if choice == "auto":
        # 静默：仅在 embed env 齐全时尝试 simplemem，失败也不 warning。
        if build_embed_callable_from_env() is None:
            return MarkdownMemoryBackend()
        try:
            return create_simplemem_backend()
        except Exception:
            return MarkdownMemoryBackend()
    # 显式 simplemem：失败降级 markdown + warning
    try:
        return create_simplemem_backend()
    except Exception as e:
        if on_warning is not None:
            on_warning(
                f"SimpleMem backend unavailable ({e}); falling back to markdown memory."
            )
        return MarkdownMemoryBackend()
