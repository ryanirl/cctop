"""Usage-limit rate-limit handling: back off, keep the last good numbers.

A transient 429/5xx/network failure must never blank the gauges, must not be
re-hit while cooling down, and a forced refresh (the R key) must respect the
cooldown so it cannot "refresh into another 429".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import cctop.monitor as monitor_mod
from cctop.authctl import RefreshResult
from cctop.collect import Account
from cctop.models import AccountLimits, LimitWindow
from cctop.monitor import _BACKOFF_BASE, FleetMonitor
from cctop.switcher import SwitchResult

T0 = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


def _good(pct: float = 10.0) -> AccountLimits:
    window = LimitWindow("session", "5h", pct, None, "normal", True)
    return AccountLimits("cc-0", "max", [window], "api", T0)


def _rate_limited(retry_after: float | None = None) -> AccountLimits:
    return AccountLimits(
        "cc-0",
        "max",
        [],
        "none",
        None,
        error="usage: rate limited (429)",
        retriable=True,
        retry_after=retry_after,
    )


def _expired() -> AccountLimits:
    return AccountLimits("cc-0", "max", [], "none", None, error="token expired")


def _monitor(monkeypatch, responses: list[AccountLimits]) -> tuple[FleetMonitor, list[int]]:
    calls = [0]
    stream = iter(responses)

    def stub(account: Account) -> AccountLimits:
        calls[0] += 1
        return next(stream)

    monkeypatch.setattr(monitor_mod, "account_limits", stub)
    monitor = FleetMonitor([Account("cc-0", Path("/x"), "claude")])
    return monitor, calls


def test_success_is_cached_as_good(monkeypatch) -> None:
    monitor, _ = _monitor(monkeypatch, [_good(12.0)])
    result = monitor.poll_limits(T0, force=True)
    assert result[0].source == "api"
    assert monitor._good_limits["cc-0"].windows[0].percent == 12.0
    assert "cc-0" not in monitor._cooldown_until


def test_429_keeps_last_good_and_sets_cooldown(monkeypatch) -> None:
    monitor, _ = _monitor(monkeypatch, [_good(20.0), _rate_limited()])
    monitor.poll_limits(T0, force=True)

    later = T0 + timedelta(seconds=200)
    result = monitor.poll_limits(later, force=True)

    assert result[0].source == "api"  # gauges preserved, not blanked
    assert result[0].windows[0].percent == 20.0
    assert monitor._cooldown_until["cc-0"] == later + _BACKOFF_BASE


def test_cooldown_skips_the_network(monkeypatch) -> None:
    monitor, calls = _monitor(monkeypatch, [_good(), _rate_limited()])
    monitor.poll_limits(T0, force=True)  # call 1: good
    monitor.poll_limits(T0 + timedelta(seconds=200), force=True)  # call 2: 429 -> cooldown
    assert calls[0] == 2

    # Forced again while still cooling down: no third network call.
    monitor.poll_limits(T0 + timedelta(seconds=230), force=True)
    assert calls[0] == 2


def test_retry_after_is_honored(monkeypatch) -> None:
    monitor, _ = _monitor(monkeypatch, [_good(), _rate_limited(retry_after=42.0)])
    monitor.poll_limits(T0, force=True)
    later = T0 + timedelta(seconds=200)
    monitor.poll_limits(later, force=True)
    assert monitor._cooldown_until["cc-0"] == later + timedelta(seconds=42)


def test_backoff_doubles_on_repeated_failures(monkeypatch) -> None:
    monitor, _ = _monitor(monkeypatch, [_good(), _rate_limited(), _rate_limited()])
    monitor.poll_limits(T0, force=True)
    t1 = T0 + timedelta(seconds=200)
    monitor.poll_limits(t1, force=True)
    assert monitor._backoff["cc-0"] == _BACKOFF_BASE

    # Advance past the first cooldown so the next forced poll actually fetches.
    t2 = t1 + _BACKOFF_BASE + timedelta(seconds=1)
    monitor.poll_limits(t2, force=True)
    assert monitor._backoff["cc-0"] == _BACKOFF_BASE * 2


def test_no_good_data_surfaces_retriable_error(monkeypatch) -> None:
    monitor, _ = _monitor(monkeypatch, [_rate_limited()])
    result = monitor.poll_limits(T0, force=True)
    assert result[0].source == "none"
    assert result[0].retriable is True


def test_token_expired_drops_stale_good(monkeypatch) -> None:
    monitor, _ = _monitor(monkeypatch, [_good(), _expired()])
    monitor.poll_limits(T0, force=True)
    result = monitor.poll_limits(T0 + timedelta(seconds=200), force=True)
    assert result[0].source == "none"
    assert result[0].error == "token expired"
    assert "cc-0" not in monitor._good_limits


def test_hot_switch_mode_auto_refreshes_saved_login(monkeypatch) -> None:
    monitor, calls = _monitor(monkeypatch, [_expired(), _good(8.0)])
    monitor.hot_switch = True
    refreshes = []

    def refresh(account: str, config_dir: Path, now: datetime) -> RefreshResult:
        refreshes.append((account, config_dir, now))
        return RefreshResult(account, True, "refreshed", now + timedelta(hours=12))

    monkeypatch.setattr(monitor_mod.authctl, "refresh", refresh)
    monkeypatch.setattr(
        monitor_mod.authctl,
        "read_expiry",
        lambda config_dir: T0 + timedelta(hours=1),
    )
    monkeypatch.setattr("cctop.switcher.sync_active_profile", lambda accounts, main: None)

    result = monitor.poll_limits(T0, force=True)

    assert result[0].source == "api"
    assert calls[0] == 2
    assert len(refreshes) == 1


def test_dead_main_credential_is_not_synced_and_requests_failover(monkeypatch) -> None:
    account = Account("cc-0", Path("/saved"), "claude")
    monitor = FleetMonitor(
        [account],
        main_config_dir=Path("/main"),
        hot_switch=True,
    )
    monkeypatch.setattr(monitor_mod.authctl, "read_expiry", lambda config_dir: None)
    monkeypatch.setattr(
        monitor_mod.authctl,
        "refresh",
        lambda name, config_dir, now: RefreshResult(name, False, "needs re-login", None),
    )
    syncs = []
    monkeypatch.setattr(
        "cctop.switcher.sync_active_profile",
        lambda accounts, main: syncs.append((accounts, main)),
    )
    monkeypatch.setattr(monitor_mod, "account_limits", lambda account: _rate_limited())
    recovery = []

    def recover(accounts, limits, main, error, now):
        recovery.append((accounts, limits, main, error, now))
        return SwitchResult(False, "cc-0", "cc-0", error)

    monitor.poll_limits(T0, force=True)
    monkeypatch.setattr("cctop.switcher.recover_auth", recover)
    result = monitor.maybe_auto_switch()

    assert syncs == []
    assert result == SwitchResult(False, "cc-0", "cc-0", "needs re-login")
    assert recovery[0][3:] == ("needs re-login", T0)


def test_hot_switch_reports_dead_local_login_without_usage_request(monkeypatch) -> None:
    monitor, calls = _monitor(monkeypatch, [_rate_limited()])
    monitor.hot_switch = True
    monkeypatch.setattr(
        monitor_mod.authctl,
        "read_expiry",
        lambda config_dir: (
            T0 + timedelta(hours=1)
            if config_dir == monitor.main_config_dir
            else T0 - timedelta(minutes=1)
        ),
    )
    monkeypatch.setattr(
        monitor_mod.authctl,
        "refresh",
        lambda name, config_dir, now: RefreshResult(name, False, "needs re-login", None),
    )
    monkeypatch.setattr("cctop.switcher.sync_active_profile", lambda accounts, main: None)

    result = monitor.poll_limits(T0, force=True)

    assert calls[0] == 0
    assert result[0].error == "needs re-login"
    assert result[0].retriable is False
