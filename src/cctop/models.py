"""Typed domain models for the collector core.

These are plain immutable records. Every field a session can carry is spelled
out here so the presentation layer never touches a raw dict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .status import SessionStatus


@dataclass(frozen=True)
class Session:
    """One live Claude Code process, from ~/.claude/sessions/<pid>.json.

    This is the registry record verbatim (undocumented, version-internal). The
    fields are read defensively; anything missing becomes None or an empty
    string rather than raising.
    """

    pid: int
    session_id: str
    cwd: str
    name: str
    raw_status: str
    kind: str
    version: str
    started_at: datetime | None
    updated_at: datetime | None
    status_updated_at: datetime | None


@dataclass(frozen=True)
class UsageTotals:
    """Cumulative token usage and cost across a session's transcript."""

    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float | None

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )


@dataclass(frozen=True)
class ContextWindow:
    """The prompt size of the most recent turn against the model's window."""

    used_tokens: int
    window_tokens: int

    @property
    def used_fraction(self) -> float:
        if self.window_tokens <= 0:
            return 0.0
        return min(1.0, self.used_tokens / self.window_tokens)


@dataclass(frozen=True)
class SessionState:
    """A session merged with its transcript-derived model, usage, and status."""

    session: Session
    account: str
    alive: bool
    status: SessionStatus
    model: str | None
    context: ContextWindow | None
    totals: UsageTotals
    last_activity: datetime | None


@dataclass(frozen=True)
class LimitWindow:
    """One usage-limit window from GET /api/oauth/usage's `limits` array.

    The real values Claude Code's /usage shows, not estimates. `percent` is
    0..100, `resets_at` is the wall-clock reset time, `is_active` marks the
    window currently binding, and `severity` (normal/warning/...) drives color.
    """

    kind: str  # "session", "weekly_all", "weekly_scoped"
    label: str  # display label, e.g. "5h", "week (all)", "week (Fable)"
    percent: float
    resets_at: datetime | None
    severity: str
    is_active: bool
    # False when the window has no real reading yet (a fresh account that has
    # not started a window: 0% with no reset time) or the value was unusable, so
    # the UI shows "no usage yet" rather than a misleading full/empty 0% bar.
    has_data: bool = True

    @property
    def used_fraction(self) -> float:
        return max(0.0, min(1.0, self.percent / 100.0))


@dataclass(frozen=True)
class AccountLimits:
    """Subscription usage limits for one account.

    `windows` is the full set of limit windows (session, weekly-all, and any
    model-scoped weekly windows like Fable), read from GET /api/oauth/usage.
    `source` is "api" when live, or "none" with `error` set when the fetch
    could not be made, so the UI never presents an absent value as live.
    """

    account: str
    tier: str | None
    windows: list[LimitWindow]
    source: str  # "api" or "none"
    fetched_at: datetime | None
    error: str | None = None
    # A transient failure (429/5xx/network) worth backing off and retrying,
    # versus a real state (token expired, wrong credential) that will not clear
    # on its own. retry_after is the server's requested wait in seconds, if any.
    retriable: bool = False
    retry_after: float | None = None


@dataclass(frozen=True)
class FleetSnapshot:
    """An immutable point-in-time view of every live session and fleet totals."""

    taken_at: datetime
    sessions: list[SessionState]
    limits: list[AccountLimits] = field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        return sum(s.totals.cost_usd or 0.0 for s in self.sessions)

    @property
    def total_tokens(self) -> int:
        return sum(s.totals.total_tokens for s in self.sessions)
