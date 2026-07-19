"""The statistics screen: a GitHub-style contribution heatmap plus totals.

Toggled from the main view with `s`. Reads merged FleetStats (from stats.py)
and renders a daily-activity heatmap in the teal ramp, lifetime totals, streaks,
and a per-model token breakdown. Read-only.
"""

from __future__ import annotations

from datetime import date, timedelta

from rich.console import Group
from rich.table import Table as RichTable
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from . import stats as stats_mod
from .collect import Account
from .stats import FleetStats

TEAL = "#20B2AA"
MUTED = "grey50"
EMPTY_CELL = "#303030"
# Low -> high activity, deep to bright teal (GitHub-style ramp).
TEAL_RAMP = ["#173e3b", "#1c6f68", "#20B2AA", "#7fd8d2"]

_WEEKDAY_LABEL = {1: "Mon", 3: "Wed", 5: "Fri"}  # rows are Sun..Sat (0..6)
_GUTTER = "    "  # 4 chars, matches the weekday-label column width


def _format_count(count: int) -> str:
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k"
    if count < 1_000_000_000:
        return f"{count / 1_000_000:.1f}M"
    return f"{count / 1_000_000_000:.1f}B"


def _thresholds(values: list[int]) -> list[int]:
    """Quartile boundaries of the nonzero values, for four activity levels."""
    nonzero = sorted(v for v in values if v > 0)
    if not nonzero:
        return [1, 1, 1]
    return [
        nonzero[min(len(nonzero) - 1, int(0.25 * len(nonzero)))],
        nonzero[min(len(nonzero) - 1, int(0.50 * len(nonzero)))],
        nonzero[min(len(nonzero) - 1, int(0.75 * len(nonzero)))],
    ]


def _level_color(count: int, thresholds: list[int]) -> str:
    if count <= 0:
        return EMPTY_CELL
    if count <= thresholds[0]:
        return TEAL_RAMP[0]
    if count <= thresholds[1]:
        return TEAL_RAMP[1]
    if count <= thresholds[2]:
        return TEAL_RAMP[2]
    return TEAL_RAMP[3]


def _heatmap(stats: FleetStats, today: date, weeks: int = 30) -> Group:
    """A weekday x week grid of daily message activity, teal by intensity."""
    end_sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    start_sunday = end_sunday - timedelta(weeks=weeks - 1)

    columns = [start_sunday + timedelta(weeks=c) for c in range(weeks)]
    grid: list[list[int | None]] = [[None] * weeks for _ in range(7)]
    values: list[int] = []
    for col, col_start in enumerate(columns):
        for row in range(7):
            day = col_start + timedelta(days=row)
            if day > today:
                continue
            entry = stats.days.get(day)
            count = entry.messages if entry else 0
            grid[row][col] = count
            values.append(count)
    thresholds = _thresholds(values)

    # Month labels, placed at the first column of each new month.
    month_line = Text(_GUTTER)
    slots = [" "] * weeks
    previous_month = None
    last_label_col = -3
    for col, col_start in enumerate(columns):
        if col_start.month != previous_month:
            previous_month = col_start.month
            if col - last_label_col < 3:
                continue  # keep labels from colliding at ~4-week spacing
            label = col_start.strftime("%b")
            for offset, char in enumerate(label):
                if col + offset < weeks:
                    slots[col + offset] = char
            last_label_col = col
    month_line.append("".join(slots), style=MUTED)

    lines = [month_line]
    for row in range(7):
        line = Text()
        label = _WEEKDAY_LABEL.get(row, "")
        line.append(f"{label:<3} ", style=MUTED)
        for col in range(weeks):
            cell = grid[row][col]
            if cell is None:
                line.append(" ")
            else:
                line.append("■", style=_level_color(cell, thresholds))
        lines.append(line)

    legend = Text(_GUTTER)
    legend.append("less ", style=MUTED)
    legend.append("■", style=EMPTY_CELL)
    for color in TEAL_RAMP:
        legend.append("■", style=color)
    legend.append(" more", style=MUTED)
    lines.append(Text())
    lines.append(legend)
    return Group(*lines)


def _summary(stats: FleetStats, today: date) -> Group:
    lines = []

    def stat(label: str, value: str) -> Text:
        text = Text()
        text.append(f"{value:>10}  ", style=f"bold {TEAL}")
        text.append(label, style="default")
        return text

    busiest = stats_mod.busiest_day(stats)
    lines.append(stat("total messages", _format_count(stats.total_messages)))
    lines.append(stat("sessions", _format_count(stats.total_sessions)))
    lines.append(stat("tool calls", _format_count(stats.total_tool_calls)))
    lines.append(stat("active days", str(stats_mod.active_days(stats))))
    lines.append(stat("longest streak (days)", str(stats_mod.longest_streak(stats))))
    if busiest is not None:
        lines.append(
            stat(
                f"busiest day  ({busiest.day.isoformat()})",
                _format_count(busiest.messages),
            )
        )
    if stats.first_day is not None:
        span = Text()
        span.append(
            f"{stats.first_day.strftime('%b %-d, %Y')} - {today.strftime('%b %-d, %Y')}",
            style=MUTED,
        )
        lines.append(Text())
        lines.append(span)
    return Group(*lines)


def _top_models(stats: FleetStats) -> Group:
    ranked = stats_mod.top_models(stats, limit=5)
    if not ranked:
        return Group(Text("no model usage recorded", style=MUTED))
    peak = ranked[0][1] or 1
    lines = [Text("Top models by total tokens", style=f"bold {TEAL}"), Text()]
    for name, total in ranked:
        filled = max(1, int(round(total / peak * 24)))
        line = Text()
        line.append(f"{name:<16} ", style="default")
        line.append("█" * filled, style=TEAL)
        line.append("░" * (24 - filled), style=EMPTY_CELL)
        line.append(f"  {_format_count(total)}", style=MUTED)
        lines.append(line)
    return Group(*lines)


def _accounts(stats: FleetStats) -> Group:
    lines = [Text("Per account", style=f"bold {TEAL}"), Text()]
    for name, (sessions, messages) in stats.per_account.items():
        line = Text()
        line.append(f"{name:<8} ", style=f"bold {TEAL}")
        line.append(
            f"{_format_count(sessions)} sessions · {_format_count(messages)} messages",
            style="default",
        )
        lines.append(line)
    return Group(*lines)


def render_heatmap(stats: FleetStats, today: date, weeks: int = 30) -> Group:
    """Public: the daily-activity heatmap, for embedding in the main view."""
    return _heatmap(stats, today, weeks)


def render_heatmaps_row(
    provider_stats: list[tuple[str, FleetStats]], today: date, weeks: int = 30
) -> RichTable:
    """Each provider's heatmap side by side, labeled, as separate blocks."""
    grid = RichTable.grid(padding=(0, 6))
    for _ in provider_stats:
        grid.add_column()
    cells = [
        Group(
            Text(label, style=f"bold {TEAL}"),
            Text(),
            _heatmap(stats, today, weeks),
        )
        for label, stats in provider_stats
    ]
    grid.add_row(*cells)
    return grid


def build_provider_stats(accounts) -> list[tuple[str, FleetStats]]:
    """Per-provider FleetStats: [("Claude", ...), ("Codex", ...)] for those present."""
    from . import codex_stats

    result = []
    claude = [a for a in accounts if a.provider != "codex"]
    codex = [a for a in accounts if a.provider == "codex"]
    if claude:
        result.append(("Claude", stats_mod.build_fleet_stats(claude)))
    if codex:
        result.append(("Codex", codex_stats.build_fleet_stats(codex)))
    return result


def render_stats_table(provider_stats: list[tuple[str, FleetStats]]) -> RichTable:
    """Per-provider totals as an aligned grid (columns line up across rows)."""
    grid = RichTable.grid(padding=(0, 2))
    grid.add_column(style=f"bold {TEAL}")  # provider name
    for _ in range(5):  # value + label for each of five metrics
        grid.add_column(justify="right", style=f"bold {TEAL}")
        grid.add_column(justify="left")

    for label, stats in provider_stats:
        busiest = stats_mod.busiest_day(stats)
        grid.add_row(
            label,
            _format_count(stats.total_messages),
            "messages",
            _format_count(stats.total_sessions),
            "sessions",
            str(stats_mod.active_days(stats)),
            "active days",
            str(stats_mod.longest_streak(stats)),
            "day streak",
            _format_count(busiest.messages) if busiest else "-",
            f"busiest ({busiest.day.isoformat()})" if busiest else "busiest",
        )
    return grid


def render_summary_line(stats: FleetStats, today: date, label: str | None = None) -> Text:
    """Public: a single-line totals summary, for the main view's stats panel."""
    line = Text()
    if label:
        line.append(f"{label:<7}", style=f"bold {TEAL}")

    def segment(value: str, label: str) -> None:
        line.append(f"{value} ", style=f"bold {TEAL}")
        line.append(label, style="default")
        line.append("    ", style="default")

    segment(_format_count(stats.total_messages), "messages")
    segment(_format_count(stats.total_sessions), "sessions")
    segment(str(stats_mod.active_days(stats)), "active days")
    segment(str(stats_mod.longest_streak(stats)), "day streak")
    busiest = stats_mod.busiest_day(stats)
    if busiest is not None:
        line.append(f"{_format_count(busiest.messages)} ", style=f"bold {TEAL}")
        line.append(f"busiest ({busiest.day.isoformat()})", style="default")
    return line


class StatsScreen(ModalScreen):
    """A read-only statistics overlay: heatmap, totals, and model breakdown."""

    CSS = f"""
    StatsScreen {{ align: center middle; }}
    #stats-box {{
        border: round {TEAL};
        border-title-color: {TEAL};
        width: 88;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: $background;
    }}
    #stats-box .section {{ height: auto; }}
    .spacer {{ height: 1; }}
    #stats-hint {{ color: #808080; height: 1; }}
    """

    BINDINGS = [
        ("s", "dismiss_stats", "Close"),
        ("escape", "dismiss_stats", "Close"),
        ("q", "dismiss_stats", "Close"),
    ]

    def __init__(self, accounts: list[Account], today: date) -> None:
        super().__init__()
        self._accounts = accounts
        self._today = today

    def compose(self) -> ComposeResult:
        provider_stats = build_provider_stats(self._accounts)
        with Container(id="stats-box") as box:
            box.border_title = "STATISTICS"
            with VerticalScroll():
                for index, (label, stats) in enumerate(provider_stats):
                    if index:
                        yield Static(classes="spacer")
                    yield Static(
                        Text(f"── {label} ──", style=f"bold {TEAL}"),
                        classes="section",
                    )
                    yield Static(_summary(stats, self._today), classes="section")
                    yield Static(classes="spacer")
                    yield Static(
                        Text("Daily activity (messages)", style=f"bold {TEAL}"),
                        classes="section",
                    )
                    yield Static(_heatmap(stats, self._today), classes="section")
                    yield Static(classes="spacer")
                    yield Static(_top_models(stats), classes="section")
                    yield Static(classes="spacer")
                    yield Static(_accounts(stats), classes="section")
            yield Static(Text("s / esc / q  close", style=MUTED), id="stats-hint")

    def action_dismiss_stats(self) -> None:
        self.dismiss()
