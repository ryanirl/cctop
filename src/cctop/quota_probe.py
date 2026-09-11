"""A one-turn quota probe, run by Claude Code itself, for logins the usage
endpoint refuses.

The usage endpoint answers 429 to long-lived `claude setup-token` logins, and
the statusline feed carries only the 5-hour and 7-day windows. The one place
every window shows up, including the model-scoped weekly one (week (Fable)),
is the `anthropic-ratelimit-unified-*` headers of an inference response. Claude
Code exposes those as a `rate_limit_event` in its stream-json output, so cctop
asks the Claude Code binary to make one minimal turn under the account's config
dir and reads the event back. This is the same delegation cctop uses for token
refresh: the binary authenticates as itself, cctop never touches the token.

Cost and footprint, deliberately small: a replaced system prompt, no tools,
`max_turns 1`, and `--no-session-persistence`, so a probe is a few hundred
tokens (Claude Code's own quota check works the same way: one `max_tokens: 1`
request). It runs only for long-lived logins, every `quota_probe_seconds`
(default 300), and can be turned off with `quota_probe = false`.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import authctl
from .models import AccountLimits, LimitWindow
from .statusline import state_dir

DEFAULT_MODEL = "claude-fable-5-1"
DEFAULT_INTERVAL = timedelta(seconds=300)
_TIMEOUT = 120
_PROMPT = "quota"
_SYSTEM = "Reply with the single word: ok"

_KINDS = {
    "five_hour": ("session", "5h"),
    "seven_day": ("weekly_all", "week (all)"),
    "seven_day_overage_included": ("weekly_scoped", None),  # label from the model
}


def probe_dir() -> Path:
    """An empty, private working directory so no project CLAUDE.md is loaded."""
    return state_dir().parent / "probe"


def probe_args(model: str) -> list[str]:
    return [
        "-p",
        _PROMPT,
        "--model",
        model,
        "--max-turns",
        "1",
        "--output-format",
        "stream-json",
        "--verbose",
        "--system-prompt",
        _SYSTEM,
        "--tools",
        "",
        "--no-session-persistence",
    ]


def scoped_label(config_dir: Path, model: str) -> str:
    """The display name for the model-scoped window, e.g. "Fable".

    Claude Code labels it from its overage-included-models allowlist, which it
    caches in the identity file; fall back to the model id's family name.
    """
    from .usage import is_default_config_dir

    identity = (
        config_dir.parent / ".claude.json"
        if is_default_config_dir(config_dir)
        else config_dir / ".claude.json"
    )
    try:
        features = json.loads(identity.read_text()).get("cachedGrowthBookFeatures") or {}
        names = features.get("tengu_usage_overage_included_models")
        if isinstance(names, list) and names and isinstance(names[0], str):
            return names[0]
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    parts = model.split("-")
    return parts[1].capitalize() if len(parts) > 1 else model


def parse_stream(text: str) -> tuple[dict | None, dict | None]:
    """The last rate_limit_event's unifiedWindows and the result's usage."""
    windows = None
    usage = None
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "rate_limit_event":
            found = (event.get("rate_limit_info") or {}).get("unifiedWindows")
            if isinstance(found, dict):
                windows = found
        elif event.get("type") == "result":
            found = event.get("usage")
            if isinstance(found, dict):
                usage = found
    return windows, usage


def windows_from_unified(unified: dict, scoped: str) -> list[LimitWindow]:
    windows = []
    for key, (kind, label) in _KINDS.items():
        entry = unified.get(key)
        if not isinstance(entry, dict):
            continue
        utilization = entry.get("utilization")
        if not isinstance(utilization, (int, float)):
            continue
        reset = entry.get("resetsAt")
        windows.append(
            LimitWindow(
                kind=kind,
                label=label or f"week ({scoped})",
                percent=max(0.0, min(100.0, float(utilization) * 100)),
                resets_at=datetime.fromtimestamp(reset, tz=timezone.utc)
                if isinstance(reset, (int, float))
                else None,
                severity="normal",
                is_active=False,
            )
        )
    if windows:
        fullest = max(windows, key=lambda w: w.percent)
        windows = [
            LimitWindow(
                w.kind, w.label, w.percent, w.resets_at, w.severity, w is fullest, w.has_data
            )
            for w in windows
        ]
    return windows


def run_probe(
    account_name: str,
    config_dir: Path,
    model: str = DEFAULT_MODEL,
    now: datetime | None = None,
    tier: str | None = None,
    email: str | None = None,
) -> AccountLimits:
    """Run one probe turn for the account and return its limits.

    Failures come back as source "none" with `retriable` set, so the monitor
    backs off exactly as it does for a failed endpoint fetch.
    """
    now = now or datetime.now(timezone.utc)

    def absent(error: str) -> AccountLimits:
        return AccountLimits(
            account_name, tier, [], "none", None, error=error, retriable=True, email=email
        )

    binary = authctl.find_claude_binary()
    if binary is None:
        return absent("probe: claude binary not found")
    cwd = probe_dir()
    try:
        cwd.mkdir(parents=True, exist_ok=True)
    except OSError:
        cwd = Path.home()
    try:
        result = subprocess.run(
            [binary, *probe_args(model)],
            env=authctl.claude_env(config_dir),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return absent(f"probe: {error.__class__.__name__}")
    unified, _usage = parse_stream(result.stdout)
    if not unified:
        tail = (result.stderr or result.stdout).strip().splitlines()
        detail = tail[-1][:80] if tail else f"exit {result.returncode}"
        return absent(f"probe: no rate limit info ({detail})")
    windows = windows_from_unified(unified, scoped_label(config_dir, model))
    if not windows:
        return absent("probe: empty rate limit info")
    return AccountLimits(account_name, tier, windows, "probe", now, email=email)
