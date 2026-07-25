"""Hot-switch saved Claude logins into one main session directory."""

from __future__ import annotations

import json
import os
import re
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path

from . import authctl
from .collect import Account
from .locks import claude_lock
from .models import AccountLimits


@dataclass(frozen=True)
class SwitchResult:
    ok: bool
    previous: str | None
    active: str | None
    message: str


def _org_id(config_dir: Path) -> str | None:
    value = authctl.read_identity(config_dir).get("oauthAccount")
    if not isinstance(value, dict):
        return None
    org = value.get("organizationUuid")
    return org if isinstance(org, str) and org else None


def active_account(accounts: list[Account], main_config_dir: Path) -> Account | None:
    """Match the main Claude identity to a stable saved profile."""
    active_org = _org_id(main_config_dir)
    if active_org is None:
        return None
    return next(
        (
            account
            for account in accounts
            if account.provider == "claude" and _org_id(account.auth_dir) == active_org
        ),
        None,
    )


def _write_main_identity(main_config_dir: Path, source_config_dir: Path) -> None:
    source_oauth = authctl.read_identity(source_config_dir).get("oauthAccount")
    if not isinstance(source_oauth, dict) or not source_oauth.get("organizationUuid"):
        raise authctl.CredentialError(f"{source_config_dir} has no saved account identity")

    path = authctl.identity_path(main_config_dir)
    try:
        main = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        main = {}
    if not isinstance(main, dict):
        main = {}
    main["oauthAccount"] = source_oauth
    temporary = path.with_suffix(path.suffix + ".cctop-new")
    temporary.write_text(json.dumps(main, separators=(",", ":")))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _managed_profile_name(identity: dict) -> str:
    oauth = identity.get("oauthAccount")
    if not isinstance(oauth, dict):
        raise authctl.CredentialError("main Claude profile has no saved account identity")
    org = oauth.get("organizationUuid")
    if not isinstance(org, str) or not org:
        raise authctl.CredentialError("main Claude profile has no organization identity")
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", org).strip(".-")
    if not cleaned:
        raise authctl.CredentialError("main Claude profile has an invalid organization identity")
    return cleaned[:128]


def _write_managed_identity(profile_dir: Path, identity: dict) -> None:
    oauth = identity.get("oauthAccount")
    if not isinstance(oauth, dict):
        raise authctl.CredentialError("main Claude profile has no saved account identity")
    path = authctl.identity_path(profile_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".cctop-new")
    temporary.write_text(json.dumps({"oauthAccount": oauth}, separators=(",", ":")))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def ensure_stable_accounts(accounts: list[Account], main_config_dir: Path) -> list[Account]:
    """Give the active login a durable, automatically managed credential store.

    A login found only in the mutable main Claude directory would otherwise be
    lost on the first switch.  Snapshot it once under cctop's config directory,
    keyed by its organization identity, then use that profile automatically.
    """
    from . import config as config_module

    current = active_account(accounts, main_config_dir)
    if current is None or current.auth_dir != main_config_dir:
        return accounts

    identity = authctl.read_identity(main_config_dir)
    profile_dir = config_module.config_dir() / "profiles" / _managed_profile_name(identity)
    with ExitStack() as stack:
        for target in sorted({main_config_dir, profile_dir}, key=str):
            stack.enter_context(claude_lock(target))
        stack.enter_context(claude_lock(authctl.identity_path(main_config_dir)))
        credential = authctl.read_credentials(main_config_dir)
        _write_managed_identity(profile_dir, identity)
        if (main_config_dir / ".credentials.json").exists():
            managed_file = profile_dir / ".credentials.json"
            if not managed_file.exists():
                managed_file.touch(mode=0o600)
        authctl.write_credentials(profile_dir, credential)

    return [
        replace(account, credential_dir=profile_dir) if account == current else account
        for account in accounts
    ]


def switch_account(
    accounts: list[Account],
    target: Account,
    main_config_dir: Path,
) -> SwitchResult:
    """Atomically activate ``target`` while preserving the current login.

    The main credential may have rotated while Claude was using it.  It is
    synced back to the matching saved profile before the target is installed.
    Claude's own credential and config locks close races with a running process.
    """
    if target.provider != "claude":
        return SwitchResult(False, None, None, "Codex profiles are not switchable")
    previous = active_account(accounts, main_config_dir)
    if previous is not None and previous.auth_dir == target.auth_dir:
        return SwitchResult(True, previous.name, target.name, f"{target.name} already active")
    if previous is not None and previous.auth_dir == main_config_dir:
        return SwitchResult(
            False,
            previous.name,
            previous.name,
            "active profile uses the mutable main directory; point it at a saved login dir",
        )
    try:
        lock_targets = {main_config_dir, target.auth_dir}
        if previous is not None:
            lock_targets.add(previous.auth_dir)
        with ExitStack() as stack:
            for config_dir in sorted(lock_targets, key=str):
                stack.enter_context(claude_lock(config_dir))
            stack.enter_context(claude_lock(authctl.identity_path(main_config_dir)))
            target_credential = authctl.read_credentials(target.auth_dir)
            if previous is not None:
                current = authctl.read_credentials(main_config_dir)
                authctl.write_credentials(previous.auth_dir, current)
            authctl.write_credentials(main_config_dir, target_credential)
            _write_main_identity(main_config_dir, target.auth_dir)
    except (authctl.CredentialError, OSError, TimeoutError) as error:
        return SwitchResult(
            False,
            previous.name if previous else None,
            previous.name if previous else None,
            str(error),
        )
    return SwitchResult(
        True,
        previous.name if previous else None,
        target.name,
        f"switched to {target.name}; running Claude sessions pick it up shortly",
    )


def sync_active_profile(accounts: list[Account], main_config_dir: Path) -> SwitchResult | None:
    """Persist any token rotation from the main store into its saved profile."""
    current = active_account(accounts, main_config_dir)
    if current is None or current.auth_dir == main_config_dir:
        return None
    try:
        with ExitStack() as stack:
            for config_dir in sorted({main_config_dir, current.auth_dir}, key=str):
                stack.enter_context(claude_lock(config_dir))
            credential = authctl.read_credentials(main_config_dir)
            authctl.write_credentials(current.auth_dir, credential)
    except (authctl.CredentialError, OSError, TimeoutError) as error:
        return SwitchResult(False, current.name, current.name, str(error))
    return SwitchResult(True, current.name, current.name, f"synced {current.name}")


def _binding_percent(limits: AccountLimits) -> float | None:
    values = [window.percent for window in limits.windows if window.has_data]
    return max(values) if limits.source == "api" and values else None


def best_account(
    accounts: list[Account],
    limits: list[AccountLimits],
    main_config_dir: Path,
) -> Account | None:
    """The non-active Claude profile with the most limit headroom."""
    current = active_account(accounts, main_config_dir)
    by_name = {item.account: item for item in limits}
    candidates: list[tuple[float, Account]] = []
    for account in accounts:
        if account.provider != "claude" or account == current:
            continue
        limits_for_account = by_name.get(
            account.name,
            AccountLimits("", None, [], "none", None),
        )
        percent = _binding_percent(limits_for_account)
        if percent is not None and percent < 99.0:
            candidates.append((percent, account))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def auto_switch(
    accounts: list[Account],
    limits: list[AccountLimits],
    main_config_dir: Path,
    remaining_percent: float,
) -> SwitchResult | None:
    """Rotate when the active profile has at most ``remaining_percent`` left."""
    current = active_account(accounts, main_config_dir)
    if current is None:
        return None
    current_limits = next((item for item in limits if item.account == current.name), None)
    used = _binding_percent(current_limits) if current_limits is not None else None
    if used is None or used < 100.0 - remaining_percent:
        return None
    target = best_account(accounts, limits, main_config_dir)
    if target is None:
        return SwitchResult(False, current.name, current.name, "1% remaining; no healthy profile")
    return switch_account(accounts, target, main_config_dir)
