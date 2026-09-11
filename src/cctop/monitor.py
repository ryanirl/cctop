"""Stateful collector for the live TUI.

Unlike build_snapshot (which reads each transcript from scratch, fine for a
one-shot), the monitor keeps a TranscriptTailer alive per session so each poll
reads only newly appended bytes. That is what makes a 1-second refresh cheap
even when transcripts are hundreds of megabytes. It also throttles the usage
fetch so the free-but-networked limits call runs on its own slower cadence.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from . import authctl, statusline
from .authctl import RefreshResult
from .collect import Account, _effective_status, account_limits, codex_session_states
from .models import AccountLimits, FleetSnapshot, SessionState, UsageTotals
from .registry import process_alive, read_registry
from .transcript import TranscriptTailer, find_transcript

# Usage windows move slowly (a 5h window shifts well under 1%/min, weekly ones
# barely at all), so poll a few minutes apart rather than every minute: fresh
# enough, and far below whatever rate the usage endpoint enforces.
DEFAULT_LIMITS_INTERVAL = timedelta(seconds=180)
# Codex discovery shells out to ps/lsof; poll it every few seconds rather than
# every tick so it stays cheap and doesn't make the terminal title flicker.
CODEX_INTERVAL = timedelta(seconds=5)
# Per-account backoff after a transient usage failure (429/5xx/network): start
# here, double each consecutive failure, cap so it always recovers eventually.
_BACKOFF_BASE = timedelta(seconds=60)
_BACKOFF_CAP = timedelta(minutes=15)
# After a delegated token refresh fails (the refresh token itself is dead and
# only /login can fix it), wait this long before letting another automatic
# attempt spawn the owner binary again.
_AUTH_RETRY = timedelta(minutes=30)
# Claude Code refreshes on its own schedule and may judge a token inside our
# proactive margin "already valid" (observed live at 1.2h remaining). Space
# proactive re-attempts out after such a no-op instead of spawning the binary
# every poll; the reactive 401 path stays armed the whole time.
_PROACTIVE_NOOP_RETRY = timedelta(minutes=5)

_EMPTY_TOTALS = UsageTotals(0, 0, 0, 0, 0.0)


class FleetMonitor:
    """Holds per-session tailers and cached limits between polls."""

    def __init__(
        self,
        accounts: list[Account],
        limits_interval: timedelta = DEFAULT_LIMITS_INTERVAL,
        auto_refresh_tokens: bool = True,
    ) -> None:
        self.accounts = accounts
        self.limits_interval = limits_interval
        self.auto_refresh_tokens = auto_refresh_tokens

        self._tailers: dict[str, TranscriptTailer] = {}
        self.limits: list[AccountLimits] = []
        self.limits_fetched_at: datetime | None = None
        self._codex_cache: dict[str, list[SessionState]] = {}
        self._codex_polled_at: datetime | None = None

        # Per-account rate-limit state: the last successful fetch (so a transient
        # failure can keep showing real numbers), when the account may be fetched
        # again, and the current backoff length.
        self._good_limits: dict[str, AccountLimits] = {}
        self._cooldown_until: dict[str, datetime] = {}
        self._backoff: dict[str, timedelta] = {}

        # Automatic token-refresh state: when an account may attempt another
        # delegated refresh after a failure, the expiry reading recorded at
        # that failure (a changed reading means the user logged in, which
        # clears the failed state by itself), and one-line notices for the UI.
        self._auth_cooldown_until: dict[str, datetime] = {}
        self._auth_failed_expiry: dict[str, datetime | None] = {}
        self._proactive_noop_until: dict[str, datetime] = {}
        self._auth_notices: list[str] = []

    def _tailer_for(self, config_dir, session_id: str) -> TranscriptTailer | None:
        """The live tailer for a session, created (and its file located) once."""
        tailer = self._tailers.get(session_id)
        if tailer is None:
            path = find_transcript(config_dir, session_id)
            if path is None:
                return None
            tailer = TranscriptTailer(path)
            self._tailers[session_id] = tailer
        return tailer

    def poll_sessions(self, now: datetime | None = None) -> list[SessionState]:
        """Rebuild the live session list, advancing each tailer by new bytes."""
        now = now or datetime.now(timezone.utc)

        codex_due = self._codex_polled_at is None or now - self._codex_polled_at >= CODEX_INTERVAL

        states: list[SessionState] = []
        seen: set[str] = set()
        for account in self.accounts:
            if account.provider == "codex":
                if codex_due:
                    self._codex_cache[account.name] = codex_session_states(account, now)
                states.extend(self._codex_cache.get(account.name, []))
                continue
            for session in read_registry(account.config_dir):
                if not process_alive(session.pid):
                    continue
                seen.add(session.session_id)
                status = _effective_status(session, True, now)

                tailer = self._tailer_for(account.config_dir, session.session_id)
                if tailer is None:
                    states.append(
                        SessionState(
                            session=session,
                            account=account.name,
                            alive=True,
                            status=status,
                            model=None,
                            context=None,
                            totals=_EMPTY_TOTALS,
                            last_activity=session.updated_at,
                        )
                    )
                    continue

                tailer.poll()
                states.append(
                    SessionState(
                        session=session,
                        account=account.name,
                        alive=True,
                        status=status,
                        model=tailer.model,
                        context=tailer.context,
                        totals=tailer.totals(),
                        last_activity=tailer.last_activity or session.updated_at,
                    )
                )

        if codex_due:
            self._codex_polled_at = now

        # Drop tailers for sessions that have exited, so state stays bounded.
        for session_id in list(self._tailers):
            if session_id not in seen:
                del self._tailers[session_id]

        states.sort(key=lambda state: state.last_activity or now, reverse=True)
        return states

    def limits_due(self, now: datetime) -> bool:
        if self.limits_fetched_at is None:
            return True
        return now - self.limits_fetched_at >= self.limits_interval

    # -- automatic token refresh (delegated to the owner binary) ---------------

    def _auth_blocked(self, account: Account, now: datetime) -> bool:
        """Whether automatic refresh must not attempt for this account now.

        Blocked when the feature is off, the provider has no delegated refresh
        (Codex refreshes on its own use), a failed attempt is cooling down, or
        the token is the same one a refresh already failed on: that state only
        a /login can change, and a changed expiry reading is how we see it did.
        """
        if not self.auto_refresh_tokens or account.provider != "claude":
            return True

        name = account.name
        stored = self._auth_failed_expiry.get(name)
        if stored is not None:
            if authctl.read_expiry(account.config_dir) == stored:
                return True
            # The stored expiry changed: a real /login happened; start fresh.
            del self._auth_failed_expiry[name]
            self._auth_cooldown_until.pop(name, None)
        # A None reading at failure time cannot identify the token, so the
        # cooldown below is what bounds repeat attempts in that case.

        cooldown = self._auth_cooldown_until.get(name)
        return cooldown is not None and now < cooldown

    def _record_auth_attempt(self, account: Account, result: RefreshResult, now: datetime) -> bool:
        """Book-keep one delegated refresh; True when the token is now valid."""
        if result.ok:
            if result.message == "refreshed":
                self._auth_notices.append(f"{account.name}: token auto-refreshed")
            return True

        self._auth_cooldown_until[account.name] = now + _AUTH_RETRY
        self._auth_failed_expiry[account.name] = authctl.read_expiry(account.config_dir)
        self._auth_notices.append(f"{account.name}: token needs /login")
        return False

    def _refresh_token_proactively(self, account: Account, now: datetime) -> None:
        """Renew a near-expiry token before fetching, so 401s never happen."""
        if self._auth_blocked(account, now):
            return
        noop_until = self._proactive_noop_until.get(account.name)
        if noop_until is not None and now < noop_until:
            return

        attempt = authctl.ensure_fresh(account.name, account.config_dir, now)
        if attempt is None:
            return
        if self._record_auth_attempt(account, attempt, now) and attempt.message == "already valid":
            # The owner binary judged the token still fine (its refresh margin
            # is tighter than ours): re-check later rather than every poll.
            self._proactive_noop_until[account.name] = now + _PROACTIVE_NOOP_RETRY

    def _refresh_token_reactively(self, account: Account, now: datetime) -> bool:
        """After a 401: one delegated refresh; True when a refetch is worth it.

        The 401 itself is the evidence the token lapsed (covers the race where
        it expired between the proactive check and the GET, and stores whose
        expiry is unreadable), so no expiry margin applies here.
        """
        if self._auth_blocked(account, now):
            return False
        attempt = authctl.refresh(account.name, account.config_dir, now)
        return self._record_auth_attempt(account, attempt, now)

    def pop_auth_notices(self) -> list[str]:
        """Drain the one-line refresh notices for the UI to toast."""
        notices, self._auth_notices = self._auth_notices, []
        return notices

    # -- limits fetching -------------------------------------------------------

    def _fetch_limits_one(self, account: Account, now: datetime) -> AccountLimits:
        """Fetch one account's limits with rate-limit backoff.

        While an account is in cooldown after a transient failure, its last good
        result is returned unchanged (gauges persist, only the "N ago" age
        grows) rather than re-hitting the endpoint. A fresh success clears the
        backoff; a fresh transient failure lengthens it (or honors Retry-After);
        a real error (token expired) is surfaced as-is.
        """
        name = account.name
        recorded = self._statusline_limits(account, now)
        if statusline.is_fresh(recorded, now):
            # A session on this account reported its windows moments ago: that
            # is the freshest possible reading, and it cost no request.
            self._good_limits[name] = recorded  # type: ignore[assignment]
            self._backoff.pop(name, None)
            self._cooldown_until.pop(name, None)
            return recorded  # type: ignore[return-value]

        if account.provider == "claude" and authctl.is_long_lived_token(account.config_dir):
            # The usage endpoint refuses setup-token logins outright (a 429 with
            # an hour-long retry-after from first use), so fetching is pointless:
            # show the last statusline reading, or say what would fix it.
            if recorded is not None:
                return recorded
            return AccountLimits(
                name,
                None,
                [],
                "none",
                None,
                error="long-lived token: run `cctop statusline install`",
            )

        cooldown = self._cooldown_until.get(name)
        good = self._good_limits.get(name)
        if cooldown is not None and now < cooldown:
            if good is not None:
                return good
            if recorded is not None:
                return recorded
            return AccountLimits(
                name, None, [], "none", None, error="rate limited, retrying", retriable=True
            )

        self._refresh_token_proactively(account, now)
        result = account_limits(account)
        if result.source != "api" and result.auth_expired:
            # The reactive safety net: refresh via the owner binary and refetch
            # in the same cycle, so the expired state is never rendered while
            # the refresh path works.
            if self._refresh_token_reactively(account, now):
                result = account_limits(account)

        if result.source == "api":
            self._good_limits[name] = result
            self._backoff.pop(name, None)
            self._cooldown_until.pop(name, None)
            return result

        if result.retriable:
            if result.retry_after is not None:
                delay = timedelta(seconds=result.retry_after)
            else:
                previous = self._backoff.get(name)
                delay = _BACKOFF_BASE if previous is None else min(previous * 2, _BACKOFF_CAP)
            self._backoff[name] = delay
            self._cooldown_until[name] = now + delay
            if good is not None:
                return good
            return recorded if recorded is not None else result

        # Non-retriable (token expired, wrong credential): a real, actionable
        # state. Drop any stale-good so the UI shows what the user must fix.
        self._good_limits.pop(name, None)
        if recorded is not None and not result.auth_expired:
            return recorded
        if result.auth_expired and name in self._auth_failed_expiry:
            # Auto-refresh already tried and failed: the refresh token itself
            # is dead, so say the one thing that actually fixes it.
            result = replace(result, error=f"needs /login - open {name} and run /login")
        return result

    def _statusline_limits(self, account: Account, now: datetime) -> AccountLimits | None:
        """The account's last statusline-reported windows, with its identity."""
        if account.provider != "claude":
            return None
        from .usage import oauth_account

        identity = oauth_account(account.config_dir)
        email = identity.get("emailAddress")
        tier = identity.get("organizationRateLimitTier")
        return statusline.limits_from_record(
            account.name,
            account.config_dir,
            now,
            tier=tier if isinstance(tier, str) else None,
            email=email if isinstance(email, str) else None,
        )

    def poll_limits(
        self,
        now: datetime | None = None,
        force: bool = False,
    ) -> list[AccountLimits]:
        """Refetch usage limits if the throttle has elapsed (or force is set).

        Networked and free (GET /api/oauth/usage). Returns the cached limits
        untouched when it is not yet due, so callers can render every tick
        without spending a request. Per-account backoff still applies even when
        forced, so a forced refresh never hammers a rate-limited account.
        """
        now = now or datetime.now(timezone.utc)
        if not force and not self.limits_due(now):
            return self.limits

        self.limits = [self._fetch_limits_one(account, now) for account in self.accounts]
        self.limits_fetched_at = now
        return self.limits

    def force_refresh_limits(self, now: datetime | None = None) -> list[AccountLimits]:
        """User-initiated hard refresh: clear per-account backoff and refetch now.

        Unlike the periodic poll (which honors cooldowns), an explicit refresh
        clears them so a rate-limited account is retried immediately; if it 429s
        again, backoff simply re-engages and the last-good numbers stay shown.
        """
        now = now or datetime.now(timezone.utc)
        self._cooldown_until.clear()
        self._backoff.clear()
        return self.poll_limits(now, force=True)

    def refresh_credentials(
        self, now: datetime | None = None
    ) -> tuple[list[RefreshResult], list[AccountLimits]]:
        """Delegate a token refresh to the owner binary for each Claude account.

        cctop writes no credential itself: authctl.refresh runs `claude auth
        status`, whose startup path renews and persists the token. Codex is
        skipped (its token store is separate). Limits are refetched through the
        backoff-aware path so a token refresh re-checks a formerly expired
        account without re-hitting one that is merely rate limited (which would
        just "refresh into another 429").
        """
        now = now or datetime.now(timezone.utc)

        # A manual refresh overrides the automatic policy's memory: clear the
        # failed-state markers so the explicit attempt is never suppressed.
        self._auth_cooldown_until.clear()
        self._auth_failed_expiry.clear()
        self._proactive_noop_until.clear()

        results = [
            authctl.refresh(account.name, account.config_dir, now)
            for account in self.accounts
            if account.provider == "claude"
        ]

        self.limits = [self._fetch_limits_one(account, now) for account in self.accounts]
        self.limits_fetched_at = now
        return results, self.limits

    def snapshot(self, now: datetime | None = None) -> FleetSnapshot:
        """A FleetSnapshot from the current session poll plus cached limits."""
        now = now or datetime.now(timezone.utc)
        return FleetSnapshot(
            taken_at=now,
            sessions=self.poll_sessions(now),
            limits=self.limits,
        )
