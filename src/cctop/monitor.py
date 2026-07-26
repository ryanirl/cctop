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
from pathlib import Path
from typing import TYPE_CHECKING

from . import authctl
from .authctl import RefreshResult
from .collect import Account, _effective_status, account_limits, codex_session_states
from .limits_cache import load as load_limits_cache
from .limits_cache import save as save_limits_cache
from .models import AccountLimits, FleetSnapshot, SessionState, UsageTotals
from .registry import process_alive, read_registry
from .transcript import TranscriptTailer, find_transcript

if TYPE_CHECKING:
    # Imported lazily at runtime: switching is only reachable in hot-switch mode.
    from .switcher import SwitchResult

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
_AUTH_RETRY = timedelta(minutes=15)

_EMPTY_TOTALS = UsageTotals(0, 0, 0, 0, 0.0)


class FleetMonitor:
    """Holds per-session tailers and cached limits between polls."""

    def __init__(
        self,
        accounts: list[Account],
        limits_interval: timedelta = DEFAULT_LIMITS_INTERVAL,
        main_config_dir: Path | None = None,
        auto_switch_remaining_percent: float = 1.0,
        hot_switch: bool = False,
        limits_cache_path: Path | None = None,
    ) -> None:
        self.accounts = accounts
        self.limits_interval = limits_interval
        self.main_config_dir = main_config_dir or Path.home() / ".claude"
        self.auto_switch_remaining_percent = auto_switch_remaining_percent
        self.hot_switch = hot_switch
        self._limits_cache_path = limits_cache_path

        self._tailers: dict[str, TranscriptTailer] = {}
        cached = load_limits_cache(limits_cache_path) if limits_cache_path is not None else {}
        account_names = {account.name for account in accounts}
        self._good_limits = {name: item for name, item in cached.items() if name in account_names}
        self.limits = list(self._good_limits.values())
        fetched = [item.fetched_at for item in self.limits if item.fetched_at is not None]
        self.limits_fetched_at = max(fetched) if fetched else None
        self._codex_cache: dict[str, list[SessionState]] = {}
        self._codex_polled_at: datetime | None = None

        # Per-account rate-limit state: the last successful fetch (so a transient
        # failure can keep showing real numbers), when the account may be fetched
        # again, and the current backoff length.
        self._cooldown_until: dict[str, datetime] = {}
        self._backoff: dict[str, timedelta] = {}
        self._transient_error: dict[str, str] = {}
        self._auth_retry_after: dict[str, datetime] = {}
        self._auth_error: dict[str, str] = {}
        self._main_auth_retry_after: datetime | None = None
        self._main_auth_error: str | None = None
        self._main_auth_checked_at: datetime | None = None

    def _reload_limits_cache(self) -> None:
        """Adopt newer readings written by another cctop process."""
        if self._limits_cache_path is None:
            return
        shared = load_limits_cache(self._limits_cache_path)
        account_names = {account.name for account in self.accounts}
        changed = False
        for name, item in shared.items():
            if name not in account_names:
                continue
            current = self._good_limits.get(name)
            if current is None or (
                item.fetched_at is not None
                and (current.fetched_at is None or item.fetched_at > current.fetched_at)
            ):
                self._good_limits[name] = item
                changed = True
        if not changed:
            return
        self.limits = [
            self._good_limits[account.name]
            for account in self.accounts
            if account.name in self._good_limits
        ]
        fetched = [item.fetched_at for item in self.limits if item.fetched_at is not None]
        if fetched:
            newest = max(fetched)
            if self.limits_fetched_at is None or newest > self.limits_fetched_at:
                self.limits_fetched_at = newest

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
        """Rebuild the live session list, advancing each tailer by new bytes.

        Claude sessions come from each account's own config dir, or -- in
        hot-switch mode, where every session runs in one directory -- from the
        main dir alone, attributed to whichever profile is currently active.
        """
        now = now or datetime.now(timezone.utc)

        codex_due = self._codex_polled_at is None or now - self._codex_polled_at >= CODEX_INTERVAL

        states: list[SessionState] = []
        seen: set[str] = set()

        def append_claude(config_dir: Path, account_name: str) -> None:
            for session in read_registry(config_dir):
                if not process_alive(session.pid):
                    continue
                seen.add(session.session_id)
                status = _effective_status(session, True, now)

                tailer = self._tailer_for(config_dir, session.session_id)
                if tailer is None:
                    states.append(
                        SessionState(
                            session=session,
                            account=account_name,
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
                        account=account_name,
                        alive=True,
                        status=status,
                        model=tailer.model,
                        context=tailer.context,
                        totals=tailer.totals(),
                        last_activity=tailer.last_activity or session.updated_at,
                    )
                )

        for account in self.accounts:
            if account.provider == "codex":
                if codex_due:
                    self._codex_cache[account.name] = codex_session_states(account, now)
                states.extend(self._codex_cache.get(account.name, []))
            elif not self.hot_switch:
                append_claude(account.config_dir, account.name)

        if self.hot_switch:
            from .switcher import active_account

            active = active_account(self.accounts, self.main_config_dir)
            append_claude(self.main_config_dir, active.name if active is not None else "main")

        if codex_due:
            self._codex_polled_at = now

        # Drop tailers for sessions that have exited, so state stays bounded.
        for session_id in list(self._tailers):
            if session_id not in seen:
                del self._tailers[session_id]

        states.sort(key=lambda state: state.last_activity or now, reverse=True)
        return states

    def maybe_auto_switch(self) -> SwitchResult | None:
        """Apply the configured remaining-headroom rotation policy to cached limits."""
        if not self.hot_switch:
            return None
        from .switcher import auto_switch, recover_auth

        if self._main_auth_error is not None:
            result = recover_auth(
                self.accounts,
                self.limits,
                self.main_config_dir,
                self._main_auth_error,
                self._main_auth_checked_at,
            )
            if result.ok:
                self._main_auth_error = None
                self._main_auth_retry_after = None
            return result

        return auto_switch(
            self.accounts,
            self.limits,
            self.main_config_dir,
            self.auto_switch_remaining_percent,
        )

    def switch_best(self) -> SwitchResult | None:
        """Hot-switch to the currently healthiest saved Claude profile."""
        if not self.hot_switch:
            return None
        from .switcher import best_account, switch_account

        target = best_account(self.accounts, self.limits, self.main_config_dir)
        if target is None:
            return None
        return switch_account(self.accounts, target, self.main_config_dir)

    def limits_due(self, now: datetime) -> bool:
        configured = {account.name for account in self.accounts}
        represented = {item.account for item in self.limits}
        if represented != configured:
            return True
        if self.limits_fetched_at is None:
            return True
        return now - self.limits_fetched_at >= self.limits_interval

    def _fetch_limits_one(self, account: Account, now: datetime) -> AccountLimits:
        """Fetch one account's limits with rate-limit backoff.

        While an account is in cooldown after a transient failure, its last good
        result is returned unchanged (gauges persist, only the "N ago" age
        grows) rather than re-hitting the endpoint. A fresh success clears the
        backoff; a fresh transient failure lengthens it (or honors Retry-After);
        a real error (token expired) is surfaced as-is.
        """
        name = account.name
        good = self._good_limits.get(name)

        # A locally expired saved credential is actionable before any network
        # request. Anthropic may return a fleet-wide 429 before authentication,
        # which would otherwise disguise a logged-out profile as merely rate
        # limited and waste another request every time its cooldown expires.
        if self.hot_switch and account.provider == "claude":
            expires_at = authctl.read_expiry(account.auth_dir)
            if expires_at is None or expires_at <= now:
                auth_retry = self._auth_retry_after.get(name)
                if auth_retry is None or now >= auth_retry:
                    refreshed = authctl.refresh(name, account.auth_dir, now)
                    if refreshed.ok:
                        self._auth_retry_after.pop(name, None)
                        self._auth_error.pop(name, None)
                        self._cooldown_until.pop(name, None)
                        self._backoff.pop(name, None)
                    else:
                        self._auth_retry_after[name] = now + _AUTH_RETRY
                        self._auth_error[name] = refreshed.message
                error = self._auth_error.get(name)
                if error is not None:
                    self._good_limits.pop(name, None)
                    self._transient_error.pop(name, None)
                    return AccountLimits(
                        name,
                        good.tier if good is not None else None,
                        [],
                        "none",
                        None,
                        error=error,
                    )
            else:
                self._auth_retry_after.pop(name, None)
                self._auth_error.pop(name, None)

        cooldown = self._cooldown_until.get(name)
        if cooldown is not None and now < cooldown:
            if good is not None:
                return replace(
                    good,
                    error=self._transient_error.get(name, "rate limited, retrying"),
                    retriable=True,
                )
            return AccountLimits(
                name, None, [], "none", None, error="rate limited, retrying", retriable=True
            )

        result = account_limits(account)

        # In hot-switch mode every saved login must remain ready to activate.
        # Let Claude Code refresh an expired access token itself; this needs no
        # login unless the saved refresh token is genuinely dead. Bound retries
        # so a dead login never spawns a command on every limits poll.
        auth_retry = self._auth_retry_after.get(name)
        expired = result.error is not None and result.error.startswith("token expired")
        if (
            self.hot_switch
            and account.provider == "claude"
            and expired
            and (auth_retry is None or now >= auth_retry)
        ):
            refreshed = authctl.refresh(account.name, account.auth_dir, now)
            if refreshed.ok:
                self._auth_retry_after.pop(name, None)
                self._auth_error.pop(name, None)
                result = account_limits(account)
            else:
                self._auth_retry_after[name] = now + _AUTH_RETRY
                self._auth_error[name] = refreshed.message
                result = replace(result, error=refreshed.message)

        if result.source == "api":
            self._good_limits[name] = result
            self._backoff.pop(name, None)
            self._cooldown_until.pop(name, None)
            self._transient_error.pop(name, None)
            return result

        if result.retriable:
            if result.retry_after is not None:
                delay = timedelta(seconds=result.retry_after)
            else:
                previous = self._backoff.get(name)
                delay = _BACKOFF_BASE if previous is None else min(previous * 2, _BACKOFF_CAP)
            self._backoff[name] = delay
            self._cooldown_until[name] = now + delay
            self._transient_error[name] = result.error or "usage temporarily unavailable"
            return (
                replace(good, error=self._transient_error[name], retriable=True)
                if good is not None
                else result
            )

        # Non-retriable (token expired, wrong credential): a real, actionable
        # state. Drop any stale-good so the UI shows what the user must fix.
        self._good_limits.pop(name, None)
        self._transient_error.pop(name, None)
        return result

    def _prepare_main_auth(self, now: datetime) -> None:
        """Refresh the live credential or mark it for immediate failover.

        Saved-profile usage probes do not prove that the mutable main store is
        usable. Check that store directly before syncing it back: if Claude
        cannot refresh it, preserving it would overwrite the saved profile with
        the credential that just produced ``Login expired``.
        """
        from .switcher import active_account, sync_active_profile

        self._main_auth_checked_at = now
        expires_at = authctl.read_expiry(self.main_config_dir)
        if expires_at is not None and expires_at > now:
            self._main_auth_error = None
            self._main_auth_retry_after = None
            sync_active_profile(self.accounts, self.main_config_dir)
            return

        if self._main_auth_retry_after is not None and now < self._main_auth_retry_after:
            return

        current = active_account(self.accounts, self.main_config_dir)
        refreshed = authctl.refresh(
            current.name if current is not None else "main",
            self.main_config_dir,
            now,
        )
        if refreshed.ok:
            self._main_auth_error = None
            self._main_auth_retry_after = None
            sync_active_profile(self.accounts, self.main_config_dir)
            return

        self._main_auth_error = refreshed.message
        self._main_auth_retry_after = now + _AUTH_RETRY

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
        self._reload_limits_cache()
        if not force and not self.limits_due(now):
            return self.limits

        if self.hot_switch:
            self._prepare_main_auth(now)

        self.limits = [self._fetch_limits_one(account, now) for account in self.accounts]
        self.limits_fetched_at = now
        if self._limits_cache_path is not None:
            save_limits_cache(self._limits_cache_path, self._good_limits)
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

        results = [
            authctl.refresh(account.name, account.auth_dir, now)
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
