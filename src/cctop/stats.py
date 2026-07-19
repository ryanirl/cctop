"""Global usage statistics, read from each account's stats-cache.json.

Claude Code maintains a per-account stats-cache.json with daily activity
(messages, sessions, tool calls), daily token counts by model, and lifetime
totals. This module reads and merges those across accounts into a FleetStats
for the stats view (a GitHub-style contribution heatmap plus summary numbers).

Read-only: this module only ever reads stats-cache.json; it never writes or
deletes anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path


@dataclass(frozen=True)
class DayStat:
    """One day's merged activity across all accounts."""

    day: date
    messages: int = 0
    sessions: int = 0
    tool_calls: int = 0
    tokens: int = 0


@dataclass
class FleetStats:
    """Lifetime usage merged across accounts, plus per-day activity."""

    total_sessions: int = 0
    total_messages: int = 0
    total_tool_calls: int = 0
    first_day: date | None = None
    days: dict[date, DayStat] = field(default_factory=dict)
    model_tokens: dict[str, int] = field(default_factory=dict)
    per_account: dict[str, tuple[int, int]] = field(default_factory=dict)


def _parse_day(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _read_cache(config_dir: Path) -> dict | None:
    try:
        return json.loads((config_dir / "stats-cache.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _normalize_model(model: str) -> str:
    """Drop the claude- prefix and any trailing -YYYYMMDD date snapshot."""
    name = re.sub(r"-\d{8}$", "", model)
    return name.removeprefix("claude-")


def build_fleet_stats(accounts) -> FleetStats:
    """Merge every account's stats-cache.json into one FleetStats."""
    stats = FleetStats()
    messages: dict[date, int] = {}
    sessions: dict[date, int] = {}
    tools: dict[date, int] = {}
    tokens: dict[date, int] = {}

    for account in accounts:
        cache = _read_cache(account.config_dir)
        if cache is None:
            stats.per_account[account.name] = (0, 0)
            continue

        account_sessions = cache.get("totalSessions", 0)
        account_messages = cache.get("totalMessages", 0)
        stats.total_sessions += account_sessions
        stats.total_messages += account_messages
        stats.per_account[account.name] = (account_sessions, account_messages)

        first = _parse_day(cache.get("firstSessionDate"))
        if first and (stats.first_day is None or first < stats.first_day):
            stats.first_day = first

        for entry in cache.get("dailyActivity", []):
            day = _parse_day(entry.get("date"))
            if day is None:
                continue
            messages[day] = messages.get(day, 0) + entry.get("messageCount", 0)
            sessions[day] = sessions.get(day, 0) + entry.get("sessionCount", 0)
            tools[day] = tools.get(day, 0) + entry.get("toolCallCount", 0)
            stats.total_tool_calls += entry.get("toolCallCount", 0)

        for entry in cache.get("dailyModelTokens", []):
            day = _parse_day(entry.get("date"))
            if day is None:
                continue
            by_model = entry.get("tokensByModel") or {}
            tokens[day] = tokens.get(day, 0) + sum(by_model.values())

        for model, usage in (cache.get("modelUsage") or {}).items():
            if not isinstance(usage, dict):
                continue
            total = (
                usage.get("inputTokens", 0)
                + usage.get("outputTokens", 0)
                + usage.get("cacheReadInputTokens", 0)
                + usage.get("cacheCreationInputTokens", 0)
            )
            name = _normalize_model(model)
            stats.model_tokens[name] = stats.model_tokens.get(name, 0) + total

    for day in set(messages) | set(tokens):
        stats.days[day] = DayStat(
            day=day,
            messages=messages.get(day, 0),
            sessions=sessions.get(day, 0),
            tool_calls=tools.get(day, 0),
            tokens=tokens.get(day, 0),
        )
    return stats


def busiest_day(stats: FleetStats) -> DayStat | None:
    """The day with the most messages, for the summary line."""
    active = [d for d in stats.days.values() if d.messages > 0]
    return max(active, key=lambda d: d.messages) if active else None


def active_days(stats: FleetStats) -> int:
    return sum(1 for d in stats.days.values() if d.messages > 0)


def longest_streak(stats: FleetStats) -> int:
    """Longest run of consecutive days with any activity."""
    active = sorted(d for d, s in stats.days.items() if s.messages > 0)
    best = run = 0
    previous: date | None = None
    for day in active:
        run = run + 1 if previous is not None and day - previous == timedelta(days=1) else 1
        best = max(best, run)
        previous = day
    return best


def top_models(stats: FleetStats, limit: int = 4) -> list[tuple[str, int]]:
    ranked = sorted(stats.model_tokens.items(), key=lambda kv: kv[1], reverse=True)
    return ranked[:limit]
