"""Assemble a FleetSnapshot across one or more accounts.

Each account is a (name, config_dir) pair: cc-0 -> ~/.claude, cc-1 ->
~/.claude-1, matching the user's shell aliases. The registry gives each
account's live process table and status; the transcript gives model, tokens,
cost, and context size; limits.py supplies the real usage-limit gauges. All of
it is combined into immutable records tagged by account.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import (
    AccountLimits,
    ContextWindow,
    FleetSnapshot,
    Session,
    SessionState,
    UsageTotals,
)
from .registry import process_alive, read_registry
from .status import SessionStatus, normalize_status
from .transcript import TranscriptTailer, find_transcript

# A session whose status has not been updated in this long is treated as stale
# even if the process is technically alive, so a wedged session reads as such.
STALE_AFTER = timedelta(minutes=10)

_EMPTY_TOTALS = UsageTotals(
    input_tokens=0,
    output_tokens=0,
    cache_creation_tokens=0,
    cache_read_tokens=0,
    cost_usd=0.0,
)


@dataclass(frozen=True)
class Account:
    """A named account backed by its own config directory.

    `provider` selects how sessions and limits are read: "claude" (the default)
    or "codex".
    """

    name: str
    config_dir: Path
    provider: str = "claude"


def default_config_dir() -> Path:
    """Where Claude Code keeps its state: CLAUDE_CONFIG_DIR or ~/.claude."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude"


def _looks_like_config_dir(path: Path) -> bool:
    """Whether a directory is a Claude Code config dir (holds its state)."""
    return (
        (path / "sessions").is_dir()
        or (path / "projects").is_dir()
        or (path / ".claude.json").exists()
    )


def _provider_for(config_dir: Path) -> str:
    """Infer the provider from a config dir path (codex vs claude)."""
    return "codex" if "codex" in config_dir.name.lower() else "claude"


def discover_accounts() -> list[Account]:
    """Every account auto-detected under the home dir.

    ~/.claude is cc-0; any ~/.claude-<suffix> that looks like a real config dir
    is picked up too (numeric suffixes sort first as cc-<N>, named ones like
    ~/.claude-work become cc-work), so it is not limited to the numeric shell-
    alias convention. ~/.codex is added as cx-0. Discovering by glob means a new
    account appears automatically, with no hardcoded list to maintain.
    """
    home = Path.home()
    found: list[tuple[tuple[int, str], Account]] = []

    base = home / ".claude"
    if _looks_like_config_dir(base):
        found.append(((0, ""), Account("cc-0", base)))

    for path in home.glob(".claude-*"):
        if not path.is_dir() or not _looks_like_config_dir(path):
            continue
        suffix = path.name[len(".claude-") :]
        order = (int(suffix), "") if suffix.isdigit() else (10_000, suffix)
        found.append((order, Account(f"cc-{suffix}", path)))

    found.sort(key=lambda item: item[0])
    accounts = [account for _, account in found] or [Account("default", default_config_dir())]

    codex_dir = home / ".codex"
    if (codex_dir / "sessions").is_dir() or (codex_dir / "auth.json").exists():
        accounts.append(Account("cx-0", codex_dir, provider="codex"))

    return accounts


def resolve_accounts(config=None) -> list[Account]:
    """Auto-detected accounts with the config file's overrides applied.

    The config can rename, hide, reorder (by listing), or add accounts pointing
    at any config dir. With no config file this returns exactly
    discover_accounts(), so cctop always works out of the box.
    """
    from . import config as config_module

    if config is None:
        config = config_module.load_config()
    overrides = {override.dir: override for override in config.accounts}

    result: list[Account] = []
    seen: set[Path] = set()
    for account in discover_accounts():
        seen.add(account.config_dir)
        override = overrides.get(account.config_dir)
        if override is not None and override.hidden:
            continue
        if override is not None:
            account = Account(
                override.name or account.name,
                account.config_dir,
                override.provider or account.provider,
            )
        result.append(account)

    for override in config.accounts:
        if override.dir in seen or override.hidden:
            continue
        result.append(
            Account(
                override.name or override.dir.name.lstrip("."),
                override.dir,
                override.provider or _provider_for(override.dir),
            )
        )

    return result


# The default account list applies the config file over auto-detection.
default_accounts = resolve_accounts


def _effective_status(
    session: Session,
    alive: bool,
    now: datetime,
) -> SessionStatus:
    """Resolve the displayed status from liveness, staleness, and raw string."""
    if not alive:
        return SessionStatus.DEAD
    if session.status_updated_at is not None:
        if now - session.status_updated_at > STALE_AFTER:
            return SessionStatus.STALE
    return normalize_status(session.raw_status)


def _state_for_session(
    session: Session,
    account: Account,
    now: datetime,
) -> SessionState:
    alive = process_alive(session.pid)
    status = _effective_status(session, alive, now)

    transcript_path = find_transcript(account.config_dir, session.session_id)
    if transcript_path is None:
        return SessionState(
            session=session,
            account=account.name,
            alive=alive,
            status=status,
            model=None,
            context=None,
            totals=_EMPTY_TOTALS,
            last_activity=session.updated_at,
        )

    tailer = TranscriptTailer(transcript_path)
    tailer.poll()

    return SessionState(
        session=session,
        account=account.name,
        alive=alive,
        status=status,
        model=tailer.model,
        context=tailer.context,
        totals=tailer.totals(),
        last_activity=tailer.last_activity or session.updated_at,
    )


def codex_session_states(account: Account, now: datetime) -> list[SessionState]:
    """Adapt live Codex sessions into the shared SessionState shape."""
    from . import codex

    states = []
    for codex_session in codex.discover_sessions():
        recent = codex_session.last_activity is not None and (
            now - codex_session.last_activity < timedelta(minutes=2)
        )
        status = SessionStatus.GENERATING if recent else SessionStatus.IDLE
        session = Session(
            pid=codex_session.pid,
            session_id=codex_session.session_id,
            cwd=codex_session.cwd,
            name=codex_session.name,
            raw_status="",
            kind="codex",
            version="",
            started_at=None,
            updated_at=codex_session.last_activity,
            status_updated_at=codex_session.last_activity,
        )
        context = (
            ContextWindow(codex_session.context_used, codex_session.context_window)
            if codex_session.context_window
            else None
        )
        # Codex is a subscription (no per-token dollar cost): totals carry the
        # cumulative token count with cost left as None.
        totals = UsageTotals(codex_session.total_tokens, 0, 0, 0, None)
        states.append(
            SessionState(
                session=session,
                account=account.name,
                alive=True,
                status=status,
                model=codex_session.model,
                context=context,
                totals=totals,
                last_activity=codex_session.last_activity,
            )
        )
    return states


def account_limits(account: Account) -> AccountLimits:
    """Fetch an account's usage limits via the right provider (free)."""
    if account.provider == "codex":
        from . import codex_usage

        return codex_usage.fetch_account_limits(account.name)

    from . import usage

    return usage.fetch_account_limits(account.name, account.config_dir)


def resolve_limits(account: Account, now: datetime | None = None) -> AccountLimits:
    """An account's limits: the statusline record when fresh, else the API.

    Long-lived (setup-token) logins never hit the API, which refuses them; a
    stale statusline record beats an error for any account whose fetch failed.
    """
    now = now or datetime.now(timezone.utc)
    if account.provider != "claude":
        return account_limits(account)

    from . import authctl, statusline, usage

    identity = usage.oauth_account(account.config_dir)
    email = identity.get("emailAddress")
    tier = identity.get("organizationRateLimitTier")
    recorded = statusline.limits_from_record(
        account.name,
        account.config_dir,
        now,
        tier=tier if isinstance(tier, str) else None,
        email=email if isinstance(email, str) else None,
    )
    if authctl.is_long_lived_token(account.config_dir):
        from . import config as config_module
        from . import quota_probe

        cfg = config_module.load_config()
        if cfg.quota_probe(True):
            probed = quota_probe.run_probe(
                account.name,
                account.config_dir,
                cfg.quota_probe_model(quota_probe.DEFAULT_MODEL),
                now,
                tier=tier if isinstance(tier, str) else None,
                email=email if isinstance(email, str) else None,
            )
            if probed.source == "probe":
                return statusline.merge(probed, recorded)
            if recorded is None:
                return probed
        if recorded is not None:
            return recorded
        return AccountLimits(
            account.name,
            None,
            [],
            "none",
            None,
            error="long-lived token: usage API refuses it; run `cctop statusline install`",
            email=email if isinstance(email, str) else None,
        )
    result = account_limits(account)
    if result.source != "api" and recorded is not None:
        return recorded
    return statusline.merge(result, recorded)


def build_snapshot(
    accounts: list[Account],
    now: datetime | None = None,
    include_dead: bool = False,
    with_limits: bool = True,
) -> FleetSnapshot:
    """Merge every account's registry, transcripts, and limits into a snapshot.

    Dead sessions (stale registry files whose process has exited) are dropped by
    default so the table shows only what is actually running. When `with_limits`
    is set, each account's usage windows are fetched live from GET
    /api/oauth/usage (free; the endpoint Claude Code's /usage uses).
    """
    now = now or datetime.now(timezone.utc)

    limits: list[AccountLimits] = []
    if with_limits:
        limits = [resolve_limits(account, now) for account in accounts]

    states: list[SessionState] = []
    for account in accounts:
        if account.provider == "codex":
            states.extend(codex_session_states(account, now))
            continue
        from .quota_probe import probe_dir

        for session in read_registry(account.config_dir):
            if session.cwd == str(probe_dir()):
                continue
            states.append(_state_for_session(session, account, now))

    if not include_dead:
        states = [state for state in states if state.alive]

    states.sort(key=lambda state: state.last_activity or now, reverse=True)

    return FleetSnapshot(taken_at=now, sessions=states, limits=limits)
