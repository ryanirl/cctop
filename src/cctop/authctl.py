"""Delegated auth control: let the Claude Code binary refresh its own token.

cctop never rewrites a credential itself. A Claude Code OAuth access token lives
only ~12-15h, and the CLI refreshes it lazily when you use that account, so an
account you are merely monitoring drifts past expiry and the free usage GET
starts returning 401. The safe fix is to ask the tool that *owns* the credential
to renew it: running a lightweight full `claude` command under the account's
CLAUDE_CONFIG_DIR runs Claude Code's startup auth path, which refreshes and
persists the Keychain record itself (the same thing that happens when you open
the account normally). cctop only triggers it and then observes the result.

Which command matters, verified empirically: `claude mcp list` refreshes an
expired token (it needs the auth context, so startup renews it), while
`claude auth status` and `claude --version`/`--help` do NOT (they short-circuit
before the refresh). Both are quota-free; we use `mcp list` to refresh and
`auth status --json` to read login identity.

Everything here is read-only from cctop's side: it invokes headless commands and
reads the credential's expiry for feedback; it never writes the credential, and
there is deliberately no logout/delete path.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# `mcp list` is a full `claude` startup that runs auth-init (which refreshes an
# expired token) but spends no quota and needs no tty; `auth status` reads creds
# without that refresh. Verified against a real expired account.
_REFRESH_ARGS = ("mcp", "list")
_STATUS_ARGS = ("auth", "status", "--json")
_COMMAND_TIMEOUT = 30

_BINARY_FALLBACK = Path("/usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe")
_KEYCHAIN_SERVICE = "Claude Code-credentials"


def find_claude_binary() -> str | None:
    """The real Claude Code executable, or None if it cannot be located.

    Prefers PATH (a `claude` symlink into the npm install); falls back to the
    known native-build location so the refresh works even when PATH is bare.
    """
    found = shutil.which("claude")
    if found:
        return found
    return str(_BINARY_FALLBACK) if _BINARY_FALLBACK.exists() else None


def _keychain_service(config_dir: Path) -> str:
    digest = hashlib.sha256(str(config_dir).encode()).hexdigest()[:8]
    return f"{_KEYCHAIN_SERVICE}-{digest}"


def read_expiry(config_dir: Path) -> datetime | None:
    """The access token's expiry from the account's Keychain record (read-only).

    Used to show a TTL and to tell whether a refresh actually renewed the token.
    Returns None when the record or its expiresAt field is unavailable.
    """
    command = ["security", "find-generic-password", "-s", _keychain_service(config_dir), "-w"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        oauth = json.loads(result.stdout.strip()).get("claudeAiOauth") or {}
    except json.JSONDecodeError:
        return None
    expires_at = oauth.get("expiresAt")
    if not isinstance(expires_at, (int, float)):
        return None
    return datetime.fromtimestamp(expires_at / 1000, tz=timezone.utc)


def has_credentials(config_dir: Path) -> bool:
    """Whether this account has its own stored OAuth credential.

    Checks only the config dir's own credentials file and its per-config-dir
    Keychain service, never the shared default service, so a freshly created
    (logged-out) dir correctly reads as having no credential rather than
    borrowing the default account's token.
    """
    credentials_file = config_dir / ".credentials.json"
    try:
        record = json.loads(credentials_file.read_text())
        oauth = record.get("claudeAiOauth")
        if isinstance(oauth, dict) and oauth.get("accessToken"):
            return True
    except (OSError, json.JSONDecodeError):
        pass

    command = ["security", "find-generic-password", "-s", _keychain_service(config_dir), "-w"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


@dataclass(frozen=True)
class AuthStatus:
    """The parsed result of `claude auth status --json` for one account."""

    logged_in: bool
    email: str | None
    org_id: str | None
    subscription_type: str | None
    error: str | None = None


def auth_status(config_dir: Path) -> AuthStatus:
    """Run the owner binary's headless status probe for one account.

    Free and non-interactive; honors CLAUDE_CONFIG_DIR so each account reports
    its own login. Returns an AuthStatus with `error` set (rather than raising)
    when the binary is missing, times out, or emits unparsable output.
    """
    binary = find_claude_binary()
    if binary is None:
        return AuthStatus(False, None, None, None, error="claude binary not found")

    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(config_dir))
    try:
        result = subprocess.run(
            [binary, *_STATUS_ARGS],
            capture_output=True,
            text=True,
            env=env,
            timeout=_COMMAND_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return AuthStatus(False, None, None, None, error="auth status timed out")
    except (OSError, subprocess.SubprocessError) as error:
        return AuthStatus(False, None, None, None, error=f"auth status failed: {error}")

    try:
        payload = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return AuthStatus(False, None, None, None, error="auth status: bad JSON")

    return AuthStatus(
        logged_in=bool(payload.get("loggedIn")),
        email=payload.get("email"),
        org_id=payload.get("orgId"),
        subscription_type=payload.get("subscriptionType"),
    )


@dataclass(frozen=True)
class RefreshResult:
    """The outcome of a delegated refresh, for a one-line status message."""

    account: str
    ok: bool
    message: str
    expires_at: datetime | None = None


def _run_refresh_trigger(config_dir: Path) -> str | None:
    """Run the headless `claude mcp list` whose startup renews an expired token.

    Quota-free and non-interactive; cctop writes nothing (Claude Code persists
    the refreshed credential itself). Returns an error string, or None on a
    clean run. stdout is discarded; we judge success by the expiry moving.
    """
    binary = find_claude_binary()
    if binary is None:
        return "claude binary not found"
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(config_dir))
    try:
        subprocess.run(
            [binary, *_REFRESH_ARGS],
            capture_output=True,
            text=True,
            env=env,
            timeout=_COMMAND_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return "refresh timed out"
    except (OSError, subprocess.SubprocessError) as error:
        return f"refresh failed: {error}"
    return None


def refresh(account: str, config_dir: Path, now: datetime | None = None) -> RefreshResult:
    """Ask the owner binary to renew this account's token; report what happened.

    cctop writes nothing: the delegated `claude mcp list` run is what refreshes
    and persists the credential (via Claude Code's own code). We read the expiry
    before and after only to describe the result. A no-op on an already-valid
    token is reported as success. A token still invalid afterward means the
    refresh token itself is dead, which only a fresh /login can fix.
    """
    now = now or datetime.now(timezone.utc)
    before = read_expiry(config_dir)

    error = _run_refresh_trigger(config_dir)
    if error is not None:
        return RefreshResult(account, False, error, before)

    after = read_expiry(config_dir)
    if after is None or after <= now:
        return RefreshResult(account, False, "needs re-login - run the account and /login", after)

    renewed = before is None or after > before
    verb = "refreshed" if renewed else "already valid"
    return RefreshResult(account, True, verb, after)
