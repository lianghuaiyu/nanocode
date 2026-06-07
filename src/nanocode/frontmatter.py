"""Shared frontmatter parser for memory, skills, and subagent files.
Lenient YAML (PyYAML) with auto-quote + flat fallback so globs / corrupt or
truncated blocks never crash callers. Public shape is stable across consumers."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class FrontmatterResult:
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""


_PROBLEM_START = ("*", "&", "!", "%", "@", "`", "[", "{")


def _split_block(content: str):
    """Return (block_str, body_str) or None if no closed `---` frontmatter."""
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:]).strip()
    return None


def _quote_problematic_values(block: str) -> str:
    """给会让 strict YAML 报错的裸值(glob/特殊字符开头、含未引用 : 或 #)自动加引号。"""
    out = []
    for line in block.split("\n"):
        m = re.match(r"^(\s*[^:\s][^:]*:)\s+(\S.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            quoted = val[:1] in ("'", '"')
            risky = val[:1] in _PROBLEM_START or ":" in val or "#" in val
            if risky and not quoted:
                out.append(f'{key} "{val.replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"')
                continue
        out.append(line)
    return "\n".join(out)


def _flat_parse(block: str) -> dict:
    """旧扁平解析器：最后兜底，绝不抛。"""
    meta: dict[str, Any] = {}
    for line in block.split("\n"):
        idx = line.find(":")
        if idx == -1:
            continue
        key = line[:idx].strip()
        if key:
            meta[key] = line[idx + 1:].strip()
    return meta


def parse_frontmatter(content: str) -> FrontmatterResult:
    split = _split_block(content)
    if split is None:
        return FrontmatterResult(body=content)
    block, body = split

    meta: Any = None
    for candidate in (block, None):
        text = block if candidate is block else _quote_problematic_values(block)
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError:
            loaded = None
        if isinstance(loaded, dict):
            meta = loaded
            break
    if meta is None:
        meta = _flat_parse(block)
    return FrontmatterResult(meta=meta, body=body)


def format_frontmatter(meta: dict, body: str) -> str:
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


def as_list(value) -> list[str] | None:
    """把 frontmatter 值归一化为字符串 list：list 直用；逗号串拆分(容忍 [a,b])；空/None→None。"""
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1]
        return [p.strip() for p in s.split(",") if p.strip()] or None
    return [str(value).strip()]
