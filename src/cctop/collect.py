"""Assemble a FleetSnapshot across one or more accounts.

Each account is a (name, config_dir) pair: cc-0 -> ~/.claude, cc-1 ->
~/.claude-1, matching the user's shell aliases. The registry gives each
account's live process table and status; the transcript gives model, tokens,
cost, and context size; limits.py supplies the real usage-limit gauges. All of
it is combined into immutable records tagged by account.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import (
    AccountLimits,
    ContextWindow,
    FleetSnapshot,
    Session,
    SessionState,
    UsageTotals,
)
from .registry import process_alive, read_registry
from .status import SessionStatus, normalize_status
from .transcript import TranscriptTailer, find_transcript

# A session whose status has not been updated in this long is treated as stale
# even if the process is technically alive, so a wedged session reads as such.
STALE_AFTER = timedelta(minutes=10)

_EMPTY_TOTALS = UsageTotals(
    input_tokens=0,
    output_tokens=0,
    cache_creation_tokens=0,
    cache_read_tokens=0,
    cost_usd=0.0,
)


@dataclass(frozen=True)
class Account:
    """A named account backed by its own config directory.

    `provider` selects how sessions and limits are read: "claude" (the default)
    or "codex".
    """

    name: str
    config_dir: Path
    provider: str = "claude"


def default_config_dir() -> Path:
    """Where Claude Code keeps its state: CLAUDE_CONFIG_DIR or ~/.claude."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude"


def _looks_like_config_dir(path: Path) -> bool:
    """Whether a directory is a Claude Code config dir (holds its state)."""
    return (
        (path / "sessions").is_dir()
        or (path / "projects").is_dir()
        or (path / ".claude.json").exists()
    )


def discover_accounts() -> list[Account]:
    """Every Claude Code account under the home dir, labeled cc-<N>.

    ~/.claude is cc-0; ~/.claude-<N> is cc-<N>, matching the `cc-N` shell
    aliases. Discovering by glob means a newly added account is picked up
    automatically, with no hardcoded list to maintain.
    """
    home = Path.home()
    found: list[tuple[int, Account]] = []

    base = home / ".claude"
    if _looks_like_config_dir(base):
        found.append((0, Account("cc-0", base)))

    for path in home.glob(".claude-*"):
        suffix = path.name[len(".claude-") :]
        if path.is_dir() and suffix.isdigit() and _looks_like_config_dir(path):
            found.append((int(suffix), Account(f"cc-{suffix}", path)))

    found.sort(key=lambda item: item[0])
    accounts = [account for _, account in found] or [Account("default", default_config_dir())]

    codex_dir = home / ".codex"
    if (codex_dir / "sessions").is_dir() or (codex_dir / "auth.json").exists():
        accounts.append(Account("cx-0", codex_dir, provider="codex"))

    return accounts


# Backwards-compatible alias for the discovery entry point.
default_accounts = discover_accounts


def _effective_status(
    session: Session,
    alive: bool,
    now: datetime,
) -> SessionStatus:
    """Resolve the displayed status from liveness, staleness, and raw string."""
    if not alive:
        return SessionStatus.DEAD
    if session.status_updated_at is not None:
        if now - session.status_updated_at > STALE_AFTER:
            return SessionStatus.STALE
    return normalize_status(session.raw_status)


def _state_for_session(
    session: Session,
    account: Account,
    now: datetime,
) -> SessionState:
    alive = process_alive(session.pid)
    status = _effective_status(session, alive, now)

    transcript_path = find_transcript(account.config_dir, session.session_id)
    if transcript_path is None:
        return SessionState(
            session=session,
            account=account.name,
            alive=alive,
            status=status,
            model=None,
            context=None,
            totals=_EMPTY_TOTALS,
            last_activity=session.updated_at,
        )

    tailer = TranscriptTailer(transcript_path)
    tailer.poll()

    return SessionState(
        session=session,
        account=account.name,
        alive=alive,
        status=status,
        model=tailer.model,
        context=tailer.context,
        totals=tailer.totals(),
        last_activity=tailer.last_activity or session.updated_at,
    )


def codex_session_states(account: Account, now: datetime) -> list[SessionState]:
    """Adapt live Codex sessions into the shared SessionState shape."""
    from . import codex

    states = []
    for codex_session in codex.discover_sessions():
        recent = codex_session.last_activity is not None and (
            now - codex_session.last_activity < timedelta(minutes=2)
        )
        status = SessionStatus.GENERATING if recent else SessionStatus.IDLE
        session = Session(
            pid=codex_session.pid,
            session_id=codex_session.session_id,
            cwd=codex_session.cwd,
            name=codex_session.name,
            raw_status="",
            kind="codex",
            version="",
            started_at=None,
            updated_at=codex_session.last_activity,
            status_updated_at=codex_session.last_activity,
        )
        context = (
            ContextWindow(codex_session.context_used, codex_session.context_window)
            if codex_session.context_window
            else None
        )
        # Codex is a subscription (no per-token dollar cost): totals carry the
        # cumulative token count with cost left as None.
        totals = UsageTotals(codex_session.total_tokens, 0, 0, 0, None)
        states.append(
            SessionState(
                session=session,
                account=account.name,
                alive=True,
                status=status,
                model=codex_session.model,
                context=context,
                totals=totals,
                last_activity=codex_session.last_activity,
            )
        )
    return states


def account_limits(account: Account) -> AccountLimits:
    """Fetch an account's usage limits via the right provider (free)."""
    if account.provider == "codex":
        from . import codex_usage

        return codex_usage.fetch_account_limits(account.name)

    from . import usage

    return usage.fetch_account_limits(account.name, account.config_dir)


def build_snapshot(
    accounts: list[Account],
    now: datetime | None = None,
    include_dead: bool = False,
    with_limits: bool = True,
) -> FleetSnapshot:
    """Merge every account's registry, transcripts, and limits into a snapshot.

    Dead sessions (stale registry files whose process has exited) are dropped by
    default so the table shows only what is actually running. When `with_limits`
    is set, each account's usage windows are fetched live from GET
    /api/oauth/usage (free; the endpoint Claude Code's /usage uses).
    """
    now = now or datetime.now(timezone.utc)

    limits: list[AccountLimits] = []
    if with_limits:
        limits = [account_limits(account) for account in accounts]

    states: list[SessionState] = []
    for account in accounts:
        if account.provider == "codex":
            states.extend(codex_session_states(account, now))
            continue
        for session in read_registry(account.config_dir):
            states.append(_state_for_session(session, account, now))

    if not include_dead:
        states = [state for state in states if state.alive]

    states.sort(key=lambda state: state.last_activity or now, reverse=True)

    return FleetSnapshot(taken_at=now, sessions=states, limits=limits)
