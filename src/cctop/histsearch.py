"""Search conversation history across every account, Claude and Codex alike.

The corpus is the transcript files both tools already write: Claude Code's
projects/<cwd-slug>/<session>.jsonl per account and Codex's rollout files under
sessions/ (and archived_sessions/). A ripgrep pass (see ripgrep.py) prefilters
matching lines; each line is then parsed defensively, kept only when the query
really occurs in message text (not in metadata like a sessionId or a path), and
grouped into per-session results tagged with the owning account. No index and
no cache: at ripgrep speed a fresh scan is faster than keeping an index honest
against an undocumented, version-internal format.

Read-only. Resuming a session (resume_plan) only describes the command for the
owner binary; the caller decides whether to run it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import ripgrep
from .collect import Account
from .transcript import _parse_timestamp

# How much of a transcript head to read for title/cwd metadata. Claude writes
# the ai-title record and the first user message early; the byte cap guards
# against a huge early line (file-history snapshots can be megabytes).
_HEAD_LINES = 80
_HEAD_BYTES = 512_000

# Stored snippet window around the first match in a message, so a hit on a
# 100 KB tool result does not drag the whole blob into memory or the UI.
_SNIPPET_RADIUS = 400

_ROLLOUT_ID = re.compile(r"rollout-.*-([0-9a-f-]{36})\.jsonl$")


@dataclass(frozen=True)
class SearchHit:
    """One matching message: where it is and the text around the match."""

    path: Path
    line_number: int
    role: str  # "user" or "assistant"
    snippet: str  # message text windowed around the first match
    timestamp: datetime | None


@dataclass(frozen=True)
class SessionMatch:
    """All of one session's hits, tagged with the account that owns it."""

    session_id: str
    path: Path
    account: str
    provider: str  # "claude" or "codex"
    project: str  # short cwd, or the path slug when no cwd was recorded
    title: str
    cwd: str
    last_timestamp: datetime | None
    hits: list[SearchHit]


@dataclass(frozen=True)
class SearchResult:
    """The outcome of one search: grouped sessions plus honesty metadata."""

    sessions: list[SessionMatch]
    total_hits: int
    truncated: bool
    backend: str


@dataclass(frozen=True)
class ResumePlan:
    """The command that would resume a session under its owning account.

    Claude sessions resume via `claude --resume` with CLAUDE_CONFIG_DIR pinned
    to the account, so the right subscription and credentials are used; Codex
    sessions via `codex resume`. `cwd` is the session's recorded working
    directory when it still exists (Claude Code requires resuming from it).
    """

    argv: list[str]
    env_extra: dict[str, str]
    cwd: Path | None


def _roots(accounts: list[Account]) -> list[tuple[Account, Path]]:
    """Every transcript root to scan, paired with its owning account."""
    pairs = []
    for account in accounts:
        if account.provider == "codex":
            for name in ("sessions", "archived_sessions"):
                root = account.config_dir / name
                if root.is_dir():
                    pairs.append((account, root))
        else:
            root = account.config_dir / "projects"
            if root.is_dir():
                pairs.append((account, root))
    return pairs


def _account_for(path: Path, roots: list[tuple[Account, Path]]) -> Account | None:
    for account, root in roots:
        if path.is_relative_to(root):
            return account
    return None


def _block_text(block: object) -> str:
    """The searchable text of one content block (text, thinking, tool traffic)."""
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    kind = block.get("type")
    if kind in ("text", "thinking"):
        value = block.get("text") or block.get("thinking")
        return value if isinstance(value, str) else ""
    if kind == "tool_use":
        try:
            return f"{block.get('name', '')} {json.dumps(block.get('input'))}"
        except (TypeError, ValueError):
            return str(block.get("name", ""))
    if kind == "tool_result":
        content = block.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(_block_text(inner) for inner in content)
    return ""


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(text for text in (_block_text(block) for block in content) if text)
    return ""


def _parse_claude_line(record: dict) -> tuple[str, str, datetime | None, str, str] | None:
    """(role, text, timestamp, session_id, cwd) from one Claude transcript line."""
    if record.get("type") not in ("user", "assistant") or record.get("isSidechain"):
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None

    text = _message_text(message)
    if not text:
        return None
    return (
        str(record.get("type")),
        text,
        _parse_timestamp(record.get("timestamp")),
        str(record.get("sessionId", "")),
        str(record.get("cwd", "")),
    )


def _parse_codex_line(record: dict) -> tuple[str, str, datetime | None, str, str] | None:
    """(role, text, timestamp, session_id, cwd) from one Codex rollout line."""
    if record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    if kind not in ("user_message", "agent_message"):
        return None

    text = payload.get("message")
    if not isinstance(text, str) or not text:
        return None
    role = "user" if kind == "user_message" else "assistant"
    return role, text, _parse_timestamp(record.get("timestamp")), "", ""


def _match_span(text: str, query: str, pattern: re.Pattern[str] | None) -> tuple[int, int] | None:
    """Where the query first occurs in decoded message text, or None.

    This is the post-filter: ripgrep matched the raw JSON line, which includes
    metadata (sessionId, paths, git branch), so a hit is only real if the query
    occurs in the message text itself.
    """
    if pattern is not None:
        found = pattern.search(text)
        return found.span() if found else None
    lowered = text.casefold()
    start = lowered.find(query.casefold())
    if start == -1:
        return None
    return start, start + len(query)


def _snippet(text: str, span: tuple[int, int]) -> str:
    start = max(0, span[0] - _SNIPPET_RADIUS)
    end = min(len(text), span[1] + _SNIPPET_RADIUS)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + " ".join(text[start:end].split()) + suffix


def _codex_session_id(path: Path) -> str:
    found = _ROLLOUT_ID.search(path.name)
    return found.group(1) if found else path.stem


def _slug_project(path: Path) -> str:
    """A readable project name from the transcript's parent directory slug."""
    slug = path.parent.name
    return slug.split("-")[-1] if "-" in slug else slug


def _skip_as_title(text: str) -> bool:
    """User messages that make bad titles: injected command/attachment traffic."""
    return text.startswith(("<", "Caveat:")) or not text.strip()


def _head_meta(path: Path, provider: str) -> tuple[str, str]:
    """(title, cwd) read from the head of a transcript.

    Claude writes an ai-title record and the first user message within the
    first lines of a session file, so a bounded head read finds a good title
    without parsing the whole (possibly huge) transcript.
    """
    title = ""
    first_user = ""
    cwd = ""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            consumed = 0
            for _ in range(_HEAD_LINES):
                line = handle.readline()
                consumed += len(line)
                if not line or consumed > _HEAD_BYTES:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue

                if provider == "codex":
                    if record.get("type") == "session_meta":
                        payload = record.get("payload") or {}
                        cwd = cwd or str(payload.get("cwd") or "")
                    parsed = _parse_codex_line(record)
                    if parsed and parsed[0] == "user" and not first_user:
                        first_user = parsed[1]
                    continue

                if isinstance(record.get("aiTitle"), str):
                    title = record["aiTitle"]
                if isinstance(record.get("summary"), str) and not title:
                    title = record["summary"]
                cwd = cwd or str(record.get("cwd") or "")
                if record.get("type") == "user" and not first_user:
                    text = _message_text(record.get("message") or {})
                    if not _skip_as_title(text):
                        first_user = text
    except OSError:
        pass

    chosen = title or first_user
    return " ".join(chosen.split())[:120], cwd


def _short_cwd(cwd: str) -> str:
    home = str(Path.home())
    return "~" + cwd[len(home) :] if cwd.startswith(home) else cwd


def search_history(
    query: str,
    accounts: list[Account],
    regex: bool = False,
    limit_sessions: int = 60,
    per_file_cap: int = 20,
    max_hits: int = 500,
    backend: ripgrep.Backend | None = None,
) -> SearchResult:
    """Search every account's transcripts and group the hits by session.

    Sessions are ordered by their most recent matching message, newest first,
    which is the "which conversation was that" ordering a history search wants.
    """
    query = query.strip()
    if len(query) < 2:
        return SearchResult([], 0, False, backend.name if backend else "")
    pattern = re.compile(query, re.IGNORECASE) if regex else None

    roots = _roots(accounts)
    raw, truncated, backend_name = ripgrep.search_lines(
        query,
        [root for _, root in roots],
        regex=regex,
        per_file_cap=per_file_cap,
        max_total=max_hits,
        backend=backend,
    )

    hits_by_path: dict[Path, list[SearchHit]] = {}
    meta_by_path: dict[Path, tuple[Account, str, str]] = {}  # account, session_id, cwd
    for match in raw:
        account = _account_for(match.path, roots)
        if account is None:
            continue
        try:
            record = json.loads(match.line_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        parse = _parse_codex_line if account.provider == "codex" else _parse_claude_line
        parsed = parse(record)
        if parsed is None:
            continue
        role, text, timestamp, session_id, cwd = parsed

        span = _match_span(text, query, pattern)
        if span is None:
            continue

        hit = SearchHit(match.path, match.line_number, role, _snippet(text, span), timestamp)
        hits_by_path.setdefault(match.path, []).append(hit)
        previous = meta_by_path.get(match.path)
        meta_by_path[match.path] = (
            account,
            session_id or (previous[1] if previous else ""),
            cwd or (previous[2] if previous else ""),
        )

    # Sort and cut BEFORE reading transcript heads for titles, so the per-file
    # metadata read happens only for the sessions that will actually be shown.
    epoch = datetime.min.replace(tzinfo=timezone.utc)

    def last_timestamp(hits: list[SearchHit]) -> datetime | None:
        stamps = [hit.timestamp for hit in hits if hit.timestamp is not None]
        return max(stamps) if stamps else None

    ordered = sorted(
        hits_by_path.items(),
        key=lambda item: last_timestamp(item[1]) or epoch,
        reverse=True,
    )
    if len(ordered) > limit_sessions:
        ordered, truncated = ordered[:limit_sessions], True

    sessions = []
    for path, hits in ordered:
        account, session_id, cwd = meta_by_path[path]
        title, head_cwd = _head_meta(path, account.provider)
        cwd = cwd or head_cwd
        if account.provider == "codex":
            session_id = session_id or _codex_session_id(path)
        session_id = session_id or path.stem

        sessions.append(
            SessionMatch(
                session_id=session_id,
                path=path,
                account=account.name,
                provider=account.provider,
                project=_short_cwd(cwd) if cwd else _slug_project(path),
                title=title or session_id[:8],
                cwd=cwd,
                last_timestamp=last_timestamp(hits),
                hits=hits,
            )
        )

    total_hits = sum(len(session.hits) for session in sessions)
    return SearchResult(sessions, total_hits, truncated, backend_name)


def resume_plan(match: SessionMatch, accounts: list[Account]) -> ResumePlan | None:
    """How to resume this session under the account that owns it, or None.

    None means the owner binary is missing. The plan pins CLAUDE_CONFIG_DIR so
    a cc-1 session resumes on cc-1's subscription, never the default account's:
    the multi-account guarantee the standalone history tools cannot make.
    """
    cwd = Path(match.cwd) if match.cwd and Path(match.cwd).is_dir() else None

    if match.provider == "codex":
        from shutil import which

        codex = which("codex")
        if codex is None:
            return None
        return ResumePlan([codex, "resume", match.session_id], {}, cwd)

    from .authctl import find_claude_binary

    claude = find_claude_binary()
    if claude is None:
        return None
    account = next((entry for entry in accounts if entry.name == match.account), None)
    env_extra = {"CLAUDE_CONFIG_DIR": str(account.config_dir)} if account else {}
    return ResumePlan([claude, "--resume", match.session_id], env_extra, cwd)
