"""Codex usage statistics, aggregated from the rollout files.

Codex has no stats-cache.json, so daily activity is computed by scanning the
rollout JSONL files under ~/.codex/sessions. Each file is parsed at most once
per modification (cached by mtime), so after the first load only actively-
growing sessions are re-read. Produces the same FleetStats shape as the Claude
side, so the stats view can render both providers uniformly.

Read-only.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

# stats.py defines DayStat and FleetStats; share the shape across providers.
from .stats import DayStat, FleetStats

CODEX_DIR = Path.home() / ".codex"

# Per-file parse cache: path -> (mtime, contribution dict).
_FILE_CACHE: dict[str, tuple[float, dict]] = {}


def _day(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_file(path: Path) -> dict:
    """One rollout's contribution: per-day messages, start day, model, tokens."""
    messages: dict[date, int] = {}
    start_day: date | None = None
    model: str | None = None
    total_tokens = 0

    try:
        handle = path.open()
    except OSError:
        return {"messages": {}, "start_day": None, "model": None, "tokens": 0}

    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = record.get("type")
            payload = record.get("payload") or {}
            day = _day(record.get("timestamp"))
            if kind == "session_meta":
                start_day = _day(payload.get("timestamp")) or day
            elif kind == "turn_context" and isinstance(payload.get("model"), str):
                model = payload["model"]
            elif kind == "event_msg":
                event = payload.get("type")
                if event in ("user_message", "agent_message") and day is not None:
                    messages[day] = messages.get(day, 0) + 1
                elif event == "token_count":
                    info = payload.get("info") or {}
                    usage = info.get("total_token_usage") or {}
                    total_tokens = usage.get("total_tokens", total_tokens)

    return {
        "messages": messages,
        "start_day": start_day,
        "model": model,
        "tokens": total_tokens,
    }


def _contribution(path: Path) -> dict:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {"messages": {}, "start_day": None, "model": None, "tokens": 0}
    cached = _FILE_CACHE.get(str(path))
    if cached is not None and cached[0] == mtime:
        return cached[1]
    parsed = _parse_file(path)
    _FILE_CACHE[str(path)] = (mtime, parsed)
    return parsed


def build_fleet_stats(accounts) -> FleetStats:
    """Aggregate Codex daily activity across the given codex accounts."""
    stats = FleetStats()
    messages: dict[date, int] = {}
    sessions: dict[date, int] = {}
    tokens: dict[date, int] = {}

    for account in accounts:
        sessions_dir = account.config_dir / "sessions"
        if not sessions_dir.is_dir():
            continue
        for path in sessions_dir.glob("**/rollout-*.jsonl"):
            data = _contribution(path)
            stats.total_sessions += 1
            for day, count in data["messages"].items():
                messages[day] = messages.get(day, 0) + count
                stats.total_messages += count
            start = data["start_day"]
            if start is not None:
                sessions[start] = sessions.get(start, 0) + 1
                tokens[start] = tokens.get(start, 0) + data["tokens"]
                if stats.first_day is None or start < stats.first_day:
                    stats.first_day = start
            if data["model"] and data["tokens"]:
                stats.model_tokens[data["model"]] = (
                    stats.model_tokens.get(data["model"], 0) + data["tokens"]
                )
            stats.per_account[account.name] = (
                stats.per_account.get(account.name, (0, 0))[0] + 1,
                stats.per_account.get(account.name, (0, 0))[1] + sum(data["messages"].values()),
            )

    for day in set(messages) | set(sessions) | set(tokens):
        stats.days[day] = DayStat(
            day=day,
            messages=messages.get(day, 0),
            sessions=sessions.get(day, 0),
            tool_calls=0,
            tokens=tokens.get(day, 0),
        )
    return stats
