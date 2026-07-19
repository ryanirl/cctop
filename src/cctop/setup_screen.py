"""The `cctop setup` provider selector.

A tiny full-screen chooser: pick the provider (Claude Code or OpenAI Codex) to
set up. A provider is only selectable when cctop can detect it (its CLI or a
config dir); an undetected provider is shown dimmed and cannot be chosen. If
neither is detected, both are dim and a note explains why. The chosen provider
is read from `.selected` after the app exits; the CLI then runs the setup.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Group
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Static

TEAL = "#20B2AA"
DIM = "grey37"
MUTED = "grey50"


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    available: bool


class SetupApp(App):
    """Keyboard-driven provider chooser; sets `.selected` to a provider key."""

    CSS = f"""
    Screen {{ align: center middle; }}
    #panel {{
        width: 54;
        height: auto;
        border: round {TEAL};
        border-title-color: {TEAL};
        padding: 1 3;
        background: $background;
    }}
    """

    BINDINGS = [
        ("up", "move(-1)", "Up"),
        ("down", "move(1)", "Down"),
        ("enter", "choose", "Select"),
        ("q", "leave", "Quit"),
        ("escape", "leave", "Quit"),
    ]

    def __init__(self, providers: list[Provider]) -> None:
        super().__init__()
        self._providers = providers
        enabled = [i for i, p in enumerate(providers) if p.available]
        self._cursor = enabled[0] if enabled else -1
        self.selected: str | None = None

    def compose(self) -> ComposeResult:
        with Container(id="panel") as panel:
            panel.border_title = "cctop setup"
            yield Static(id="body")

    def on_mount(self) -> None:
        self._render()

    def _render(self) -> None:
        any_enabled = any(p.available for p in self._providers)
        lines: list = [Text("Set up which provider?", style="default"), Text()]
        for index, provider in enumerate(self._providers):
            line = Text()
            if provider.available:
                selected = index == self._cursor
                line.append("> " if selected else "  ", style=TEAL)
                line.append(provider.label, style=f"bold {TEAL}" if selected else "default")
            else:
                line.append("  ", style="default")
                line.append(f"{provider.label}  (not detected)", style=DIM)
            lines.append(line)
        lines.append(Text())
        if not any_enabled:
            lines.append(Text("Could not detect Claude Code or Codex.", style=MUTED))
            lines.append(Text("Install or sign into one, then run cctop setup again.", style=MUTED))
            lines.append(Text())
        lines.append(Text("up/down move  ·  enter select  ·  q quit", style=MUTED))
        self.query_one("#body", Static).update(Group(*lines))

    def action_move(self, delta: int) -> None:
        enabled = [i for i, p in enumerate(self._providers) if p.available]
        if not enabled:
            return
        current = enabled.index(self._cursor) if self._cursor in enabled else 0
        self._cursor = enabled[(current + delta) % len(enabled)]
        self._render()

    def action_choose(self) -> None:
        if self._cursor >= 0 and self._providers[self._cursor].available:
            self.selected = self._providers[self._cursor].key
            self.exit()

    def action_leave(self) -> None:
        self.exit()
