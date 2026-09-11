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
from datetime import datetime, timedelta, timezone
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


def is_default_config_dir(config_dir: Path) -> bool:
    """Whether this is Claude Code's default config dir (~/.claude).

    The default account's stores are NOT the per-dir ones: with no
    CLAUDE_CONFIG_DIR set, Claude Code keeps identity in ~/.claude.json (home
    level, next to the dir) and the credential under the un-suffixed Keychain
    service. Treating ~/.claude like an explicit config dir (setting the env
    var, hashing its path into a service name) silently forks a second,
    parallel login for the same directory.
    """
    return config_dir == Path.home() / ".claude"


def _keychain_service(config_dir: Path) -> str:
    digest = hashlib.sha256(str(config_dir).encode()).hexdigest()[:8]
    return f"{_KEYCHAIN_SERVICE}-{digest}"


def keychain_services(config_dir: Path) -> tuple[str, ...]:
    """The Keychain services that can hold this account's credential, in
    lookup order.

    An explicit config dir has exactly one: its hashed per-dir service. The
    default dir's real credential lives under the plain default service; its
    hashed service is consulted second, only to cover a setup that ever ran
    claude with CLAUDE_CONFIG_DIR=~/.claude set explicitly.
    """
    if is_default_config_dir(config_dir):
        return (_KEYCHAIN_SERVICE, _keychain_service(config_dir))
    return (_keychain_service(config_dir),)


def claude_env(config_dir: Path) -> dict[str, str]:
    """The environment for a delegated claude run, scoped to one account.

    An explicit config dir is pinned via CLAUDE_CONFIG_DIR. The default dir
    must run WITHOUT the variable: setting it -- even to ~/.claude itself --
    switches Claude Code onto the per-dir identity file and Keychain service,
    forking a parallel login for the same directory instead of using the
    account the user's own `claude` command uses.
    """
    env = dict(os.environ)
    if is_default_config_dir(config_dir):
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def _expiry_from_millis(value: object) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _keychain_read(service: str) -> str | None:
    """The raw payload of one Keychain service, or None when absent/unreadable."""
    command = ["security", "find-generic-password", "-s", service, "-w"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _oauth_record(config_dir: Path) -> dict | None:
    """The account's claudeAiOauth record from its own store, or None."""
    try:
        record = json.loads((config_dir / ".credentials.json").read_text())
        oauth = record.get("claudeAiOauth")
        if isinstance(oauth, dict):
            return oauth
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    for service in keychain_services(config_dir):
        raw = _keychain_read(service)
        if raw is None:
            continue
        try:
            oauth = json.loads(raw).get("claudeAiOauth")
        except (json.JSONDecodeError, AttributeError):
            continue
        if isinstance(oauth, dict):
            return oauth
    return None


def is_long_lived_token(config_dir: Path) -> bool:
    """Whether the stored login is a long-lived `claude setup-token` token.

    Such tokens carry no refresh token. The usage endpoint refuses them (a 429
    with an hour-long retry-after from first use), so callers skip the fetch
    and rely on the statusline feed instead.
    """
    oauth = _oauth_record(config_dir)
    if not oauth or not isinstance(oauth.get("accessToken"), str):
        return False
    return not oauth.get("refreshToken")


def read_expiry(config_dir: Path) -> datetime | None:
    """The access token's expiry from the account's own store (read-only).

    Prefers the on-disk credentials file (no subprocess), then the account's
    Keychain services, mirroring get_token's resolution order. Used to show a
    TTL, to decide whether a proactive refresh is due, and to tell whether a
    refresh actually renewed the token. None when unavailable.
    """
    try:
        record = json.loads((config_dir / ".credentials.json").read_text())
        expiry = _expiry_from_millis((record.get("claudeAiOauth") or {}).get("expiresAt"))
        if expiry is not None:
            return expiry
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    for service in keychain_services(config_dir):
        raw = _keychain_read(service)
        if raw is None:
            continue
        try:
            oauth = json.loads(raw).get("claudeAiOauth") or {}
        except json.JSONDecodeError:
            continue
        expiry = _expiry_from_millis(oauth.get("expiresAt"))
        if expiry is not None:
            return expiry
    return None


def credentials_file_present(config_dir: Path) -> bool:
    """Whether the dir's own .credentials.json holds an access token.

    A credentials file lives inside the dir, so unlike a Keychain entry it
    cannot outlive the dir it belongs to.
    """
    try:
        record = json.loads((config_dir / ".credentials.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    oauth = record.get("claudeAiOauth")
    return isinstance(oauth, dict) and bool(oauth.get("accessToken"))


def keychain_credential_present(config_dir: Path) -> bool:
    """Whether any of the account's own Keychain services holds a credential.

    Caveat: Keychain entries survive deleting the config dir, so a hit can be
    a leftover from a previous account at the same path; callers deciding
    "is this account set up?" should corroborate with the dir's identity.
    """
    return any(_keychain_read(service) for service in keychain_services(config_dir))


def has_credentials(config_dir: Path) -> bool:
    """Whether this account has its own stored OAuth credential.

    Checks only the account's own stores: its credentials file and its own
    Keychain services (the per-dir hashed service; plus the plain default
    service only for the default ~/.claude dir, where that IS the account's
    own store). A freshly created explicit dir never borrows the default
    account's token.
    """
    return credentials_file_present(config_dir) or keychain_credential_present(config_dir)


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

    Free and non-interactive; scoped to the account via claude_env so each
    account reports its own login (and the default account reports the real
    default, not a forked per-dir identity). Returns an AuthStatus with
    `error` set (rather than raising) when the binary is missing, times out,
    or emits unparsable output.
    """
    binary = find_claude_binary()
    if binary is None:
        return AuthStatus(False, None, None, None, error="claude binary not found")

    env = claude_env(config_dir)
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
    env = claude_env(config_dir)
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


# How close to expiry a token must be before a proactive refresh is worth it.
# Well inside the ~12-15h token lifetime, and generous enough that a 3-minute
# poll cadence gets many chances before the token actually lapses.
_REFRESH_MARGIN = timedelta(minutes=30)


def ensure_fresh(
    account: str, config_dir: Path, now: datetime | None = None
) -> RefreshResult | None:
    """Delegated refresh only when the stored token is at or past its margin.

    None means nothing needed doing: the expiry is comfortably in the future,
    or it is unreadable (the reactive 401 path is the safety net for that).
    """
    now = now or datetime.now(timezone.utc)

    expiry = read_expiry(config_dir)
    if expiry is None or expiry - now > _REFRESH_MARGIN:
        return None
    return refresh(account, config_dir, now)


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
