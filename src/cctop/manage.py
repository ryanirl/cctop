"""Account management: provision a new Claude Code account, optionally copy an
existing account's user config into it, and (only when explicitly asked) set a
shell alias.

Strictly additive and safe by design:
  - never deletes or overwrites any file or directory,
  - creates the new config dir only if it does not exist,
  - copies only an allowlist of user-authored config (never credentials,
    identity, or session state), and only files not already present,
  - writes a shell alias only on an explicit request (never automatically),
    appends it idempotently after backing up the rc file.

There is deliberately no remove/delete operation, so cctop can manage accounts
without any risk of destroying credentials, sessions, or shell config.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

ALIAS_MARKER = "# added by cctop"
_RC_CANDIDATES = (".zshrc", ".bashrc", ".bash_profile", ".profile")

# User-authored config worth cloning into a new account. This is an ALLOWLIST:
# anything not named here (identity in .claude.json, .credentials.json, and all
# of projects/, sessions/, tasks/, history, caches) is never copied, so a clone
# can only ever carry settings and customizations, never login or session state.
CONFIG_ALLOWLIST: tuple[str, ...] = (
    "CLAUDE.md",
    "settings.json",
    "commands",
    "agents",
    "skills",
    "hooks",
    "output-styles",
)


@dataclass(frozen=True)
class AddPlan:
    """A planned account provision; nothing changes until the apply calls run."""

    index: int
    alias_name: str
    config_dir: Path
    rc_file: Path
    alias_line: str
    dir_exists: bool
    alias_exists: bool


def _next_free_index(home: Path) -> int:
    index = 1
    while (home / f".claude-{index}").exists():
        index += 1
    return index


def detect_rc_file(home: Path) -> Path:
    """The shell rc file to append an alias to.

    Prefer whichever rc already defines Claude config-dir aliases, so a new
    alias lands next to the existing cc-* ones; otherwise pick by $SHELL.
    """
    for name in _RC_CANDIDATES:
        rc = home / name
        try:
            if rc.exists() and "CLAUDE_CONFIG_DIR" in rc.read_text():
                return rc
        except OSError:
            continue
    shell = os.environ.get("SHELL", "")
    if "bash" in shell:
        return home / ".bashrc"
    return home / ".zshrc"


def _alias_present(rc_file: Path, alias_name: str) -> bool:
    try:
        text = rc_file.read_text()
    except OSError:
        return False
    needle = f"alias {alias_name}="
    return any(needle in line for line in text.splitlines())


def plan_add(home: Path, name: str | None = None, reuse_dir: Path | None = None) -> AddPlan:
    """Compute what adding a new account would do, without changing anything.

    When `reuse_dir` is given (an existing but logged-out `.claude-N`), the plan
    targets that directory instead of minting a new index, so a previously
    created empty account is filled in rather than stranded. `alias_name` and
    `alias_line` are computed for display, but no alias is written unless
    set_alias is called explicitly.
    """
    if reuse_dir is not None:
        config_dir = reuse_dir
        suffix = reuse_dir.name[len(".claude-") :]
        index = int(suffix) if suffix.isdigit() else _next_free_index(home)
    else:
        index = _next_free_index(home)
        config_dir = home / f".claude-{index}"
    alias_name = name or f"cc-{index}"
    rc_file = detect_rc_file(home)
    alias_line = f"alias {alias_name}='CLAUDE_CONFIG_DIR=$HOME/.claude-{index} command claude'"
    return AddPlan(
        index=index,
        alias_name=alias_name,
        config_dir=config_dir,
        rc_file=rc_file,
        alias_line=alias_line,
        dir_exists=config_dir.exists(),
        alias_exists=_alias_present(rc_file, alias_name),
    )


def ensure_config_dir(config_dir: Path) -> list[str]:
    """Create the config dir if absent. Never touches an existing one."""
    if config_dir.exists():
        return [f"config dir {config_dir} already exists, left as-is"]
    config_dir.mkdir(parents=True)
    return [f"created config dir {config_dir}"]


def copy_config(source_dir: Path, dest_dir: Path) -> list[str]:
    """Clone the allowlisted user config from source_dir into dest_dir.

    Additive only: copies an item just when it exists in the source and is NOT
    already present in the destination, so nothing is ever overwritten and the
    source is never modified. Only CONFIG_ALLOWLIST entries are eligible, so
    credentials, identity (.claude.json), and session state are never copied.
    """
    actions = []
    for name in CONFIG_ALLOWLIST:
        source = source_dir / name
        if not source.exists():
            continue
        dest = dest_dir / name
        if dest.exists():
            actions.append(f"{name} already present, left as-is")
            continue
        if source.is_dir():
            shutil.copytree(source, dest)
        else:
            shutil.copy2(source, dest)
        actions.append(f"copied {name}")
    if not actions:
        actions.append("no allowlisted config found to copy")
    return actions


def set_alias(plan: AddPlan) -> list[str]:
    """Append the shell alias for this account (explicit, opt-in only).

    Idempotent: appends only when the alias is not already present, after a
    one-time backup of the rc file. Never removes or rewrites existing lines.
    """
    if plan.alias_exists:
        return [f"alias {plan.alias_name} already in {plan.rc_file.name}, left as-is"]

    actions = []
    if plan.rc_file.exists():
        backup = plan.rc_file.with_name(plan.rc_file.name + ".cctop.bak")
        if not backup.exists():
            shutil.copy2(plan.rc_file, backup)
            actions.append(f"backed up {plan.rc_file.name} to {backup.name}")
    with plan.rc_file.open("a") as handle:
        handle.write(f"\n{ALIAS_MARKER}\n{plan.alias_line}\n")
    actions.append(f"appended alias {plan.alias_name} to {plan.rc_file.name}")
    return actions
