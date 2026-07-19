"""Pure collector logic: model math, status normalization, stats helpers."""

from __future__ import annotations

from datetime import date

from cctop.models import ContextWindow, LimitWindow, UsageTotals
from cctop.stats import DayStat, FleetStats, active_days, busiest_day, longest_streak, top_models
from cctop.status import SessionStatus, normalize_status


def test_usage_totals_sums_all_token_kinds() -> None:
    totals = UsageTotals(1, 2, 3, 4, cost_usd=0.0)
    assert totals.total_tokens == 10


def test_context_window_fraction_clamps() -> None:
    assert ContextWindow(50, 100).used_fraction == 0.5
    assert ContextWindow(0, 0).used_fraction == 0.0  # no divide-by-zero
    assert ContextWindow(200, 100).used_fraction == 1.0  # clamped


def test_limit_window_fraction_clamps() -> None:
    def frac(percent: float) -> float:
        return LimitWindow("session", "5h", percent, None, "normal", False).used_fraction

    assert frac(50) == 0.5
    assert frac(150) == 1.0
    assert frac(-5) == 0.0


def test_normalize_status_known_and_unknown() -> None:
    assert normalize_status("idle") is SessionStatus.IDLE
    assert normalize_status("busy") is SessionStatus.GENERATING
    assert normalize_status("shell") is SessionStatus.SHELL
    assert normalize_status("waiting") is SessionStatus.WAITING_PERMISSION
    assert normalize_status("BUSY") is SessionStatus.GENERATING  # case-insensitive
    assert normalize_status(None) is SessionStatus.UNKNOWN
    assert normalize_status("some-future-status") is SessionStatus.UNKNOWN


def _stats(days: dict[date, int], model_tokens: dict[str, int] | None = None) -> FleetStats:
    stats = FleetStats()
    for day, messages in days.items():
        stats.days[day] = DayStat(day=day, messages=messages)
    stats.model_tokens = model_tokens or {}
    return stats


def test_active_days_counts_only_nonzero() -> None:
    stats = _stats({date(2026, 1, 1): 5, date(2026, 1, 2): 0, date(2026, 1, 3): 2})
    assert active_days(stats) == 2


def test_busiest_day_picks_max_messages() -> None:
    stats = _stats({date(2026, 1, 1): 5, date(2026, 1, 2): 9, date(2026, 1, 3): 2})
    best = busiest_day(stats)
    assert best is not None and best.day == date(2026, 1, 2) and best.messages == 9


def test_busiest_day_none_when_empty() -> None:
    assert busiest_day(FleetStats()) is None


def test_longest_streak_counts_consecutive() -> None:
    # Jan 1,2,3 active; gap; Jan 5,6 active -> longest run is 3.
    stats = _stats(
        {
            date(2026, 1, 1): 1,
            date(2026, 1, 2): 1,
            date(2026, 1, 3): 1,
            date(2026, 1, 5): 1,
            date(2026, 1, 6): 1,
        }
    )
    assert longest_streak(stats) == 3


def test_top_models_ranked_and_limited() -> None:
    stats = _stats({}, {"opus": 300, "haiku": 100, "sonnet": 200})
    assert top_models(stats, limit=2) == [("opus", 300), ("sonnet", 200)]


def test_next_refresh_countdown() -> None:
    from datetime import datetime, timedelta, timezone

    from cctop.app import _format_next_refresh

    now = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
    assert _format_next_refresh(now - timedelta(seconds=12), 180, now) == "next in 2m48s"
    assert _format_next_refresh(now - timedelta(seconds=45), 60, now) == "next in 15s"
    assert _format_next_refresh(now - timedelta(seconds=200), 180, now) == "refreshing"
    assert _format_next_refresh(None, 180, now) == "refreshing"
