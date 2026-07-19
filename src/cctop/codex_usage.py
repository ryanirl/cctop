"""Live Codex (OpenAI) usage limits from chatgpt.com.

Codex fetches its subscription rate limits from GET
https://chatgpt.com/backend-api/codex/usage (discovered from Codex's own logs).
It is a plain GET, so it consumes no message quota (free), and returns the
5-hour / weekly windows plus any per-model ("additional") limits.

The ChatGPT OAuth token is read at runtime from ~/.codex/auth.json (a plain
file, not the Keychain) and used only to authenticate to OpenAI's own API; it is
never logged or persisted.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .models import AccountLimits, LimitWindow

CODEX_DIR = Path.home() / ".codex"
USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
_CLIENT_VERSION = "0.144.1"


def _read_auth() -> tuple[str, str] | None:
    """The (access_token, account_id) from auth.json, or None if unavailable."""
    try:
        record = json.loads((CODEX_DIR / "auth.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    tokens = record.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    account = tokens.get("account_id") or ""
    if not isinstance(access, str) or not access:
        return None
    return access, account


def _window_label(seconds: int) -> str:
    """Label a window by its duration: 18000s -> 5h, 604800s -> week."""
    if seconds == 18000:
        return "5h"
    if seconds == 604800:
        return "week"
    if seconds and seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{max(1, seconds // 60)}m"


def _window(block: dict | None, kind: str, label_prefix: str, hot: bool) -> LimitWindow | None:
    if not isinstance(block, dict) or "used_percent" not in block:
        return None
    seconds = block.get("limit_window_seconds") or 0
    label = _window_label(seconds)
    if label_prefix:
        label = f"{label_prefix} {label}"
    resets_at = None
    if isinstance(block.get("reset_at"), (int, float)):
        resets_at = datetime.fromtimestamp(block["reset_at"], tz=timezone.utc)
    percent = float(block.get("used_percent", 0))
    return LimitWindow(
        kind=kind,
        label=label,
        percent=percent,
        resets_at=resets_at,
        severity="warning" if hot else "normal",
        is_active=False,
        has_data=not (percent == 0 and resets_at is None),
    )


def _windows(payload: dict) -> list[LimitWindow]:
    windows: list[LimitWindow] = []

    rate = payload.get("rate_limit") or {}
    hot = bool(rate.get("limit_reached"))
    for kind in ("primary_window", "secondary_window"):
        window = _window(rate.get(kind), kind, "", hot)
        if window is not None:
            windows.append(window)

    for extra in payload.get("additional_rate_limits") or []:
        if not isinstance(extra, dict):
            continue
        # Shorten a verbose limit name (e.g. "GPT-5.3-Codex-Spark" -> "Spark")
        # so it stays readable in the compact side-by-side usage panel.
        full = extra.get("limit_name") or "extra"
        name = full.split("-")[-1] if "-" in full else full
        extra_rate = extra.get("rate_limit") or {}
        extra_hot = bool(extra_rate.get("limit_reached"))
        window = _window(extra_rate.get("primary_window"), "additional", name, extra_hot)
        if window is not None:
            windows.append(window)

    # Mark the most-consumed window as the active (binding) one.
    if windows:
        peak = max(range(len(windows)), key=lambda i: windows[i].percent)
        windows[peak] = LimitWindow(
            kind=windows[peak].kind,
            label=windows[peak].label,
            percent=windows[peak].percent,
            resets_at=windows[peak].resets_at,
            severity=windows[peak].severity,
            is_active=True,
        )
    return windows


def fetch_account_limits(account: str = "cx-0") -> AccountLimits:
    """Fetch and parse the Codex account's live usage limits (free GET)."""
    auth = _read_auth()
    if auth is None:
        return AccountLimits(account, None, [], "none", None, error="no codex token")
    token, account_id = auth

    request = urllib.request.Request(USAGE_URL, method="GET")
    request.add_header("authorization", f"Bearer {token}")
    request.add_header("chatgpt-account-id", account_id)
    request.add_header("user-agent", f"codex_cli_rs/{_CLIENT_VERSION}")
    request.add_header("originator", "codex_cli_rs")
    request.add_header("accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return AccountLimits(
                account, None, [], "none", None, error="token expired - run codex to refresh"
            )
        if error.code == 429 or error.code >= 500:
            retry_after: float | None = None
            raw_retry = error.headers.get("retry-after")
            if raw_retry is not None:
                try:
                    retry_after = float(raw_retry)
                except ValueError:
                    retry_after = None
            label = "rate limited (429)" if error.code == 429 else f"HTTP {error.code}"
            return AccountLimits(
                account,
                None,
                [],
                "none",
                None,
                error=f"codex usage: {label}",
                retriable=True,
                retry_after=retry_after,
            )
        return AccountLimits(
            account, None, [], "none", None, error=f"codex usage: HTTP {error.code}"
        )
    except (urllib.error.URLError, TimeoutError) as error:
        return AccountLimits(
            account, None, [], "none", None, error=f"codex usage: {error}", retriable=True
        )
    except json.JSONDecodeError as error:
        return AccountLimits(account, None, [], "none", None, error=f"codex usage: {error}")

    tier = payload.get("plan_type")
    return AccountLimits(
        account=account,
        tier=str(tier) if tier else None,
        windows=_windows(payload),
        source="api",
        fetched_at=datetime.now(timezone.utc),
    )
