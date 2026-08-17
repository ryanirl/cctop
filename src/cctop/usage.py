"""Fetch real subscription usage limits from GET /api/oauth/usage.

This is the endpoint Claude Code's /usage command hits (confirmed from its
debug log: `fetchUtilization: GET /api/oauth/usage`). It is a plain GET, so it
consumes no message quota (free), is model-independent, and returns every limit
window as JSON: the 5-hour session window, the all-models weekly window, and any
model-scoped weekly window (e.g. Fable), each with utilization, reset time,
severity, and which one is currently binding.

The OAuth token is read at runtime from the account's own store (config-dir
credentials file, else the macOS Keychain) and used only to authenticate to
Anthropic's own API. It is never logged or persisted.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .authctl import is_default_config_dir, keychain_services
from .models import AccountLimits, LimitWindow

API_BASE = "https://api.anthropic.com"
USAGE_PATH = "/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
KEYCHAIN_SERVICE = "Claude Code-credentials"


def _oauth_block(path: Path) -> dict:
    """The oauthAccount block from one .claude.json file, or {} if unreadable."""
    try:
        record = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    account = record.get("oauthAccount")
    return account if isinstance(account, dict) else {}


def oauth_account(config_dir: Path) -> dict:
    """The account's oauthAccount identity block (identifiers only).

    An explicit config dir keeps its identity at <dir>/.claude.json. The
    default dir's real identity is the home-level ~/.claude.json (that is
    what a plain `claude` run reads); ~/.claude/.claude.json exists only if
    something ran claude with CLAUDE_CONFIG_DIR=~/.claude set and is consulted
    last, so a forked in-dir identity never shadows the real default login.
    """
    if is_default_config_dir(config_dir):
        home_level = _oauth_block(config_dir.parent / ".claude.json")
        if home_level:
            return home_level
    return _oauth_block(config_dir / ".claude.json")


def read_tier(config_dir: Path) -> str | None:
    """The subscription tier string from .claude.json, e.g. default_claude_max_5x."""
    tier = oauth_account(config_dir).get("organizationRateLimitTier")
    return tier if isinstance(tier, str) else None


def _token_from_credentials_file(config_dir: Path) -> str | None:
    try:
        record = json.loads((config_dir / ".credentials.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    oauth = record.get("claudeAiOauth")
    if isinstance(oauth, dict) and isinstance(oauth.get("accessToken"), str):
        return oauth["accessToken"]
    return None


def _keychain_lookup(service: str) -> str | None:
    command = ["security", "find-generic-password", "-s", service, "-w"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        return raw or None
    oauth = record.get("claudeAiOauth")
    if isinstance(oauth, dict) and isinstance(oauth.get("accessToken"), str):
        return oauth["accessToken"]
    return None


def resolve_token(config_dir: Path) -> tuple[str | None, bool]:
    """Resolve this account's OAuth token: (token, borrowed_default).

    Prefers an on-disk credentials file, then the account's own Keychain
    services (for the default ~/.claude dir the plain default service IS its
    own store). As a last resort a non-default dir falls back to the default
    service so a legacy single-account setup still works -- that case returns
    borrowed_default=True, because the token belongs to the default account
    and must never be presented as this account's without verification.
    """
    token = _token_from_credentials_file(config_dir)
    if token:
        return token, False
    for service in keychain_services(config_dir):
        token = _keychain_lookup(service)
        if token:
            return token, False
    if is_default_config_dir(config_dir):
        return None, False
    token = _keychain_lookup(KEYCHAIN_SERVICE)
    return token, token is not None


def get_token(config_dir: Path) -> str | None:
    """The resolved token alone, for callers that only need presence."""
    return resolve_token(config_dir)[0]


def _get(url: str, token: str) -> tuple[int | None, dict[str, str], str]:
    """Authorized GET; returns (status, headers, body). Never logs the token."""
    request = urllib.request.Request(url, method="GET")
    request.add_header("authorization", f"Bearer {token}")
    request.add_header("anthropic-beta", OAUTH_BETA)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status, headers = response.status, response.headers
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        status, headers = error.code, error.headers
        body = error.read(2000).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as error:
        return None, {}, f"request failed: {error}"
    lowered = {key.lower(): value for key, value in headers.items()}
    return status, lowered, body


def _parse_retry_after(headers: dict[str, str]) -> float | None:
    """The Retry-After wait in seconds, when the server sends the numeric form.

    The HTTP-date form is ignored (we fall back to exponential backoff), which
    keeps this simple and avoids trusting clock skew.
    """
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _parse_reset(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _label(entry: dict) -> str:
    """A short display label for one limit entry, e.g. "5h" or "week (Fable)"."""
    kind = entry.get("kind")
    if kind == "session":
        return "5h"
    if kind == "weekly_all":
        return "week (all)"
    if kind == "weekly_scoped":
        scope = entry.get("scope") or {}
        model = (scope.get("model") or {}).get("display_name")
        return f"week ({model})" if model else "week (scoped)"
    return str(kind or "?")


def parse_windows(payload: dict) -> list[LimitWindow]:
    """Turn the usage payload's `limits` array into typed windows.

    Reads the `limits` list rather than the top-level five_hour/seven_day fields
    so any model-scoped window (Fable today, others later) is picked up without
    hardcoding. Malformed entries are skipped.
    """
    windows = []
    for entry in payload.get("limits", []):
        if not isinstance(entry, dict):
            continue
        try:
            percent = float(entry.get("percent", 0))
        except (TypeError, ValueError):
            continue
        resets_at = _parse_reset(entry.get("resets_at"))

        # Guard the known bug where `percent` occasionally carries the reset
        # epoch instead of a real value; and treat a never-started window (0%
        # with no reset) as "no data" rather than a real zero.
        has_data = True
        if percent > 101:
            percent, has_data = 0.0, False
        elif percent > 100:
            percent = 100.0
        elif percent == 0 and resets_at is None:
            has_data = False

        windows.append(
            LimitWindow(
                kind=str(entry.get("kind", "")),
                label=_label(entry),
                percent=percent,
                resets_at=resets_at,
                severity=str(entry.get("severity", "normal")),
                is_active=bool(entry.get("is_active")),
                has_data=has_data,
            )
        )
    return windows


def fetch_account_limits(account: str, config_dir: Path) -> AccountLimits:
    """Fetch and parse an account's live usage limits (free GET).

    Returns source="none" with an `error` note when the token is missing, the
    request fails, or the token's org does not match this account (a wrong
    credential) so the UI never shows one account's numbers under another.
    Every result carries the login's email so the UI can show which Claude
    account each entry really is.
    """
    identity = oauth_account(config_dir)
    tier = identity.get("organizationRateLimitTier")
    tier = tier if isinstance(tier, str) else None
    email = identity.get("emailAddress")
    email = email if isinstance(email, str) else None

    def absent(error: str, **flags) -> AccountLimits:
        return AccountLimits(account, tier, [], "none", None, error=error, email=email, **flags)

    token, borrowed_default = resolve_token(config_dir)
    if token is None:
        return absent("no token found")
    if borrowed_default and not identity.get("organizationUuid"):
        # Only the default account's token exists and this dir has no identity
        # of its own to verify the response against: fetching would show the
        # default account's numbers under this account's name.
        return absent("no own credential - run this account and /login")

    status, headers, body = _get(f"{API_BASE}{USAGE_PATH}", token)
    if status in (401, 403):
        # Stale/expired OAuth token. cctop never refreshes a token itself (the
        # refresh token may rotate and invalidate the copy Claude Code relies
        # on); the monitor reacts to auth_expired by delegating a refresh to
        # the owner binary, the same thing the R key does.
        return absent(f"token expired - run {account} to refresh", auth_expired=True)
    if status == 429 or status is None or (status is not None and status >= 500):
        # Transient: rate limited (429), a server hiccup (5xx), or a network
        # failure (status None). Mark retriable so the monitor backs off and
        # keeps showing the last good numbers instead of blanking the gauges.
        label = (
            "rate limited (429)"
            if status == 429
            else (f"HTTP {status}" if status else "network error")
        )
        return absent(
            f"usage: {label}",
            retriable=True,
            retry_after=_parse_retry_after(headers) if status == 429 else None,
        )
    if status != 200:
        return absent(f"usage fetch: HTTP {status}")

    expected_org = identity.get("organizationUuid")
    got_org = headers.get("anthropic-organization-id")
    if expected_org and got_org and expected_org != got_org:
        return absent("wrong credential (token org != account)")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return absent("usage fetch: bad JSON")

    return AccountLimits(
        account=account,
        tier=tier,
        windows=parse_windows(payload),
        source="api",
        fetched_at=datetime.now(timezone.utc),
        email=email,
    )
