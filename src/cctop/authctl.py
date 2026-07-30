"""Claude credential storage and delegated token refresh.

Saved account directories remain the durable login profiles.  Switching copies
one complete credential record into the single main Claude Code store; before
the next switch, the possibly-refreshed main record is copied back to the active
profile.  Credentials stay in their existing file or macOS Keychain and are
never printed.

Which command matters, verified empirically: `claude mcp list` refreshes an
expired token (it needs the auth context, so startup renews it), while
`claude auth status` and `claude --version`/`--help` do NOT (they short-circuit
before the refresh). Both are quota-free; we use `mcp list` to refresh and
`auth status --json` to read login identity.

There is deliberately no login, logout, or delete path.
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

_USER_BINARY = Path.home() / ".local" / "bin" / "claude"
_BINARY_FALLBACK = Path("/usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe")
_KEYCHAIN_SERVICE = "Claude Code-credentials"
_KEYCHAIN_ACCOUNT = "user"
_SECURITY_TIMEOUT = 5


class CredentialError(RuntimeError):
    """A credential store could not be read or updated safely."""


def find_claude_binary() -> str | None:
    """The real Claude Code executable, or None if it cannot be located.

    Prefers PATH (a `claude` symlink into the native install), then the standard
    user-local link, then the legacy global npm location.  The explicit
    user-local path matters for launchd, whose deliberately small PATH omits
    ``~/.local/bin``.
    """
    found = shutil.which("claude")
    if found:
        return found
    if _USER_BINARY.exists():
        return str(_USER_BINARY)
    return str(_BINARY_FALLBACK) if _BINARY_FALLBACK.exists() else None


def keychain_service(config_dir: Path) -> str:
    """The Keychain service name Claude Code uses for a profile on macOS.

    Verified empirically: the default ``~/.claude`` profile owns the bare
    service name, and every other CLAUDE_CONFIG_DIR gets
    ``Claude Code-credentials-<first 8 hex of sha256(config_dir_path)>``, which
    is how a second account stays distinct from the default in one Keychain.
    """
    if config_dir == Path.home() / ".claude":
        return _KEYCHAIN_SERVICE
    digest = hashlib.sha256(str(config_dir).encode()).hexdigest()[:8]
    return f"{_KEYCHAIN_SERVICE}-{digest}"


def identity_path(config_dir: Path) -> Path:
    """Claude's identity/config file for a profile.

    The default profile is the historical special case at ``~/.claude.json``;
    custom ``CLAUDE_CONFIG_DIR`` profiles keep it inside their directory.
    """
    if config_dir == Path.home() / ".claude":
        return Path.home() / ".claude.json"
    return config_dir / ".claude.json"


def read_identity(config_dir: Path) -> dict:
    try:
        value = json.loads(identity_path(config_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _keychain_read(service: str) -> str | None:
    command = ["security", "find-generic-password", "-s", service, "-w"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=_SECURITY_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _quote_security(value: str) -> str:
    """Quote a non-secret value for security(1)'s interactive command parser."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _keychain_write(service: str, credential: str) -> None:
    """Update a Keychain item without putting plaintext credential data in argv."""
    credential_hex = credential.encode("utf-8").hex()
    command = (
        "add-generic-password -U "
        f"-a {_quote_security(_KEYCHAIN_ACCOUNT)} "
        f"-s {_quote_security(service)} -X {credential_hex}\n"
    )
    try:
        result = subprocess.run(
            ["security", "-i"],
            input=command,
            capture_output=True,
            text=True,
            timeout=_SECURITY_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CredentialError(f"Keychain update failed: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise CredentialError(f"Keychain update failed: {detail}")


def read_credentials(config_dir: Path) -> str:
    """Read one profile's complete credential record, without fallback."""
    credentials_file = config_dir / ".credentials.json"
    try:
        raw = credentials_file.read_text().strip()
    except OSError:
        raw = ""
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise CredentialError(f"invalid credentials in {credentials_file}") from error
        if isinstance(payload.get("claudeAiOauth"), dict):
            return raw

    keychain_raw = _keychain_read(keychain_service(config_dir))
    if keychain_raw is None:
        raise CredentialError(f"no credential found for {config_dir}")
    try:
        payload = json.loads(keychain_raw)
    except json.JSONDecodeError as error:
        raise CredentialError(f"invalid Keychain credential for {config_dir}") from error
    if not isinstance(payload.get("claudeAiOauth"), dict):
        raise CredentialError(f"credential for {config_dir} has no OAuth record")
    return keychain_raw


def write_credentials(config_dir: Path, credential: str) -> None:
    """Replace one profile's credential in its existing storage backend."""
    try:
        payload = json.loads(credential)
    except json.JSONDecodeError as error:
        raise CredentialError("refusing to write an invalid credential record") from error
    if not isinstance(payload.get("claudeAiOauth"), dict):
        raise CredentialError("refusing to write a credential without OAuth data")

    credentials_file = config_dir / ".credentials.json"
    if credentials_file.exists():
        temporary = credentials_file.with_suffix(".json.cctop-new")
        temporary.write_text(credential)
        os.chmod(temporary, 0o600)
        os.replace(temporary, credentials_file)
        return
    _keychain_write(keychain_service(config_dir), credential)


def read_expiry(config_dir: Path) -> datetime | None:
    """The access token's expiry from the account's Keychain record (read-only).

    Used to show a TTL and to tell whether a refresh actually renewed the token.
    Returns None when the record or its expiresAt field is unavailable.
    """
    try:
        oauth = json.loads(read_credentials(config_dir)).get("claudeAiOauth") or {}
    except (CredentialError, json.JSONDecodeError):
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
    try:
        oauth = json.loads(read_credentials(config_dir)).get("claudeAiOauth")
    except (CredentialError, json.JSONDecodeError):
        return False
    return isinstance(oauth, dict) and bool(oauth.get("accessToken"))


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
        return RefreshResult(account, False, "needs re-login - refresh token itself is dead", after)

    renewed = before is None or after > before
    verb = "refreshed" if renewed else "already valid"
    return RefreshResult(account, True, verb, after)
