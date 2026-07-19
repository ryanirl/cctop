"""Optional user config at ~/.config/cctop/config.toml.

The config is layered: built-in defaults, then what cctop auto-detects, then the
config file on top. An absent (or unparseable) file means pure auto-detect plus
defaults, so cctop always works with no config at all. The file only ever
*overrides* or *extends*: it can rename, hide, reorder, or add accounts, and
tune a few settings. It is never written silently; `cctop config init` generates
a pre-populated, commented starter for those who want to customize.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def config_dir() -> Path:
    """The cctop config directory, honoring XDG_CONFIG_HOME (else ~/.config)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "cctop"


def config_path() -> Path:
    return config_dir() / "config.toml"


@dataclass(frozen=True)
class AccountOverride:
    """One `[[account]]` entry: keyed by dir, everything else optional."""

    dir: Path
    name: str | None = None
    provider: str | None = None
    hidden: bool = False


@dataclass(frozen=True)
class Config:
    """Parsed config: a settings map plus per-account overrides/additions."""

    settings: dict = field(default_factory=dict)
    accounts: list[AccountOverride] = field(default_factory=list)

    def limits_refresh_seconds(self, default: float) -> float:
        value = self.settings.get("limits_refresh_seconds")
        return float(value) if isinstance(value, (int, float)) and value > 0 else default


def load_config(path: Path | None = None) -> Config:
    """Load the config file, or an empty Config when it is absent/unparseable."""
    path = path or config_path()
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return Config()

    settings = data.get("settings")
    accounts: list[AccountOverride] = []
    for entry in data.get("account") or []:
        if not isinstance(entry, dict) or not entry.get("dir"):
            continue
        accounts.append(
            AccountOverride(
                dir=Path(str(entry["dir"])).expanduser(),
                name=entry.get("name"),
                provider=entry.get("provider"),
                hidden=bool(entry.get("hidden", False)),
            )
        )
    return Config(settings=settings if isinstance(settings, dict) else {}, accounts=accounts)
