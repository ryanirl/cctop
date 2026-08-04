"""The history-search screen: live search across every account's transcripts.

Opened with `/` from the main view, or standalone via `cctop search`. A
debounced input drives ripgrep-backed searches in a background thread (stale
results are discarded, so fast typing never shows an old query's hits).
Results are grouped by session and tagged with the owning account; the preview
pane shows the matching messages with the query highlighted. Enter opens the
transcript viewer to read the conversation before resuming it. Read-only.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Group
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import DataTable, Input, Rule, Static

from . import histsearch
from .app import _fit
from .cli import _format_age, _format_model
from .collect import Account

TEAL = "#20B2AA"
DIM = "grey37"
MUTED = "grey50"

_DEBOUNCE_SECONDS = 0.25
_PREVIEW_HITS = 4
# Fixed column budgets so title never pushes the trailing columns off-screen;
# project and title are clipped with an ellipsis instead of forcing a
# horizontal scroll.
_PROJECT_WIDTH = 24
_TITLE_WIDTH = 34
_MODEL_WIDTH = 10


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
    #search-layout {{
        width: 90%;
        height: 90%;
        background: $background;
    }}
    #search-bar, #path-bar {{
        border: round {TEAL};
        border-title-color: {TEAL};
        height: 3;
        padding: 0 1;
    }}
    #path-bar {{ border: round {TEAL} 45%; border-title-color: {TEAL}; }}
    #search-layout Input {{ border: none; height: 1; padding: 0; background: transparent; }}
    #results-box {{
        border: round {TEAL};
        border-title-color: {TEAL};
        height: 1fr;
        padding: 0 1;
    }}
    #search-status {{ height: 1; color: #808080; }}
    #search-results {{ height: 1fr; background: transparent; }}
    #search-results > .datatable--header {{
        color: {TEAL}; text-style: bold; background: transparent;
    }}
    #search-results > .datatable--cursor {{ background: {TEAL} 25%; }}
    #search-results.unselected > .datatable--cursor {{ background: transparent; }}
    #results-box Rule {{ color: {TEAL} 30%; margin: 0; height: 1; }}
    #search-preview {{ height: auto; max-height: 12; }}
    #search-hint {{ height: 1; color: #808080; padding: 0 1; }}
    """

    # Up/down and enter are screen bindings (not table focus) so the results
    # can be steered and opened while the query input keeps keyboard focus,
    # the way a search-as-you-type picker is expected to feel. The ctrl keys
    # are priority bindings because the focused Input would otherwise consume
    # them (ctrl+d is its delete-right).
    BINDINGS = [
        ("escape", "close", "Close"),
        Binding("ctrl+r", "toggle_regex", "Regex", priority=True),
        Binding("ctrl+d", "toggle_dir", "This dir only", priority=True),
        ("up", "move_cursor(-1)", "Up"),
        ("down", "move_cursor(1)", "Down"),
    ]

    def __init__(
        self,
        accounts: list[Account],
        initial_query: str = "",
        regex: bool = False,
        within: Path | None = None,
        path_filter: str = "",
        standalone: bool = False,
    ) -> None:
        super().__init__()
        self._accounts = accounts
        self._initial_query = initial_query
        self._initial_path = path_filter
        self._regex = regex
        # The directory scope: None searches everywhere. ctrl+d toggles it back
        # and forth against the default (--dir when given, else the directory
        # cctop was launched from).
        self._within = within
        self._within_default = within if within is not None else Path.cwd()
        self._standalone = standalone
        self._timer: Timer | None = None
        self._result: histsearch.SearchResult | None = None
        self._matches: list[histsearch.SessionMatch] = []
        # One cursor: while typing in a bar no row is selected, so enter never
        # opens a session by accident. Down (or a click) selects; up past the
        # first row returns to the bar-only state.
        self._row_selected = False

    def compose(self) -> ComposeResult:
        with Container(id="search-layout"):
            with Container(id="search-bar") as bar:
                bar.border_title = "SEARCH"
                yield Input(placeholder="search all history...", id="search-input")
            with Container(id="path-bar") as bar:
                bar.border_title = "PATH"
                yield Input(
                    placeholder="filter by path (e.g. cctop, ~/master/interp)...",
                    id="path-input",
                )
            with Container(id="results-box") as box:
                box.border_title = "RESULTS"
                yield Static(id="search-status")
                yield DataTable(id="search-results", cursor_type="row")
                yield Rule(line_style="dashed")
                yield Static(id="search-preview")
            yield Static(
                Text(
                    "down select · enter view transcript · tab path filter · "
                    "ctrl+r regex · ctrl+d this dir · esc close",
                    style=MUTED,
                ),
                id="search-hint",
            )

    def on_mount(self) -> None:
        table = self.query_one("#search-results", DataTable)
        table.add_column("acct", key="acct")
        table.add_column("prov", key="prov")
        table.add_column("●", key="live")
        table.add_column("project", key="project", width=_PROJECT_WIDTH)
        table.add_column("title", key="title", width=_TITLE_WIDTH)
        table.add_column("model", key="model", width=_MODEL_WIDTH)
        table.add_column("turns", key="turns")
        table.add_column("hits", key="hits")
        table.add_column("start", key="start")
        table.add_column("last", key="last")

        search_input = self.query_one("#search-input", Input)
        search_input.focus()
        if self._initial_path:
            self.query_one("#path-input", Input).value = self._initial_path
        if self._initial_query:
            # Setting the value fires Input.Changed, which debounces into the
            # first search, so a query passed on the CLI behaves exactly as if
            # it had been typed.
            search_input.value = self._initial_query
        elif not self._initial_path:
            # Nothing pre-filled fires no Input.Changed, so open directly on
            # the recent-session browse listing.
            self._start_search()

    # -- query handling (debounced, stale-result safe) -------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_timer(_DEBOUNCE_SECONDS, self._start_search)

    def action_toggle_regex(self) -> None:
        self._regex = not self._regex
        self._start_search()

    def action_toggle_dir(self) -> None:
        self._within = None if self._within is not None else self._within_default
        self._start_search()

    def _set_row_selected(self, selected: bool) -> None:
        self._row_selected = selected
        self.query_one("#search-results", DataTable).set_class(not selected, "unselected")

    def action_move_cursor(self, delta: int) -> None:
        table = self.query_one("#search-results", DataTable)
        if table.row_count == 0:
            return

        if not self._row_selected:
            if delta > 0:
                self._set_row_selected(True)
                table.move_cursor(row=0, animate=False)
                self._render_preview()
            return

        row = (table.cursor_row or 0) + delta
        if row < 0:
            self._set_row_selected(False)
            self._render_preview()
            return
        table.move_cursor(row=min(table.row_count - 1, row), animate=False)
        self._render_preview()

    def _inputs(self) -> tuple[str, str]:
        return (
            self.query_one("#search-input", Input).value,
            self.query_one("#path-input", Input).value,
        )

    def _start_search(self) -> None:
        query, path_filter = self._inputs()
        self._run_search(query, path_filter, self._regex, self._within)

    @work(thread=True, exclusive=True, group="history-search")
    def _run_search(
        self,
        query: str,
        path_filter: str,
        regex: bool,
        within: Path | None,
    ) -> None:
        started = time.monotonic()
        if len(query.strip()) >= 2:
            result = histsearch.search_history(
                query,
                self._accounts,
                regex=regex,
                within=within,
                path_filter=path_filter or None,
            )
        else:
            # An empty (or single-character) query browses recent sessions
            # instead, so the screen doubles as a session browser and a path
            # filter alone answers "what ran in this repo".
            result = histsearch.list_sessions(
                self._accounts,
                within=within,
                path_filter=path_filter or None,
            )
        elapsed = time.monotonic() - started
        self.app.call_from_thread(self._apply_result, query, path_filter, result, elapsed)

    def _apply_result(
        self,
        query: str,
        path_filter: str,
        result: histsearch.SearchResult | None,
        elapsed: float,
    ) -> None:
        # A slower search for older inputs must never overwrite the current
        # ones: only apply when both bars still show what was searched.
        if (query, path_filter) != self._inputs():
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
        # Fresh results always start unselected, so a stale selection can
        # never be opened by an enter meant for the input bar.
        self._set_row_selected(False)

        result = self._result
        if result is None:
            self._set_status(Text("type to search every account's history", style=MUTED))
            self.query_one("#search-preview", Static).update(Text(""))
            return

        now = datetime.now(timezone.utc)
        for index, match in enumerate(self._matches):
            table.add_row(
                Text(match.account, style=TEAL),
                Text(match.provider, style=MUTED),
                Text("●", style=TEAL) if match.live else Text(""),
                Text(_fit(match.project, _PROJECT_WIDTH), style=MUTED),
                _fit(match.title, _TITLE_WIDTH),
                Text(_fit(_format_model(match.model), _MODEL_WIDTH), style=MUTED),
                str(match.turns) if match.turns is not None else "-",
                str(len(match.hits)) if match.hits else "-",
                Text(_format_age(match.started, now), style=MUTED),
                Text(_format_age(match.last_timestamp, now), style=MUTED),
                key=str(index),
            )

        status = Text()
        status.append(f"{len(result.sessions)} sessions", style="default")
        if result.total_hits:
            status.append(f" · {result.total_hits} matches", style="default")
        status.append(f" · {result.backend} · {elapsed * 1000:.0f}ms", style=MUTED)
        if result.truncated:
            status.append(" · truncated", style=MUTED)
        if self._regex:
            status.append("  [regex]", style=TEAL)
        if self._within is not None:
            home = str(Path.home())
            shown = str(self._within)
            shown = "~" + shown[len(home) :] if shown.startswith(home) else shown
            status.append(f"  [in {shown}]", style=TEAL)
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
        card = Text()
        card.append(match.title, style=f"bold {TEAL}")
        card.append(
            f"   {match.cwd or match.project} · session {match.session_id}",
            style=MUTED,
        )

        lines: list = [card]
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
        if not self._row_selected or not self._matches:
            return None
        table = self.query_one("#search-results", DataTable)
        if table.cursor_row is None or not 0 <= table.cursor_row < len(self._matches):
            return None
        return self._matches[table.cursor_row]

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # The cursor also moves programmatically (table rebuilds); only a real
        # selection should drive the preview.
        if self._row_selected:
            self._render_preview()

    # -- open the transcript viewer --------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in a bar opens nothing unless a row was explicitly selected.
        if self._row_selected:
            self._open_viewer()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # A click is an explicit selection: select the row and open it.
        self._set_row_selected(True)
        self._open_viewer()

    def _open_viewer(self) -> None:
        """Open the selected session's transcript to read before resuming."""
        from .viewer_screen import TranscriptScreen

        match = self._selected_match()
        if match is None:
            return
        query = self.query_one("#search-input", Input).value
        self.app.push_screen(TranscriptScreen(match, query, self._regex, self._accounts))

    def action_close(self) -> None:
        if self._standalone:
            self.app.exit()
        else:
            self.dismiss()


class SearchApp(App):
    """A minimal host app so `cctop search` opens straight into the search TUI."""

    TITLE = "cctop search"
    SUB_TITLE = ""

    def __init__(
        self,
        accounts: list[Account],
        initial_query: str = "",
        regex: bool = False,
        within: Path | None = None,
        path_filter: str = "",
    ) -> None:
        super().__init__()
        self._accounts = accounts
        self._initial_query = initial_query
        self._regex = regex
        self._within = within
        self._path_filter = path_filter

    def on_mount(self) -> None:
        self.push_screen(
            SearchScreen(
                self._accounts,
                initial_query=self._initial_query,
                regex=self._regex,
                within=self._within,
                path_filter=self._path_filter,
                standalone=True,
            )
        )
