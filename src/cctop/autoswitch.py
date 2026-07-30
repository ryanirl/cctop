"""A headless hot-switch supervisor independent of the Textual TUI."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from . import config as config_module
from .collect import AccountLimits, default_accounts
from .monitor import FleetMonitor
from .switcher import SwitchResult, active_account, ensure_stable_accounts


@dataclass(frozen=True)
class AutoswitchCycle:
    active: str | None
    limits: list[AccountLimits]
    switch: SwitchResult | None


class AutoswitchService:
    """Poll limits and rotate logins even when no interactive display is open."""

    def __init__(self, monitor: FleetMonitor, interval_seconds: float) -> None:
        self.monitor = monitor
        self.interval_seconds = interval_seconds

    @classmethod
    def from_config(cls, config: config_module.Config | None = None) -> AutoswitchService:
        config = config or config_module.load_config()
        if not config.hot_switch():
            raise ValueError("hot switching is disabled")

        main_config_dir = config.main_config_dir()
        accounts = ensure_stable_accounts(default_accounts(), main_config_dir)
        claude_accounts = [account for account in accounts if account.provider == "claude"]
        if not claude_accounts:
            raise ValueError("no Claude profiles found")

        monitor = FleetMonitor(
            claude_accounts,
            main_config_dir=main_config_dir,
            auto_switch_remaining_percent=config.auto_switch_remaining_percent(),
            hot_switch=True,
            limits_cache_path=config_module.config_dir() / "limits-cache.json",
        )
        return cls(monitor, config.limits_refresh_seconds(180.0))

    def poll(self, now: datetime | None = None) -> AutoswitchCycle:
        now = now or datetime.now(timezone.utc)
        limits = self.monitor.poll_limits(now, force=True)
        switch = self.monitor.maybe_auto_switch()
        current = active_account(self.monitor.accounts, self.monitor.main_config_dir)
        return AutoswitchCycle(current.name if current is not None else None, limits, switch)

    def run(
        self,
        *,
        once: bool = False,
        emit: Callable[[str], None] = print,
        wait: Callable[[float], None] = time.sleep,
    ) -> AutoswitchCycle:
        """Run immediately, then continue at the configured interval."""
        previous_summary: str | None = None
        while True:
            cycle = self.poll()
            summary = _cycle_summary(cycle)
            if summary != previous_summary or cycle.switch is not None:
                emit(summary)
                previous_summary = summary
            if once:
                return cycle
            wait(self.interval_seconds)


def _binding_percent(limits: AccountLimits) -> float | None:
    if limits.source != "api":
        return None
    values = [window.percent for window in limits.windows if window.has_data]
    return max(values, default=0.0)


def _cycle_summary(cycle: AutoswitchCycle) -> str:
    readings = []
    for item in cycle.limits:
        percent = _binding_percent(item)
        if percent is not None:
            suffix = f" (cached: {item.error})" if item.error else ""
            readings.append(f"{item.account}={percent:g}%{suffix}")
        else:
            readings.append(f"{item.account}=unavailable ({item.error or 'no usage data'})")
    status = (
        cycle.switch.message if cycle.switch is not None else f"active {cycle.active or 'unknown'}"
    )
    return f"{status}; " + ", ".join(readings)
