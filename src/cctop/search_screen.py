"""The history-search screen: live search across every account's transcripts.

Opened with `/` from the main view. A debounced input drives ripgrep-backed
searches in a background thread (stale results are discarded, so fast typing
never shows an old query's hits). Results are grouped by session and tagged
with the owning account; the preview pane shows the matching messages with the
query highlighted. Enter resumes the selected session under its own account
via the owner binary. Read-only apart from that explicit hand-off.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import datetime, timezone

from rich.console import Group
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import DataTable, Input, Rule, Static

from . import histsearch
from .app import _fit
from .cli import _format_age
from .collect import Account

TEAL = "#20B2AA"
DIM = "grey37"
MUTED = "grey50"

_DEBOUNCE_SECONDS = 0.25
_PREVIEW_HITS = 4
# Fixed column budgets so title never pushes hits/age off-screen; project and
# title are clipped with an ellipsis instead of forcing a horizontal scroll.
_PROJECT_WIDTH = 36
_TITLE_WIDTH = 56


def _highlight(snippet: str, query: str, regex: bool) -> Text:
    """The snippet with every query occurrence emphasized in teal."""
    text = Text(snippet, style="default")
    try:
        pattern = re.compile(query if regex else re.escape(query), re.IGNORECASE)
    except re.error:
        return text
    for found in pattern.finditer(snippet):
        if found.start() == found.end():
            break
        text.stylize(f"bold {TEAL}", found.start(), found.end())
    return text


class SearchScreen(ModalScreen):
    """A full-screen overlay: query input, session results, match preview."""

    CSS = f"""
    SearchScreen {{ align: center middle; }}
    #search-box {{
        border: round {TEAL};
        border-title-color: {TEAL};
        width: 90%;
        height: 90%;
        padding: 0 1;
        background: $background;
    }}
    #search-input {{ border: none; height: 1; padding: 0; background: transparent; }}
    #search-status {{ height: 1; color: #808080; }}
    #search-results {{ height: 1fr; background: transparent; }}
    #search-results > .datatable--header {{
        color: {TEAL}; text-style: bold; background: transparent;
    }}
    #search-results > .datatable--cursor {{ background: {TEAL} 25%; }}
    #search-box Rule {{ color: {TEAL} 30%; margin: 0; height: 1; }}
    #search-preview {{ height: auto; max-height: 12; }}
    #search-hint {{ height: 1; color: #808080; }}
    """

    # Up/down and enter are screen bindings (not table focus) so the results
    # can be steered and resumed while the query input keeps keyboard focus,
    # the way a search-as-you-type picker is expected to feel.
    BINDINGS = [
        ("escape", "close", "Close"),
        ("ctrl+r", "toggle_regex", "Regex"),
        ("up", "move_cursor(-1)", "Up"),
        ("down", "move_cursor(1)", "Down"),
    ]

    def __init__(self, accounts: list[Account]) -> None:
        super().__init__()
        self._accounts = accounts
        self._regex = False
        self._timer: Timer | None = None
        self._result: histsearch.SearchResult | None = None
        self._matches: list[histsearch.SessionMatch] = []

    def compose(self) -> ComposeResult:
        with Container(id="search-box") as box:
            box.border_title = "SEARCH"
            yield Input(placeholder="search all history...", id="search-input")
            yield Static(id="search-status")
            yield DataTable(id="search-results", cursor_type="row")
            yield Rule(line_style="dashed")
            yield Static(id="search-preview")
            yield Static(
                Text("enter resume · ctrl+r regex · esc close", style=MUTED),
                id="search-hint",
            )

    def on_mount(self) -> None:
        table = self.query_one("#search-results", DataTable)
        table.add_column("acct", key="acct")
        table.add_column("project", key="project", width=_PROJECT_WIDTH)
        table.add_column("title", key="title", width=_TITLE_WIDTH)
        table.add_column("hits", key="hits")
        table.add_column("last", key="last")
        self.query_one("#search-input", Input).focus()
        self._set_status(Text("type to search every account's history", style=MUTED))

    # -- query handling (debounced, stale-result safe) -------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_timer(_DEBOUNCE_SECONDS, self._start_search)

    def action_toggle_regex(self) -> None:
        self._regex = not self._regex
        self._start_search()

    def action_move_cursor(self, delta: int) -> None:
        table = self.query_one("#search-results", DataTable)
        if table.row_count == 0:
            return
        row = max(0, min(table.row_count - 1, (table.cursor_row or 0) + delta))
        table.move_cursor(row=row, animate=False)
        self._render_preview()

    def _start_search(self) -> None:
        query = self.query_one("#search-input", Input).value
        if len(query.strip()) < 2:
            self._apply_result(query, None, 0.0)
            return
        self._run_search(query, self._regex)

    @work(thread=True, exclusive=True, group="history-search")
    def _run_search(self, query: str, regex: bool) -> None:
        started = time.monotonic()
        result = histsearch.search_history(query, self._accounts, regex=regex)
        elapsed = time.monotonic() - started
        self.app.call_from_thread(self._apply_result, query, result, elapsed)

    def _apply_result(
        self,
        query: str,
        result: histsearch.SearchResult | None,
        elapsed: float,
    ) -> None:
        # A slower search for an older query must never overwrite the current
        # one: only apply when the input still shows the query that ran.
        if query != self.query_one("#search-input", Input).value:
            return
        self._result = result
        self._matches = result.sessions if result else []
        self._render_results(query, elapsed)

    # -- rendering -------------------------------------------------------------

    def _set_status(self, content: Text) -> None:
        self.query_one("#search-status", Static).update(content)

    def _render_results(self, query: str, elapsed: float) -> None:
        table = self.query_one("#search-results", DataTable)
        table.clear()

        result = self._result
        if result is None:
            self._set_status(Text("type to search every account's history", style=MUTED))
            self.query_one("#search-preview", Static).update(Text(""))
            return

        now = datetime.now(timezone.utc)
        for index, match in enumerate(self._matches):
            table.add_row(
                Text(match.account, style=TEAL),
                Text(_fit(match.project, _PROJECT_WIDTH), style=MUTED),
                _fit(match.title, _TITLE_WIDTH),
                str(len(match.hits)),
                Text(_format_age(match.last_timestamp, now), style=MUTED),
                key=str(index),
            )

        status = Text()
        status.append(
            f"{len(result.sessions)} sessions · {result.total_hits} matches",
            style="default",
        )
        status.append(f" · {result.backend} · {elapsed * 1000:.0f}ms", style=MUTED)
        if result.truncated:
            status.append(" · truncated", style=MUTED)
        if self._regex:
            status.append("  [regex]", style=TEAL)
        self._set_status(status)
        self._render_preview(query)

    def _render_preview(self, query: str | None = None) -> None:
        widget = self.query_one("#search-preview", Static)
        match = self._selected_match()
        if match is None:
            widget.update(Text(""))
            return
        query = query if query is not None else self.query_one("#search-input", Input).value

        now = datetime.now(timezone.utc)
        lines: list = []
        for hit in match.hits[:_PREVIEW_HITS]:
            header = Text()
            header.append(hit.role, style=TEAL if hit.role == "user" else "default")
            header.append(f" · {_format_age(hit.timestamp, now)} ago", style=MUTED)
            lines.append(header)
            lines.append(_highlight(hit.snippet, query, self._regex))
        remaining = len(match.hits) - _PREVIEW_HITS
        if remaining > 0:
            lines.append(Text(f"... {remaining} more matches", style=MUTED))
        widget.update(Group(*lines))

    def _selected_match(self) -> histsearch.SessionMatch | None:
        table = self.query_one("#search-results", DataTable)
        if not self._matches or table.cursor_row is None or table.cursor_row < 0:
            return None
        if table.cursor_row >= len(self._matches):
            return None
        return self._matches[table.cursor_row]

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._render_preview()

    # -- resume ----------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        match = self._selected_match()
        if match is not None:
            self._resume(match)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        match = self._selected_match()
        if match is not None:
            self._resume(match)

    def _resume(self, match: histsearch.SessionMatch) -> None:
        """Hand the terminal to the owner binary to resume this session.

        The plan pins the session's own account (CLAUDE_CONFIG_DIR) and cwd;
        cctop only launches the owner and takes the terminal back when it exits.
        """
        plan = histsearch.resume_plan(match, self._accounts)
        if plan is None:
            self.notify(
                f"{match.provider} binary not found on PATH",
                title="resume",
                severity="warning",
                timeout=5,
            )
            return

        with self.app.suspend():
            env = dict(os.environ, **plan.env_extra)
            try:
                subprocess.run(plan.argv, env=env, cwd=plan.cwd)
            except (OSError, KeyboardInterrupt):
                pass
        self.notify(f"returned from {match.account} session", title="resume", timeout=4)

    def action_close(self) -> None:
        self.dismiss()
