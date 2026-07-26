from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import cctop.monitor as monitor_mod
from cctop.collect import Account
from cctop.limits_cache import load, save
from cctop.models import AccountLimits, LimitWindow
from cctop.monitor import FleetMonitor

T0 = datetime(2026, 7, 26, 12, tzinfo=timezone.utc)


def _reading(percent: float = 93.0) -> AccountLimits:
    return AccountLimits(
        "work",
        "max",
        [LimitWindow("session", "5h", percent, T0 + timedelta(hours=2), "warning", True)],
        "api",
        T0,
    )


def test_cache_round_trips_percent_and_reset(tmp_path: Path) -> None:
    path = tmp_path / "limits-cache.json"

    save(path, {"work": _reading()})
    cached = load(path)["work"]

    assert cached.windows[0].percent == 93.0
    assert cached.windows[0].resets_at == T0 + timedelta(hours=2)
    assert path.stat().st_mode & 0o777 == 0o600


def test_new_monitor_shows_shared_cache_during_429(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "limits-cache.json"
    save(path, {"work": _reading()})
    monkeypatch.setattr(
        monitor_mod,
        "account_limits",
        lambda account: AccountLimits(
            account.name,
            "max",
            [],
            "none",
            None,
            error="usage: rate limited (429)",
            retriable=True,
        ),
    )
    monitor = FleetMonitor(
        [Account("work", Path("/work"))],
        limits_cache_path=path,
    )

    result = monitor.poll_limits(T0 + timedelta(minutes=1), force=True)

    assert result[0].windows[0].percent == 93.0
    assert result[0].windows[0].resets_at == T0 + timedelta(hours=2)
    assert result[0].error == "usage: rate limited (429)"


def test_running_monitor_adopts_newer_shared_reading_without_network(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "limits-cache.json"
    calls = []
    monitor = FleetMonitor(
        [Account("work", Path("/work"))],
        limits_interval=timedelta(minutes=3),
        limits_cache_path=path,
    )
    save(path, {"work": _reading(81.0)})
    monkeypatch.setattr(monitor_mod, "account_limits", lambda account: calls.append(account))

    result = monitor.poll_limits(T0 + timedelta(minutes=1))

    assert result[0].windows[0].percent == 81.0
    assert calls == []


def test_recent_partial_cache_does_not_hide_uncached_account(tmp_path: Path) -> None:
    path = tmp_path / "limits-cache.json"
    save(path, {"work": _reading()})
    monitor = FleetMonitor(
        [
            Account("work", Path("/work")),
            Account("needs-login", Path("/needs-login")),
        ],
        limits_interval=timedelta(minutes=3),
        limits_cache_path=path,
    )

    assert monitor.limits_due(T0 + timedelta(minutes=1)) is True
