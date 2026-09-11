"""In-app settings screen: live-edit ~/.config/cctop/config.toml, no restart.

Opened with `,` from the main view. Edits the usage-refresh interval, the
heatmap range, and per-account show/hide + name, then writes the config file and
asks the app to apply it immediately. The config file stays the single source of
truth: Save writes it, the app reloads and applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static, Switch

from . import config as config_module
from .collect import Account
from .config import AccountOverride, Config

TEAL = "#20B2AA"
MUTED = "grey50"


@dataclass
class AccountRow:
    """One account's editable state in the settings screen."""

    dir: Path
    name: str
    provider: str
    hidden: bool


def build_rows(detected: list[Account], config: Config) -> list[AccountRow]:
    """Every detected account plus any config-only ones, with current name/hidden."""
    overrides = {override.dir: override for override in config.accounts}
    rows: list[AccountRow] = []
    seen: set[Path] = set()
    for account in detected:
        seen.add(account.config_dir)
        override = overrides.get(account.config_dir)
        rows.append(
            AccountRow(
                dir=account.config_dir,
                name=override.name if override and override.name else account.name,
                provider=account.provider,
                hidden=bool(override.hidden) if override else False,
            )
        )
    for override in config.accounts:
        if override.dir not in seen:
            rows.append(
                AccountRow(
                    dir=override.dir,
                    name=override.name or override.dir.name.lstrip("."),
                    provider=override.provider or "claude",
                    hidden=override.hidden,
                )
            )
    return rows


def _home(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if text.startswith(home) else text


class SettingsScreen(ModalScreen):
    """A read/write settings overlay that applies live on Save."""

    CSS = f"""
    SettingsScreen {{ align: center middle; }}
    #settings-box {{
        border: round {TEAL};
        border-title-color: {TEAL};
        width: 78;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: $background;
    }}
    #settings-box Label {{ color: {TEAL}; margin: 1 0 0 0; }}
    #settings-box Input {{ width: 20; }}
    .acct-row {{ height: 3; }}
    .acct-row Switch {{ width: 8; }}
    .acct-row Input {{ width: 18; }}
    .acct-dir {{ color: #808080; content-align: left middle; margin: 0 0 0 2; }}
    #settings-actions {{ height: 3; margin: 1 0 0 0; }}
    #settings-actions Button {{ margin: 0 2 0 0; }}
    #settings-hint {{ color: #808080; height: 1; }}
    """

    BINDINGS = [("escape", "cancel", "Close")]

    def __init__(
        self,
        interval: float,
        weeks: int,
        rows: list[AccountRow],
        auto_refresh_tokens: bool = True,
        quota_probe: bool = True,
    ) -> None:
        super().__init__()
        self._interval = interval
        self._weeks = weeks
        self._rows = rows
        self._auto_refresh_tokens = auto_refresh_tokens
        self._quota_probe = quota_probe

    def compose(self) -> ComposeResult:
        with Container(id="settings-box") as box:
            box.border_title = "SETTINGS"
            with VerticalScroll():
                yield Label("Usage refresh (seconds)")
                yield Input(value=str(int(self._interval)), id="interval", type="integer")
                yield Label("Heatmap weeks")
                yield Input(value=str(self._weeks), id="weeks", type="integer")
                yield Label("Auto-refresh expiring tokens (via the claude binary)")
                yield Switch(value=self._auto_refresh_tokens, id="auto-refresh")
                yield Label("Long-lived logins: one-turn claude probe for every limit window")
                yield Switch(value=self._quota_probe, id="quota-probe")
                yield Label("Accounts  (toggle to show, edit the name)")
                for index, row in enumerate(self._rows):
                    with Horizontal(classes="acct-row"):
                        yield Switch(value=not row.hidden, id=f"show-{index}")
                        yield Input(value=row.name, id=f"name-{index}")
                        yield Label(_home(row.dir), classes="acct-dir")
            with Horizontal(id="settings-actions"):
                yield Button("Save", variant="success", id="save")
                yield Button("Cancel", id="cancel")
            yield Static(
                Text(f"writes {_home(config_module.config_path())}", style=MUTED),
                id="settings-hint",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._save()
        else:
            self.dismiss()

    def action_cancel(self) -> None:
        self.dismiss()

    def _int(self, widget_id: str, default: int, minimum: int) -> int:
        try:
            return max(minimum, int(self.query_one(f"#{widget_id}", Input).value))
        except ValueError:
            return default

    def _save(self) -> None:
        settings = {
            "limits_refresh_seconds": self._int("interval", 180, 10),
            "heatmap_weeks": self._int("weeks", 26, 1),
            "auto_refresh_tokens": self.query_one("#auto-refresh", Switch).value,
            "quota_probe": self.query_one("#quota-probe", Switch).value,
        }
        accounts = []
        for index, row in enumerate(self._rows):
            name = self.query_one(f"#name-{index}", Input).value.strip() or row.name
            hidden = not self.query_one(f"#show-{index}", Switch).value
            accounts.append(
                AccountOverride(dir=row.dir, name=name, provider=row.provider, hidden=hidden)
            )
        config_module.save_config(settings, accounts)
        self.app.apply_settings()  # type: ignore[attr-defined]
        self.dismiss()
