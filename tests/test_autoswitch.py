from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import cctop.autoswitch as autoswitch_mod
from cctop.autoswitch import AutoswitchService
from cctop.collect import Account
from cctop.config import Config
from cctop.models import AccountLimits
from cctop.switcher import SwitchResult

T0 = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)


class FakeMonitor:
    def __init__(self) -> None:
        self.accounts = [Account("work", Path("/saved"))]
        self.main_config_dir = Path("/main")
        self.calls = []

    def poll_limits(self, now: datetime, force: bool = False) -> list[AccountLimits]:
        self.calls.append((now, force))
        return [AccountLimits("work", "max", [], "none", None, error="token expired")]

    def maybe_auto_switch(self) -> SwitchResult:
        return SwitchResult(True, "work", "other", "switched to other")


def test_service_polls_before_applying_switch_policy(monkeypatch) -> None:
    monitor = FakeMonitor()
    monkeypatch.setattr(
        autoswitch_mod,
        "active_account",
        lambda accounts, main: Account("other", Path("/other")),
    )

    cycle = AutoswitchService(monitor, 60).poll(T0)  # type: ignore[arg-type]

    assert monitor.calls == [(T0, True)]
    assert cycle.active == "other"
    assert cycle.switch is not None and cycle.switch.ok is True


def test_once_does_not_wait(monkeypatch) -> None:
    monitor = FakeMonitor()
    monkeypatch.setattr(autoswitch_mod, "active_account", lambda accounts, main: None)
    waits = []
    output = []

    AutoswitchService(monitor, 60).run(  # type: ignore[arg-type]
        once=True,
        emit=output.append,
        wait=waits.append,
    )

    assert waits == []
    assert output == ["switched to other; work=unavailable (token expired)"]


def test_service_requires_hot_switch() -> None:
    with pytest.raises(ValueError, match="disabled"):
        AutoswitchService.from_config(Config(settings={"hot_switch": False}))
