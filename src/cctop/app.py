"""The live Textual TUI.

Layout: a prominent USAGE panel at the top (the two accounts side by side, each
window a wide bar), the SESSIONS table below as the denser ledger, and a footer
with fleet totals, the limits-fetch age, and key hints.

Palette: teal accent (#20B2AA) on bar fills, section borders, and the active-
window marker; monochrome terminal otherwise. Usage bars stay teal even when
maxed (fullness carries the signal, not color); red is reserved for a blocked
or dead session. This mirrors the house teal-on-mono look (to be matched
exactly to sleuth-tui later).

Cost safety: the session table refreshes every second from local files only
(free), while the usage-limits panel is refetched on a slower throttle (GET
/api/oauth/usage, also free) with a visible "N ago" stamp and a key to refresh
now. Both polls run in thread workers so neither blocks the UI.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from rich.console import Group
from rich.table import Table as RichTable
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Container
from textual.timer import Timer
from textual.widgets import DataTable, Rule, Static

from .cli import (
    _format_age,
    _format_context,
    _format_cost,
    _format_model,
    _format_reset,
    _format_tokens,
    _short_cwd,
    _tier_label,
)
from .collect import Account
from .models import AccountLimits, LimitWindow, SessionState
from .monitor import FleetMonitor

TEAL = "#20B2AA"
DIM = "grey37"
MUTED = "grey50"
BAR_WIDTH = 22

_STATUS_STYLE = {
    "idle": TEAL,
    "shell": "white",
    "generating": TEAL,
    "waiting_permission": "red",
    "stale": DIM,
    "dead": "red",
    "unknown": DIM,
}

_COLUMNS = ("acct", "name", "status", "model", "cwd", "ctx", "tokens", "cost", "age")


def _fit(text: str, width: int) -> str:
    """Pad to width, or truncate with an ellipsis if longer."""
    if len(text) <= width:
        return f"{text:<{width}}"
    return text[: width - 1] + "…"


def _bar(fraction: float, width: int) -> Text:
    """A teal fill over a grey track. Fullness carries the signal, not color, so
    a maxed window reads as a full teal bar rather than switching to red."""
    filled = max(0, min(width, int(round(fraction * width))))
    bar = Text()
    bar.append("█" * filled, style=TEAL)
    bar.append("░" * (width - filled), style=DIM)
    return bar


def _gauge_line(window: LimitWindow, now: datetime, bar_width: int, label_width: int) -> Text:
    line = Text()
    line.append(_fit(window.label, label_width) + " ", style="default")
    if not window.has_data:
        # A window with no reading yet: an empty track and a muted dash, so it
        # reads as "not started" rather than a real 0%.
        line.append("░" * bar_width, style=DIM)
        line.append("   —", style=MUTED)
        return line
    line.append(_bar(window.used_fraction, bar_width))
    line.append(f" {window.percent:>3.0f}%", style=TEAL)
    line.append(" ●" if window.is_active else "  ", style=TEAL)
    line.append(f" {_format_reset(window.resets_at, now)}", style=MUTED)
    return line


def _account_block(
    account: AccountLimits, now: datetime, bar_width: int, label_width: int
) -> Group:
    header = Text()
    header.append(account.account, style=f"bold {TEAL}")
    tier = _tier_label(account.tier)
    if tier:
        header.append("   ", style="default")
        header.append(tier, style=MUTED)

    lines = [header]
    if account.source != "api":
        lines.append(Text(account.error or "no limit data", style=MUTED))
    else:
        lines.extend(_gauge_line(window, now, bar_width, label_width) for window in account.windows)
    return Group(*lines)


# Rough character budget for one account block (label + bar + % + reset), used
# to decide how many fit per row before wrapping to the next.
_USAGE_BLOCK = 40


def _render_usage(
    limits: list[AccountLimits], now: datetime, width: int | None = None
) -> RichTable:
    """Account blocks laid out in a grid that wraps to more rows as needed.

    How many share a row is chosen from the available width (so it reflows on
    resize) capped at the account count; bars widen when few share a row. Past
    that many accounts, the rest wrap onto further rows rather than overflowing.
    """
    count = max(1, len(limits))
    if not width or width <= 0:
        per_row = count if count <= 3 else 3
    else:
        per_row = max(1, min(count, width // _USAGE_BLOCK))

    label_width = 13 if per_row <= 2 else 11 if per_row == 3 else 9
    pad = 8 if per_row <= 2 else 6 if per_row == 3 else 4
    if width and width > 0:
        # Size the bar to fill each column (label + bar + ~15 for %/marker/reset),
        # so bars never overrun the column and wrap.
        column = (width - (per_row - 1) * pad) // per_row
        bar_width = max(6, min(BAR_WIDTH, column - label_width - 15))
    else:
        bar_width = BAR_WIDTH if per_row <= 2 else 12 if per_row == 3 else 9

    grid = RichTable.grid(padding=(0, pad))
    for _ in range(per_row):
        grid.add_column()

    blank = Text("")
    rows = -(-count // per_row)  # ceil
    for row in range(rows):
        if row:
            grid.add_row(*([blank] * per_row))  # a blank line between rows
        chunk = list(limits[row * per_row : (row + 1) * per_row])
        cells: list = [_account_block(a, now, bar_width, label_width) for a in chunk]
        cells += [blank] * (per_row - len(cells))  # pad the last partial row
        grid.add_row(*cells)
    return grid


def _format_next_refresh(fetched: datetime | None, interval: float, now: datetime) -> str:
    """Countdown to the next usage refresh, e.g. "next in 2m14s" or "refreshing"."""
    if fetched is None:
        return "refreshing"
    remaining = interval - (now - fetched).total_seconds()
    if remaining <= 1:
        return "refreshing"
    if remaining < 60:
        return f"next in {int(remaining)}s"
    minutes, seconds = divmod(int(remaining), 60)
    return f"next in {minutes}m{seconds:02d}s"


class CctopApp(App):
    """nvtop-style live view of Claude Code sessions and usage limits."""

    # Pin the terminal tab title so it doesn't flicker as the view refreshes.
    TITLE = "cctop"
    SUB_TITLE = ""

    CSS = f"""
    Screen {{ background: $background; }}
    #topbar {{ height: 1; padding: 0 1; }}
    #usage-box {{
        border: round {TEAL};
        border-title-color: {TEAL};
        height: auto;
        padding: 0 1;
    }}
    #sessions-box {{
        border: round {TEAL};
        border-title-color: {TEAL};
        height: 2fr;
        padding: 0 1;
    }}
    #sessions-box DataTable {{ height: 1fr; }}
    #sessions-box Rule {{ color: {TEAL} 30%; margin: 0; height: 1; }}
    #detail {{ height: auto; }}
    #stats-box {{
        border: round {TEAL};
        border-title-color: {TEAL};
        height: 3fr;
        padding: 0 1;
    }}
    #footer {{ height: 1; padding: 0 1; color: #808080; }}
    DataTable {{ background: transparent; }}
    DataTable > .datatable--header {{ color: {TEAL}; text-style: bold; background: transparent; }}
    DataTable > .datatable--cursor {{ background: {TEAL} 25%; }}
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_limits", "Refresh limits"),
        ("R", "refresh_token", "Refresh token"),
        ("a", "add_account", "Add account"),
        ("s", "stats", "Stats"),
        ("slash", "search", "Search"),
        ("comma", "settings", "Settings"),
    ]

    def __init__(
        self,
        accounts: list[Account],
        limits_interval: float = 180.0,
        heatmap_weeks: int = 26,
    ) -> None:
        super().__init__()
        self.monitor = FleetMonitor(accounts)
        self._limits_interval = limits_interval
        self._heatmap_weeks = heatmap_weeks
        self._limits_timer: Timer | None = None
        self._states_by_id: dict[str, SessionState] = {}
        self._selected_session_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with Container(id="usage-box"):
            yield Static(id="usage")
        with Container(id="sessions-box"):
            yield DataTable(id="sessions", cursor_type="row")
            yield Rule(line_style="dashed")
            yield Static(id="detail")
        with Container(id="stats-box"):
            yield Static(id="stats")
        yield Static(id="footer")

    def on_mount(self) -> None:
        self.query_one("#usage-box", Container).border_title = "USAGE"
        self.query_one("#sessions-box", Container).border_title = "SESSIONS"
        self.query_one("#stats-box", Container).border_title = "STATS"

        table = self.query_one("#sessions", DataTable)
        for column in _COLUMNS:
            table.add_column(column, key=column)

        self.query_one("#usage", Static).update(Text("fetching limits...", style=MUTED))
        self.query_one("#stats", Static).update(Text("loading stats...", style=MUTED))
        self.set_interval(1.0, self._tick_sessions)
        self._limits_timer = self.set_interval(self._limits_interval, self._tick_limits)
        self.set_interval(300.0, self._tick_stats)
        self._tick_sessions()
        self.action_refresh_limits()
        self._tick_stats()

    @work(thread=True, exclusive=True, group="stats")
    def _tick_stats(self) -> None:
        from .stats_screen import (
            build_provider_stats,
            render_heatmaps_row,
            render_stats_table,
        )

        today = date.today()
        provider_stats = build_provider_stats(self.monitor.accounts)

        parts: list = []
        if provider_stats:
            parts.append(render_stats_table(provider_stats))
            parts.append(Text())
            parts.append(render_heatmaps_row(provider_stats, today, weeks=self._heatmap_weeks))
        content = Group(*parts) if parts else Text("no stats", style=MUTED)

        self.call_from_thread(lambda: self.query_one("#stats", Static).update(content))

    # -- session polling (fast, free, file-only) -------------------------------

    @work(thread=True, exclusive=True, group="sessions")
    def _tick_sessions(self) -> None:
        now = datetime.now(timezone.utc)
        states = self.monitor.poll_sessions(now)
        self.call_from_thread(self._render_sessions, states, now)

    def _render_sessions(self, states: list[SessionState], now: datetime) -> None:
        self._render_topbar(now)
        self._states_by_id = {s.session.session_id: s for s in states}

        table = self.query_one("#sessions", DataTable)
        table.clear()
        selected_index = None
        for index, state in enumerate(states):
            style = _STATUS_STYLE.get(state.status.value, "white")
            session_id = state.session.session_id
            table.add_row(
                state.account,
                state.session.name or session_id[:8],
                Text(state.status.value, style=style),
                _format_model(state.model),
                _short_cwd(state.session.cwd),
                _format_context(state),
                _format_tokens(state.totals.total_tokens),
                _format_cost(state.totals.cost_usd),
                _format_age(state.last_activity, now),
                key=session_id,
            )
            if session_id == self._selected_session_id:
                selected_index = index

        # Keep the cursor on the same session across the 1s rebuild instead of
        # snapping back to the top.
        if selected_index is not None:
            table.move_cursor(row=selected_index, animate=False)

        self._render_detail(now)
        self._render_footer(states, now)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._selected_session_id = event.row_key.value
        self._render_detail(datetime.now(timezone.utc))

    def _render_detail(self, now: datetime) -> None:
        selected = self._selected_session_id
        state = self._states_by_id.get(selected) if selected is not None else None
        widget = self.query_one("#detail", Static)
        if state is None:
            widget.update(Text("", style=MUTED))
            return

        session = state.session
        totals = state.totals
        ctx = state.context
        ctx_text = f"{ctx.used_tokens:,} / {ctx.window_tokens:,}" if ctx is not None else "-"
        gap = ("   ", "default")

        title_parts = [
            (session.name or session.session_id[:8], f"bold {TEAL}"),
            gap,
            (state.account, MUTED),
            gap,
            (state.status.value, MUTED),
            gap,
            (f"pid {session.pid}", MUTED),
        ]
        if session.version:
            title_parts += [gap, (f"v{session.version}", MUTED)]
        title_parts += [gap, (_short_cwd(session.cwd), MUTED)]

        tokens = (
            f"in {_format_tokens(totals.input_tokens)} · "
            f"out {_format_tokens(totals.output_tokens)} · "
            f"cache r {_format_tokens(totals.cache_read_tokens)} "
            f"w {_format_tokens(totals.cache_creation_tokens)}"
        )
        wide = ("      ", "default")
        usage_line = Text.assemble(
            ("tokens ", MUTED),
            (tokens, "default"),
            wide,
            ("cost ", MUTED),
            (_format_cost(totals.cost_usd), "default"),
            wide,
            ("context ", MUTED),
            (ctx_text, "default"),
        )

        widget.update(Group(Text.assemble(*title_parts), usage_line))

    def _render_topbar(self, now: datetime) -> None:
        names = "  ".join(account.name for account in self.monitor.accounts)
        clock = now.astimezone().strftime("%H:%M:%S")
        bar = RichTable.grid(expand=True)
        bar.add_column(justify="left")
        bar.add_column(justify="right")
        left = Text.assemble(
            ("cctop", f"bold {TEAL}"),
            ("     ", "default"),
            (names, MUTED),
        )
        bar.add_row(left, Text(clock, style=MUTED))
        self.query_one("#topbar", Static).update(bar)

    def _render_footer(self, states: list[SessionState], now: datetime) -> None:
        total_tokens = sum(s.totals.total_tokens for s in states)
        total_cost = sum(s.totals.cost_usd or 0.0 for s in states)
        fetched = self.monitor.limits_fetched_at
        age = f"updated {_format_age(fetched, now)} ago" if fetched else "no data yet"
        nxt = _format_next_refresh(fetched, self._limits_interval, now)
        footer = Text.assemble(
            (
                f"{len(states)} sessions · {_format_tokens(total_tokens)} tokens · "
                f"${total_cost:,.0f}",
                "default",
            ),
            (f"      usage {age} · {nxt}", MUTED),
            ("      / search · r refresh · R token · a add · s stats · , settings · q quit", MUTED),
        )
        self.query_one("#footer", Static).update(footer)

    # -- limits polling (slow, free, networked) --------------------------------

    @work(thread=True, exclusive=True, group="limits")
    def _tick_limits(self) -> None:
        now = datetime.now(timezone.utc)
        limits = self.monitor.poll_limits(now)
        self.call_from_thread(self._render_limits, limits, now)

    def action_stats(self) -> None:
        from .stats_screen import StatsScreen

        self.push_screen(StatsScreen(self.monitor.accounts, date.today()))

    def action_search(self) -> None:
        from .search_screen import SearchScreen

        self.push_screen(SearchScreen(self.monitor.accounts))

    def action_settings(self) -> None:
        from . import config as config_module
        from .collect import discover_accounts
        from .settings_screen import SettingsScreen, build_rows

        rows = build_rows(discover_accounts(), config_module.load_config())
        self.push_screen(SettingsScreen(self._limits_interval, self._heatmap_weeks, rows))

    def apply_settings(self) -> None:
        """Reload the config file and apply it live (called by the settings screen).

        Updates the refresh interval (rescheduling the timer + countdown), the
        heatmap range, and the visible account set, with no restart.
        """
        from datetime import timedelta

        from . import config as config_module
        from .collect import resolve_accounts

        config = config_module.load_config()
        self._limits_interval = config.limits_refresh_seconds(180.0)
        self._heatmap_weeks = config.heatmap_weeks(26)
        self.monitor.limits_interval = timedelta(seconds=self._limits_interval)
        self.monitor.accounts = resolve_accounts(config)

        self._reschedule_limits_timer()
        self.action_refresh_limits()
        self._tick_sessions()
        self._tick_stats()

    def action_add_account(self) -> None:
        """Provision an account and sign in, without leaving cctop.

        Suspends the dashboard to hand the terminal to two optional prompts
        (clone config from an existing account; set a shell alias) and the
        interactive login, then rediscovers accounts so the new one shows
        immediately. Everything is explicit and additive: nothing is aliased or
        cloned unless typed in, the clone is an allowlist (never credentials or
        state), and the login is scoped to the one new config dir.
        """
        from pathlib import Path

        from rich.console import Console

        from . import manage
        from .cli import resolve_config_dir, reusable_logged_out_dir, run_login
        from .collect import discover_accounts

        home = Path.home()
        console = Console()

        with self.suspend():
            claude = [a.name for a in discover_accounts() if a.provider == "claude"]
            console.print("[bold]Add a Claude account[/bold]")
            console.print(f"existing: {'  '.join(claude)}\n")

            source_spec = console.input(
                "Clone config (CLAUDE.md, settings, ...) from which account? [blank=none]: "
            ).strip()
            alias = console.input("Shell alias to set? [blank=none]: ").strip()

            plan = manage.plan_add(home, alias or None, reuse_dir=reusable_logged_out_dir(home))
            source_dir = resolve_config_dir(source_spec) if source_spec else None
            if source_spec and (source_dir is None or source_dir == plan.config_dir):
                console.print(f"[red]skipping clone: '{source_spec}' not usable[/red]")
                source_dir = None

            console.print(f"\nconfig dir {plan.config_dir}")
            for action in manage.ensure_config_dir(plan.config_dir):
                console.print(f"  + {action}")
            if source_dir is not None:
                for action in manage.copy_config(source_dir, plan.config_dir):
                    console.print(f"  + {action}")
            if alias:
                for action in manage.set_alias(plan):
                    console.print(f"  + {action}")
                console.print(f"[grey50]run `source ~/.zshrc` to use `{alias}` here[/grey50]")

            signed_in = run_login(plan.config_dir, console)
            console.input("\nPress Enter to return to cctop...")

        self.monitor.accounts = discover_accounts()
        self._tick_sessions()
        self.action_refresh_limits()
        state = "signed in" if signed_in else "set up (not signed in)"
        self.notify(f"{plan.config_dir.name} {state}", title="add account", timeout=6)

    def action_refresh_limits(self) -> None:
        self._force_limits()

    def action_refresh_token(self) -> None:
        """Ask the owner binary to renew each Claude account's expired token.

        cctop writes no credential: the delegated `claude auth status` run does
        the refresh. Runs off the UI thread since it shells out per account.
        """
        self.notify("Refreshing tokens via claude auth status...", timeout=3)
        self._refresh_token()

    @work(thread=True, exclusive=True, group="token")
    def _refresh_token(self) -> None:
        now = datetime.now(timezone.utc)
        results, limits = self.monitor.refresh_credentials(now)

        summary = "   ".join(f"{r.account}: {r.message}" for r in results)
        summary = summary or "no Claude accounts to refresh"
        any_failed = any(not r.ok for r in results)

        self.call_from_thread(self._render_limits, limits, now)
        self.call_from_thread(
            lambda: self.notify(
                summary,
                title="token refresh",
                severity="warning" if any_failed else "information",
                timeout=6,
            )
        )

    @work(thread=True, exclusive=True, group="limits")
    def _force_limits(self) -> None:
        now = datetime.now(timezone.utc)
        limits = self.monitor.force_refresh_limits(now)
        self.call_from_thread(self._render_limits, limits, now)
        # Reset the periodic timer so the next auto-refresh is a full interval
        # away, keeping the footer countdown honest after a manual refresh.
        self.call_from_thread(self._reschedule_limits_timer)

    def _reschedule_limits_timer(self) -> None:
        if self._limits_timer is not None:
            self._limits_timer.stop()
        self._limits_timer = self.set_interval(self._limits_interval, self._tick_limits)

    def _render_limits(self, limits: list[AccountLimits], now: datetime) -> None:
        usage = self.query_one("#usage", Static)
        width = usage.size.width or self.size.width
        content = _render_usage(limits, now, width) if limits else Text("no accounts", style=MUTED)
        usage.update(content)

    def on_resize(self, event) -> None:
        # Re-lay the usage panel to the new width so accounts reflow/wrap live,
        # not just on the next limits poll.
        if self.monitor.limits:
            self._render_limits(self.monitor.limits, datetime.now(timezone.utc))
