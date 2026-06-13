"""Search, filter, and sort helpers for session selectors.

Pi keeps this logic outside the UI component.  The TUI page owns presentation
state, but token parsing and matching stay here so the behavior is testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .listing import SessionInfo

SortMode = Literal["threaded", "recent", "relevance"]
NameFilter = Literal["all", "named"]


@dataclass(frozen=True)
class QueryToken:
    kind: Literal["fuzzy", "phrase"]
    value: str


@dataclass(frozen=True)
class ParsedSearchQuery:
    mode: Literal["tokens", "regex"]
    tokens: tuple[QueryToken, ...] = ()
    regex: re.Pattern[str] | None = None
    error: str | None = None


@dataclass(frozen=True)
class MatchResult:
    matches: bool
    score: float = 0.0


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def session_search_text(session: SessionInfo) -> str:
    return " ".join((
        session.sid,
        session.name or "",
        session.first_message,
        session.all_messages_text,
        session.cwd,
        session.path,
    ))


def has_session_name(session: SessionInfo) -> bool:
    return bool((session.name or "").strip())


def parse_search_query(query: str) -> ParsedSearchQuery:
    trimmed = query.strip()
    if not trimmed:
        return ParsedSearchQuery(mode="tokens")

    if trimmed.startswith("re:"):
        pattern = trimmed[3:].strip()
        if not pattern:
            return ParsedSearchQuery(mode="regex", error="Empty regex")
        try:
            return ParsedSearchQuery(mode="regex", regex=re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            return ParsedSearchQuery(mode="regex", error=str(exc))

    tokens: list[QueryToken] = []
    buf: list[str] = []
    in_quote = False
    unclosed_quote = False

    def flush(kind: Literal["fuzzy", "phrase"]) -> None:
        value = "".join(buf).strip()
        buf.clear()
        if value:
            tokens.append(QueryToken(kind, value))

    for ch in trimmed:
        if ch == '"':
            if in_quote:
                flush("phrase")
                in_quote = False
            else:
                flush("fuzzy")
                in_quote = True
            continue
        if not in_quote and ch.isspace():
            flush("fuzzy")
            continue
        buf.append(ch)

    if in_quote:
        unclosed_quote = True

    if unclosed_quote:
        fallback = tuple(QueryToken("fuzzy", t) for t in trimmed.split() if t.strip())
        return ParsedSearchQuery(mode="tokens", tokens=fallback)

    flush("phrase" if in_quote else "fuzzy")
    return ParsedSearchQuery(mode="tokens", tokens=tuple(tokens))


def fuzzy_match(needle: str, haystack: str) -> MatchResult:
    """Small local fuzzy matcher: ordered characters, lower score is better."""

    n = needle.lower()
    h = haystack.lower()
    if not n:
        return MatchResult(True, 0)

    pos = -1
    first = -1
    gaps = 0
    for ch in n:
        nxt = h.find(ch, pos + 1)
        if nxt < 0:
            return MatchResult(False, 0)
        if first < 0:
            first = nxt
        elif nxt > pos + 1:
            gaps += nxt - pos - 1
        pos = nxt
    return MatchResult(True, first * 0.2 + gaps)


def match_session(session: SessionInfo, parsed: ParsedSearchQuery) -> MatchResult:
    text = session_search_text(session)

    if parsed.mode == "regex":
        if parsed.regex is None:
            return MatchResult(False, 0)
        match = parsed.regex.search(text)
        if match is None:
            return MatchResult(False, 0)
        return MatchResult(True, match.start() * 0.1)

    if not parsed.tokens:
        return MatchResult(True, 0)

    total = 0.0
    normalized: str | None = None
    for token in parsed.tokens:
        if token.kind == "phrase":
            normalized = normalized if normalized is not None else normalize_text(text)
            phrase = normalize_text(token.value)
            if not phrase:
                continue
            idx = normalized.find(phrase)
            if idx < 0:
                return MatchResult(False, 0)
            total += idx * 0.1
            continue

        m = fuzzy_match(token.value, text)
        if not m.matches:
            return MatchResult(False, 0)
        total += m.score

    return MatchResult(True, total)


def filter_and_sort_sessions(
    sessions: list[SessionInfo],
    query: str,
    sort_mode: SortMode,
    name_filter: NameFilter = "all",
) -> list[SessionInfo]:
    name_filtered = sessions if name_filter == "all" else [s for s in sessions if has_session_name(s)]
    trimmed = query.strip()

    if sort_mode == "threaded" and not trimmed:
        return name_filtered

    base = sorted(name_filtered, key=lambda s: s.modified, reverse=True) if sort_mode == "recent" else name_filtered
    if not trimmed:
        return base

    parsed = parse_search_query(query)
    if parsed.error:
        return []

    if sort_mode == "recent":
        return [s for s in base if match_session(s, parsed).matches]

    scored: list[tuple[SessionInfo, float]] = []
    for session in base:
        result = match_session(session, parsed)
        if result.matches:
            scored.append((session, result.score))

    scored.sort(key=lambda item: (item[1], -item[0].modified))
    return [session for session, _ in scored]
