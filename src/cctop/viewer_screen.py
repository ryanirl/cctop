"""The transcript viewer: read a conversation before deciding to resume it.

Pushed by the search screen when a session is selected. The dialogue (tool
traffic hidden) loads in a background worker, query matches are highlighted,
and the view starts at the first matching message. From here the session can
be resumed in place (cctop suspends, the owner binary takes the terminal, and
cctop returns when it exits) or in a new Terminal window, both under the
session's own account.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from . import histsearch
from .cli import _format_age
from .collect import Account

TEAL = "#20B2AA"
MUTED = "grey50"


def resume_in_place(screen: ModalScreen, plan: histsearch.ResumePlan) -> None:
    """Suspend the app, hand the terminal to the owner binary, take it back.

    Shared by every resume path; cctop itself writes nothing, it only launches
    the tool that owns the session and waits for it to exit.
    """
    with screen.app.suspend():
        env = dict(os.environ, **plan.env_extra)
        try:
            subprocess.run(plan.argv, env=env, cwd=plan.cwd)
        except (OSError, KeyboardInterrupt):
            pass


class TranscriptScreen(ModalScreen):
    """A scrollable, read-only view of one session's conversation."""

    CSS = f"""
    TranscriptScreen {{ align: center middle; }}
    #viewer-box {{
        border: round {TEAL};
        border-title-color: {TEAL};
        width: 90%;
        height: 90%;
        padding: 0 1;
        background: $background;
    }}
    #viewer-header {{ height: 1; color: #808080; }}
    #viewer-scroll {{ height: 1fr; }}
    #viewer-scroll Static {{ height: auto; margin: 0 0 1 0; }}
    #viewer-hint {{ height: 1; color: #808080; }}
    """

    BINDINGS = [
        ("escape", "back", "Back"),
        ("q", "back", "Back"),
        ("enter", "resume_here", "Resume here"),
        ("o", "resume_here", "Resume here"),
        ("t", "resume_new_window", "Resume in new window"),
        ("j", "scroll(1)", "Down"),
        ("k", "scroll(-1)", "Up"),
    ]

    def __init__(
        self,
        match: histsearch.SessionMatch,
        query: str,
        regex: bool,
        accounts: list[Account],
    ) -> None:
        super().__init__()
        self._match = match
        self._query = query
        self._regex = regex
        self._accounts = accounts

    def compose(self) -> ComposeResult:
        match = self._match
        with Container(id="viewer-box") as box:
            box.border_title = f"TRANSCRIPT · {match.account}"
            header = Text.assemble(
                (match.title, f"bold {TEAL}"),
                ("   ", "default"),
                (match.project, MUTED),
                ("   ", "default"),
                (f"{len(match.hits)} match(es)", MUTED),
            )
            yield Static(header, id="viewer-header")
            with VerticalScroll(id="viewer-scroll"):
                yield Static(Text("loading transcript...", style=MUTED), id="viewer-loading")
            yield Static(
                Text(
                    "enter/o resume here · t resume in new window · j/k scroll · esc back",
                    style=MUTED,
                ),
                id="viewer-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#viewer-scroll", VerticalScroll).focus()
        self._load()

    @work(thread=True, exclusive=True, group="transcript")
    def _load(self) -> None:
        messages, truncated = histsearch.read_conversation(self._match.path, self._match.provider)
        self.app.call_from_thread(self._show_conversation, messages, truncated)

    def _show_conversation(self, messages: list[histsearch.Message], truncated: bool) -> None:
        from .search_screen import _highlight

        scroll = self.query_one("#viewer-scroll", VerticalScroll)
        self.query_one("#viewer-loading", Static).remove()

        now = datetime.now(timezone.utc)
        widgets: list[Static] = []
        first_match: Static | None = None
        if truncated:
            widgets.append(
                Static(Text("... earlier messages omitted (large transcript)", style=MUTED))
            )
        for message in messages:
            header = Text()
            header.append(
                message.role,
                style=f"bold {TEAL}" if message.role == "user" else "bold default",
            )
            header.append(f" · {_format_age(message.timestamp, now)} ago", style=MUTED)
            body = _highlight(message.text, self._query, self._regex)
            widget = Static(Text.assemble(header, "\n", body))
            widgets.append(widget)
            if first_match is None and self._query_in(message.text):
                first_match = widget
        if not messages:
            widgets.append(Static(Text("no conversational messages found", style=MUTED)))

        scroll.mount_all(widgets)
        if first_match is not None:
            self.call_after_refresh(scroll.scroll_to_widget, first_match, animate=False)

    def _query_in(self, text: str) -> bool:
        import re

        if not self._query:
            return False
        if self._regex:
            try:
                return re.search(self._query, text, re.IGNORECASE) is not None
            except re.error:
                return False
        return self._query.casefold() in text.casefold()

    def action_scroll(self, delta: int) -> None:
        scroll = self.query_one("#viewer-scroll", VerticalScroll)
        if delta > 0:
            scroll.scroll_down(animate=False)
        else:
            scroll.scroll_up(animate=False)

    # -- resume ----------------------------------------------------------------

    def _plan(self) -> histsearch.ResumePlan | None:
        plan = histsearch.resume_plan(self._match, self._accounts)
        if plan is None:
            self.notify(
                f"{self._match.provider} binary not found on PATH",
                title="resume",
                severity="warning",
                timeout=5,
            )
        return plan

    def action_resume_here(self) -> None:
        plan = self._plan()
        if plan is None:
            return
        resume_in_place(self, plan)
        self.notify(f"returned from {self._match.account} session", title="resume", timeout=4)

    def action_resume_new_window(self) -> None:
        plan = self._plan()
        if plan is None:
            return
        error = histsearch.resume_in_new_terminal(plan)
        if error is not None:
            self.notify(error, title="resume", severity="warning", timeout=6)
        else:
            self.notify(
                f"{self._match.account} session opened in a new Terminal window",
                title="resume",
                timeout=4,
            )

    def action_back(self) -> None:
        self.dismiss()
