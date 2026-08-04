"""Automatic token refresh: the expired-token banner must never be shown while
the delegated refresh path works.

The policy under test: renew proactively before expiry, catch a 401 reactively
and refetch in the same cycle, never spam attempts when the refresh token is
dead (cooldown plus a sticky needs-/login state that clears itself when a real
login changes the stored expiry), and stay away from Codex and disabled setups.
authctl is mocked throughout; the delegated mechanism itself is authctl's own
concern.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import cctop.monitor as monitor_mod
from cctop.authctl import RefreshResult
from cctop.collect import Account
from cctop.models import AccountLimits, LimitWindow
from cctop.monitor import _AUTH_RETRY, FleetMonitor

T0 = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)
ACCOUNT = Account("cc-0", Path("/tmp/fake-claude"))


def _good() -> AccountLimits:
    window = LimitWindow("session", "5h", 10.0, None, "normal", True)
    return AccountLimits("cc-0", "max", [window], "api", T0)


def _expired() -> AccountLimits:
    return AccountLimits(
        "cc-0",
        "max",
        [],
        "none",
        None,
        error="token expired - run cc-0 to refresh",
        auth_expired=True,
    )


class _AuthStub:
    """A scriptable stand-in for authctl: expiry readings and refresh outcomes."""

    def __init__(self, expiry: datetime | None, refresh_ok: bool = True) -> None:
        self.expiry = expiry
        self.refresh_ok = refresh_ok
        self.refresh_calls = 0
        self.ensure_calls = 0

    def read_expiry(self, config_dir: Path) -> datetime | None:
        return self.expiry

    def refresh(self, account: str, config_dir: Path, now=None) -> RefreshResult:
        self.refresh_calls += 1
        if self.refresh_ok:
            self.expiry = (now or T0) + timedelta(hours=12)
            return RefreshResult(account, True, "refreshed", self.expiry)
        return RefreshResult(account, False, "needs re-login - run the account and /login", None)

    def ensure_fresh(self, account: str, config_dir: Path, now=None) -> RefreshResult | None:
        self.ensure_calls += 1
        now = now or T0
        if self.expiry is None or self.expiry - now > timedelta(minutes=30):
            return None
        return self.refresh(account, config_dir, now)


def _install(monkeypatch, stub: _AuthStub, fetches: list[AccountLimits]) -> FleetMonitor:
    monkeypatch.setattr(monitor_mod.authctl, "read_expiry", stub.read_expiry)
    monkeypatch.setattr(monitor_mod.authctl, "refresh", stub.refresh)
    monkeypatch.setattr(monitor_mod.authctl, "ensure_fresh", stub.ensure_fresh)
    monkeypatch.setattr(monitor_mod, "account_limits", lambda account: fetches.pop(0))
    return FleetMonitor([ACCOUNT])


def test_proactive_refresh_runs_before_fetch(monkeypatch) -> None:
    # Expiry within the margin: the refresh happens first, the fetch succeeds,
    # and the expired banner never exists.
    stub = _AuthStub(expiry=T0 + timedelta(minutes=5))
    monitor = _install(monkeypatch, stub, [_good()])

    result = monitor._fetch_limits_one(ACCOUNT, T0)

    assert stub.refresh_calls == 1
    assert result.source == "api"


def test_comfortably_valid_token_is_left_alone(monkeypatch) -> None:
    stub = _AuthStub(expiry=T0 + timedelta(hours=10))
    monitor = _install(monkeypatch, stub, [_good()])

    monitor._fetch_limits_one(ACCOUNT, T0)

    assert stub.refresh_calls == 0


def test_reactive_refresh_refetches_in_the_same_cycle(monkeypatch) -> None:
    # Unreadable expiry so the proactive check passes, then the fetch 401s:
    # one delegated refresh and an immediate refetch replace the error.
    stub = _AuthStub(expiry=None)
    monitor = _install(monkeypatch, stub, [_expired(), _good()])

    result = monitor._fetch_limits_one(ACCOUNT, T0)

    assert stub.refresh_calls == 1
    assert result.source == "api"
    assert monitor.pop_auth_notices() == ["cc-0: token auto-refreshed"]


def test_failed_refresh_is_sticky_until_login(monkeypatch) -> None:
    dead_expiry = T0 - timedelta(hours=1)
    stub = _AuthStub(expiry=dead_expiry, refresh_ok=False)
    fetches = [_expired(), _expired(), _expired()]
    monitor = _install(monkeypatch, stub, fetches)

    first = monitor._fetch_limits_one(ACCOUNT, T0)
    assert stub.refresh_calls == 1
    assert first.error is not None and "/login" in first.error
    assert monitor.pop_auth_notices() == ["cc-0: token needs /login"]

    # Well past the cooldown, same dead token: still no new attempt.
    later = T0 + _AUTH_RETRY * 3
    second = monitor._fetch_limits_one(ACCOUNT, later)
    assert stub.refresh_calls == 1
    assert second.error is not None and "/login" in second.error

    # A real /login changes the stored expiry; the failed state clears itself.
    stub.expiry = later + timedelta(hours=12)
    stub.refresh_ok = True
    fetches.append(_good())
    third = monitor._fetch_limits_one(ACCOUNT, later + timedelta(minutes=1))
    assert third.source == "api"


def test_cooldown_limits_attempts_when_expiry_is_unreadable(monkeypatch) -> None:
    # With no expiry reading the sticky marker cannot bind to a token, so the
    # cooldown alone must bound how often a failing refresh is attempted.
    stub = _AuthStub(expiry=None, refresh_ok=False)
    monitor = _install(monkeypatch, stub, [_expired(), _expired(), _expired()])

    monitor._fetch_limits_one(ACCOUNT, T0)
    monitor._fetch_limits_one(ACCOUNT, T0 + timedelta(minutes=3))
    assert stub.refresh_calls == 1

    monitor._fetch_limits_one(ACCOUNT, T0 + _AUTH_RETRY + timedelta(minutes=1))
    assert stub.refresh_calls == 2


def test_disabled_flag_never_touches_authctl(monkeypatch) -> None:
    stub = _AuthStub(expiry=T0 - timedelta(hours=1), refresh_ok=True)
    monitor = _install(monkeypatch, stub, [_expired()])
    monitor.auto_refresh_tokens = False

    result = monitor._fetch_limits_one(ACCOUNT, T0)

    assert stub.refresh_calls == 0 and stub.ensure_calls == 0
    assert result.error is not None and "token expired" in result.error


def test_codex_accounts_are_never_refreshed(monkeypatch) -> None:
    stub = _AuthStub(expiry=T0 - timedelta(hours=1))
    codex = Account("cx-0", Path("/tmp/fake-codex"), provider="codex")
    monkeypatch.setattr(monitor_mod.authctl, "refresh", stub.refresh)
    monkeypatch.setattr(monitor_mod.authctl, "ensure_fresh", stub.ensure_fresh)
    expired_codex = AccountLimits(
        "cx-0", None, [], "none", None, error="token expired - run codex", auth_expired=True
    )
    monkeypatch.setattr(monitor_mod, "account_limits", lambda account: expired_codex)
    monitor = FleetMonitor([codex])

    result = monitor._fetch_limits_one(codex, T0)

    assert stub.refresh_calls == 0 and stub.ensure_calls == 0
    assert result.auth_expired


def test_proactive_noop_backs_off_but_reactive_stays_armed(monkeypatch) -> None:
    # Claude Code may judge a token inside our margin "already valid" (its own
    # refresh margin is tighter). Proactive attempts must then space out, but
    # a real 401 in that window must still trigger the reactive refresh.
    stub = _AuthStub(expiry=T0 + timedelta(minutes=10))

    def noop_refresh(account: str, config_dir: Path, now=None) -> RefreshResult:
        stub.refresh_calls += 1
        return RefreshResult(account, True, "already valid", stub.expiry)

    stub.refresh = noop_refresh  # type: ignore[method-assign]
    fetches = [_good(), _good(), _expired(), _good()]
    monitor = _install(monkeypatch, stub, fetches)

    monitor._fetch_limits_one(ACCOUNT, T0)
    monitor._fetch_limits_one(ACCOUNT, T0 + timedelta(minutes=3))
    assert stub.refresh_calls == 1  # second poll inside the no-op backoff

    result = monitor._fetch_limits_one(ACCOUNT, T0 + timedelta(minutes=4))
    assert stub.refresh_calls == 2  # the 401 bypassed the proactive backoff
    assert result.source == "api"


def test_manual_refresh_clears_the_failed_state(monkeypatch) -> None:
    stub = _AuthStub(expiry=T0 - timedelta(hours=1), refresh_ok=False)
    monitor = _install(monkeypatch, stub, [_expired(), _good()])

    monitor._fetch_limits_one(ACCOUNT, T0)
    assert "cc-0" in monitor._auth_failed_expiry

    stub.refresh_ok = True
    monitor.refresh_credentials(T0 + timedelta(minutes=1))
    assert monitor._auth_failed_expiry == {}
    assert monitor._auth_cooldown_until == {}
