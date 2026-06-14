"""Shared frontmatter parser for memory, skills, and subagent files.
Lenient YAML (PyYAML) with auto-quote + flat fallback so globs / corrupt or
truncated blocks never crash callers. Public shape is stable across consumers."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # optional for lightweight embedded imports
    yaml = None


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


def _strip_scalar_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    if value in ("null", "Null", "none", "None", "~"):
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    return _strip_scalar_quotes(value)


def _split_key_value(text: str) -> tuple[str, str] | None:
    key, sep, value = text.partition(":")
    if not sep:
        return None
    return key.strip(), value.strip()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_map(lines: list[str], start: int, indent: int) -> tuple[dict, int]:
    out: dict[str, Any] = {}
    i = start
    while i < len(lines):
        raw = lines[i]
        if not raw.strip():
            i += 1
            continue
        cur = _indent(raw)
        if cur < indent:
            break
        if cur > indent:
            i += 1
            continue
        pair = _split_key_value(raw.strip())
        if pair is None:
            i += 1
            continue
        key, value = pair
        if value:
            out[key] = _parse_scalar(value)
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines) or _indent(lines[j]) <= cur:
            out[key] = ""
            i += 1
            continue
        child_indent = _indent(lines[j])
        if lines[j].lstrip().startswith("- "):
            out[key], i = _parse_list(lines, j, child_indent)
        else:
            out[key], i = _parse_map(lines, j, child_indent)
    return out, i


def _parse_list(lines: list[str], start: int, indent: int) -> tuple[list, int]:
    out: list[Any] = []
    i = start
    while i < len(lines):
        raw = lines[i]
        if not raw.strip():
            i += 1
            continue
        cur = _indent(raw)
        if cur < indent:
            break
        if cur != indent or not raw.lstrip().startswith("- "):
            break
        item = raw.lstrip()[2:].strip()
        pair = _split_key_value(item)
        if pair is None:
            out.append(_parse_scalar(item))
            i += 1
            continue
        key, value = pair
        obj: dict[str, Any] = {key: _parse_scalar(value) if value else ""}
        i += 1
        while i < len(lines):
            nxt = lines[i]
            if not nxt.strip():
                i += 1
                continue
            nxt_indent = _indent(nxt)
            if nxt_indent <= cur:
                break
            pair2 = _split_key_value(nxt.strip())
            if pair2 is None:
                i += 1
                continue
            k2, v2 = pair2
            if v2:
                obj[k2] = _parse_scalar(v2)
                i += 1
                continue
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and _indent(lines[j]) > nxt_indent and lines[j].lstrip().startswith("- "):
                obj[k2], i = _parse_list(lines, j, _indent(lines[j]))
            elif j < len(lines) and _indent(lines[j]) > nxt_indent:
                obj[k2], i = _parse_map(lines, j, _indent(lines[j]))
            else:
                obj[k2] = ""
                i += 1
        out.append(obj)
    return out, i


def _flat_parse(block: str) -> dict:
    """Small YAML subset parser used when PyYAML is unavailable or rejects input."""
    lines = [line.rstrip() for line in block.split("\n") if line.strip() and not line.lstrip().startswith("#")]
    meta, _ = _parse_map(lines, 0, 0)
    return meta


def parse_frontmatter(content: str) -> FrontmatterResult:
    split = _split_block(content)
    if split is None:
        return FrontmatterResult(body=content)
    block, body = split

    meta: Any = None
    for candidate in (block, None):
        text = block if candidate is block else _quote_problematic_values(block)
        if yaml is None:
            loaded = None
        else:
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
