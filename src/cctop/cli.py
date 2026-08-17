"""Command-line entry point.

M0 ships the one-shot views: `cctop --once` prints a limits header band plus the
current fleet table, and `cctop --json` emits the same snapshot for scripting.
The live Textual TUI is M1.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .collect import Account, build_snapshot, default_accounts
from .manage import AddPlan
from .models import AccountLimits, FleetSnapshot, LimitWindow, SessionState
from .status import SessionStatus

# Status to (label, rich style). Colors follow a quiet palette: green idle,
# yellow working, red blocked or dead, grey stale or unknown.
_STATUS_DISPLAY: dict[SessionStatus, tuple[str, str]] = {
    SessionStatus.IDLE: ("idle", "green"),
    SessionStatus.SHELL: ("shell", "yellow"),
    SessionStatus.GENERATING: ("generating", "cyan"),
    SessionStatus.WAITING_PERMISSION: ("waiting", "bold red"),
    SessionStatus.STALE: ("stale", "grey50"),
    SessionStatus.DEAD: ("dead", "red"),
    SessionStatus.UNKNOWN: ("unknown", "grey50"),
}


def _format_age(moment: datetime | None, now: datetime) -> str:
    """A compact relative age like "12s", "5m", "3h" for a past timestamp."""
    if moment is None:
        return "-"
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _format_reset(moment: datetime | None, now: datetime) -> str:
    """A compact countdown to a future reset time like "3h12m", "2d"."""
    if moment is None:
        return "?"
    seconds = int((moment - now).total_seconds())
    if seconds <= 0:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d"


def _format_tokens(count: int) -> str:
    """Human counts: 1234 -> "1.2k", 3400000 -> "3.4M"."""
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1_000_000:.1f}M"


def _format_cost(cost: float | None) -> str:
    return "-" if cost is None else f"${cost:.2f}"


def _format_context(state: SessionState) -> str:
    if state.context is None:
        return "-"
    return f"{state.context.used_fraction * 100:.0f}%"


def _format_model(model: str | None) -> str:
    if not model:
        return "-"
    return model.removeprefix("claude-")


def _short_cwd(cwd: str) -> str:
    """Show the cwd relative to home, keeping the last two path segments."""
    home = str(Path.home())
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home) :]
    parts = cwd.split("/")
    if len(parts) > 3:
        return ".../" + "/".join(parts[-2:])
    return cwd


def _bar_color(fraction: float) -> str:
    """Green below 70%, amber to 90%, red above: a quiet load ramp."""
    if fraction >= 0.9:
        return "red"
    if fraction >= 0.7:
        return "yellow"
    return "green"


def _severity_color(severity: str, fraction: float) -> str:
    """Red for any non-normal severity, else a quiet load ramp by fill."""
    if severity and severity != "normal":
        return "red"
    return _bar_color(fraction)


def _render_gauge(window: LimitWindow, now: datetime) -> str:
    """One "week (Fable)* [####    ] 14% resets 1d" gauge as rich markup."""
    if not window.has_data:
        return f"{window.label} [{'.' * 10}] [grey50]no usage yet[/grey50]"
    fraction = window.used_fraction
    filled = int(round(fraction * 10))
    color = _severity_color(window.severity, fraction)
    bar = "[" + color + "]" + "#" * filled + "[/" + color + "]" + "." * (10 - filled)
    reset = _format_reset(window.resets_at, now)
    active = "[bold]*[/bold]" if window.is_active else ""
    return (
        f"{window.label}{active} [{bar}] "
        f"[{color}]{window.percent:.0f}%[/{color}] "
        f"[grey50]resets {reset}[/grey50]"
    )


def _tier_label(tier: str | None) -> str:
    if not tier:
        return ""
    return tier.replace("default_claude_", "").replace("_", " ")


def _render_limits(limits: list[AccountLimits], now: datetime, console: Console) -> None:
    for account in limits:
        header = f"[bold]{account.account}[/bold]"
        if account.email:
            header += f" [grey50]{account.email}[/grey50]"
        tier = _tier_label(account.tier)
        if tier:
            header += f" [grey50]({tier})[/grey50]"

        if account.source != "api":
            console.print(f"{header}   [grey50]{account.error or 'no limit data'}[/grey50]")
            continue

        gauges = "   ".join(_render_gauge(w, now) for w in account.windows)
        age = _format_age(account.fetched_at, now)
        console.print(f"{header}   {gauges}   [grey50]{age} ago[/grey50]")


def _render_table(snapshot: FleetSnapshot, multi_account: bool) -> Table:
    table = Table(title="cctop", title_style="bold", expand=False)
    if multi_account:
        table.add_column("acct", style="magenta")
    table.add_column("name", style="bold")
    table.add_column("status")
    table.add_column("model")
    table.add_column("cwd", style="grey70")
    table.add_column("ctx", justify="right")
    table.add_column("tokens", justify="right")
    table.add_column("cost", justify="right")
    table.add_column("age", justify="right")

    for state in snapshot.sessions:
        label, style = _STATUS_DISPLAY[state.status]
        row = [
            state.session.name or state.session.session_id[:8],
            f"[{style}]{label}[/{style}]",
            _format_model(state.model),
            _short_cwd(state.session.cwd),
            _format_context(state),
            _format_tokens(state.totals.total_tokens),
            _format_cost(state.totals.cost_usd),
            _format_age(state.last_activity, snapshot.taken_at),
        ]
        if multi_account:
            row.insert(0, state.account)
        table.add_row(*row)

    return table


def _print_view(snapshot: FleetSnapshot, console: Console) -> None:
    if snapshot.limits:
        _render_limits(snapshot.limits, snapshot.taken_at, console)
        console.print()

    if not snapshot.sessions:
        console.print("[grey50]No live Claude Code sessions found.[/grey50]")
        return

    multi_account = len({s.account for s in snapshot.sessions}) > 1
    console.print(_render_table(snapshot, multi_account))
    console.print(
        f"[grey50]{len(snapshot.sessions)} sessions   "
        f"{_format_tokens(snapshot.total_tokens)} tokens   "
        f"${snapshot.total_cost_usd:.2f} total[/grey50]"
    )


def _window_to_dict(window: LimitWindow) -> dict:
    return {
        "kind": window.kind,
        "label": window.label,
        "percent": window.percent,
        "severity": window.severity,
        "is_active": window.is_active,
        "has_data": window.has_data,
        "resets_at": window.resets_at.isoformat() if window.resets_at else None,
    }


def _snapshot_to_dict(snapshot: FleetSnapshot) -> dict:
    return {
        "taken_at": snapshot.taken_at.isoformat(),
        "total_cost_usd": snapshot.total_cost_usd,
        "total_tokens": snapshot.total_tokens,
        "limits": [
            {
                "account": a.account,
                "email": a.email,
                "tier": a.tier,
                "source": a.source,
                "error": a.error,
                "fetched_at": a.fetched_at.isoformat() if a.fetched_at else None,
                "windows": [_window_to_dict(w) for w in a.windows],
            }
            for a in snapshot.limits
        ],
        "sessions": [
            {
                "account": s.account,
                "name": s.session.name,
                "pid": s.session.pid,
                "session_id": s.session.session_id,
                "cwd": s.session.cwd,
                "status": s.status.value,
                "model": s.model,
                "context_used_fraction": (s.context.used_fraction if s.context else None),
                "total_tokens": s.totals.total_tokens,
                "cost_usd": s.totals.cost_usd,
                "last_activity": (s.last_activity.isoformat() if s.last_activity else None),
            }
            for s in snapshot.sessions
        ],
    }


def _cmd_accounts() -> None:
    """List the discovered accounts (read-only): tier and login status."""
    from . import usage
    from .registry import read_registry

    console = Console()
    table = Table(title="cctop accounts", title_style="bold", expand=False)
    table.add_column("acct", style="magenta")
    table.add_column("email", style="grey70")
    table.add_column("config dir", style="grey70")
    table.add_column("tier")
    table.add_column("token", justify="center")
    table.add_column("live", justify="right")

    for account in default_accounts():
        identity = usage.oauth_account(account.config_dir)
        tier = _tier_label(usage.read_tier(account.config_dir)) or "-"
        has_token = usage.get_token(account.config_dir) is not None
        live = sum(1 for _ in read_registry(account.config_dir))
        table.add_row(
            account.name,
            identity.get("emailAddress") or "-",
            str(account.config_dir).replace(str(Path.home()), "~"),
            tier,
            "[green]yes[/green]" if has_token else "[grey50]no[/grey50]",
            str(live),
        )
    console.print(table)


def _cmd_doctor(accounts: list[Account] | None = None) -> None:
    """Read-only self-check for troubleshooting adoption issues.

    Reports the platform, whether the claude/codex binaries and auth are found,
    and each account's own credential, token expiry, tier, and live-session
    count. Local reads only: it makes no network calls (so it cannot 429) and
    never touches a credential beyond reading its expiry.
    """
    import platform as platform_module
    import sys

    from . import authctl, usage
    from .codex_usage import CODEX_DIR
    from .registry import read_registry

    accounts = accounts if accounts is not None else default_accounts()
    now = datetime.now(timezone.utc)
    console = Console()

    console.print("[bold]cctop doctor[/bold]  [grey50](read-only; no network)[/grey50]\n")
    console.print(
        f"platform   {platform_module.system()} {platform_module.release()}   "
        f"Python {sys.version.split()[0]}"
    )
    binary = authctl.find_claude_binary()
    console.print(f"claude     {binary or '[red]not found on PATH[/red]'}")
    codex_auth = (CODEX_DIR / "auth.json").exists()
    codex_note = "[green]auth present[/green]" if codex_auth else "[grey50]no auth[/grey50]"
    console.print(f"codex      {codex_note}  ({str(CODEX_DIR).replace(str(Path.home()), '~')})\n")

    table = Table(title="accounts", title_style="bold", expand=False)
    for column in ("acct", "provider", "config dir", "token", "expires", "tier", "live"):
        table.add_column(column)

    for account in accounts:
        config_display = str(account.config_dir).replace(str(Path.home()), "~")
        if account.provider == "codex":
            has_token = (CODEX_DIR / "auth.json").exists()
            table.add_row(
                account.name,
                "codex",
                config_display,
                "[green]yes[/green]" if has_token else "[grey50]no[/grey50]",
                "-",
                "-",
                "-",
            )
            continue

        has_token = authctl.has_credentials(account.config_dir)
        expiry = authctl.read_expiry(account.config_dir)
        expires = _format_reset(expiry, now) if expiry else "-"
        tier = _tier_label(usage.read_tier(account.config_dir)) or "-"
        live = sum(1 for _ in read_registry(account.config_dir))
        table.add_row(
            account.name,
            "claude",
            config_display,
            "[green]yes[/green]" if has_token else "[red]no[/red]",
            expires,
            tier,
            str(live),
        )

    console.print(table)
    console.print(
        "\n[grey50]Usage limits and stats are fetched live in the TUI "
        "(free, read-only); run [/grey50]cctop[grey50] to see them.[/grey50]"
    )


def reusable_logged_out_dir(home: Path) -> Path | None:
    """The lowest-index existing `.claude-N` that is not a signed-in account.

    So an account created earlier but never signed into gets filled in on the
    next add, instead of leaving it stranded and minting a fresh index. Signed
    in means: an on-disk credentials file (which cannot outlive its dir), or a
    Keychain credential CORROBORATED by the dir's own identity -- a Keychain
    entry alone can be a ghost from a deleted dir at the same path (macOS keeps
    the entry when the dir is removed), and treating one as a login is how a
    brand-new dir gets skipped and a spurious extra index minted.
    """
    from . import authctl, usage

    candidates = sorted(
        (
            path
            for path in home.glob(".claude-*")
            if path.is_dir() and path.name[len(".claude-") :].isdigit()
        ),
        key=lambda path: int(path.name[len(".claude-") :]),
    )
    for path in candidates:
        if authctl.credentials_file_present(path):
            continue
        if authctl.keychain_credential_present(path) and usage.oauth_account(path):
            continue
        return path
    return None


def run_login(config_dir: Path, console: Console) -> bool:
    """Run `claude auth login` for one account, interactively; True on success.

    Scoped to the account via authctl.claude_env (an explicit config dir is
    pinned to its own per-dir Keychain service; the default dir runs without
    CLAUDE_CONFIG_DIR so its real login is used, not a forked per-dir one), so
    signing in here can never overwrite another account's credential. stdio is
    inherited so the browser OAuth flow works; cctop writes nothing itself.
    After a successful login, warns when the new login is the same Claude
    account as an existing one -- the browser signs into whichever claude.ai
    account is already active, which is how "new" accounts silently merge.
    """
    import subprocess

    from . import authctl

    binary = authctl.find_claude_binary()
    if binary is None:
        console.print(
            "[red]claude binary not found.[/red] Open the account manually and "
            "run [bold]/login[/bold]."
        )
        return False

    console.print(f"\n[bold]Signing in[/bold] [grey50](config dir {config_dir})[/grey50]")
    console.print(
        "[grey50]The browser signs in with whichever claude.ai account is "
        "already active; for a different account, log out at claude.ai first "
        "or use a private window.[/grey50]\n"
    )
    try:
        result = subprocess.run([binary, "auth", "login"], env=authctl.claude_env(config_dir))
    except (OSError, KeyboardInterrupt):
        return False
    if result.returncode != 0:
        return False
    _warn_if_duplicate_login(config_dir, console)
    return True


def _warn_if_duplicate_login(config_dir: Path, console: Console) -> None:
    """Say so, loudly, when a fresh login landed on an already-known account."""
    from . import usage

    identity = usage.oauth_account(config_dir)
    uuid = identity.get("accountUuid")
    if not uuid:
        return
    for account in default_accounts():
        if account.provider != "claude" or account.config_dir == config_dir:
            continue
        if usage.oauth_account(account.config_dir).get("accountUuid") == uuid:
            email = identity.get("emailAddress") or "this account"
            console.print(
                f"[yellow]note: this signed in as the SAME Claude account as "
                f"{account.name} ({email}). For a separate account, log out at "
                f"claude.ai (or use a private window) and run /login again.[/yellow]"
            )
            return


def resolve_config_dir(spec: str) -> Path | None:
    """Resolve an account spec to a config dir: a known account name or a path."""
    for account in default_accounts():
        if account.name == spec:
            return account.config_dir
    path = Path(spec).expanduser()
    return path if path.is_dir() else None


def _warn_leftover_keychain_credential(plan: AddPlan, console: Console) -> None:
    """Warn when the Keychain already holds a credential for a dir this fresh.

    Keychain entries survive deleting a config dir, so a previous account at
    the same path leaves a ghost credential behind; Claude Code will silently
    adopt it instead of asking for a login, which reads as accounts merging.
    cctop never deletes credentials, so this only tells the user how to.
    """
    from . import authctl

    if not authctl.keychain_credential_present(plan.config_dir):
        return
    if plan.dir_exists and (plan.config_dir / ".claude.json").exists():
        return  # an actual signed-in account at this path, not a ghost
    service = authctl.keychain_services(plan.config_dir)[0]
    console.print(
        f"  [yellow]! the Keychain already holds a credential for this path "
        f"(left over from a previous account). Claude Code will adopt that "
        f"login instead of asking you to sign in. To start clean:[/yellow]\n"
        f"    [grey50]security delete-generic-password -s '{service}'[/grey50]"
    )


def _cmd_add_account(argv: list[str]) -> None:
    """Provision a new account: create its config dir, optionally clone an
    existing account's config, optionally set a shell alias, and (with --login)
    sign in.

    Dry-run by default. Aliasing never happens automatically: a shell alias is
    written only when --alias NAME is given. --from clones an allowlist of user
    config (CLAUDE.md, settings, commands...) from another account, never its
    credentials, identity, or sessions. Strictly additive; login is isolated to
    the new dir.
    """
    from . import manage

    parser = argparse.ArgumentParser(prog="cctop add-account")
    parser.add_argument(
        "--from",
        dest="source",
        metavar="ACCOUNT",
        default=None,
        help="Clone user config (CLAUDE.md, settings, commands...) from this "
        "account (name like cc-0, or a config-dir path).",
    )
    parser.add_argument(
        "--alias",
        metavar="NAME",
        default=None,
        help="Set this shell alias for the new account (opt-in; never automatic).",
    )
    parser.add_argument("--apply", action="store_true", help="Create the config dir.")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Apply, then run the login flow for the new account (one-step setup).",
    )
    args = parser.parse_args(argv)

    home = Path.home()
    reuse_dir = reusable_logged_out_dir(home)
    plan = manage.plan_add(home, args.alias, reuse_dir=reuse_dir)
    console = Console()

    if args.alias is not None:
        conflict = manage.alias_index_conflict(args.alias, plan.index)
        if conflict is not None:
            console.print(f"[red]--alias: {conflict}[/red]")
            return

    source_dir = None
    if args.source is not None:
        source_dir = resolve_config_dir(args.source)
        if source_dir is None:
            console.print(f"[red]--from: unknown account or path '{args.source}'[/red]")
            return
        if source_dir == plan.config_dir:
            console.print("[red]--from: source and destination are the same[/red]")
            return

    console.print(f"[bold]Add account[/bold]  config dir [magenta]{plan.config_dir}[/magenta]")
    _warn_leftover_keychain_credential(plan, console)
    if source_dir is not None:
        console.print(f"  clone from : {source_dir}  {list(manage.CONFIG_ALLOWLIST)}")
    if args.alias is not None:
        console.print(f"  alias      : {plan.alias_line}")
    else:
        console.print("  alias      : [grey50](none; pass --alias NAME to set one)[/grey50]")

    if not args.apply and not args.login:
        console.print(
            "\n[grey50]Dry run. Add [/grey50][bold]--login[/bold][grey50] for one-step "
            "setup (create dir, clone config, sign in), or [/grey50][bold]--apply[/bold]"
            "[grey50] to create the dir only.[/grey50]"
        )
        return

    console.print()
    for action in manage.ensure_config_dir(plan.config_dir):
        console.print(f"  [green]+[/green] {action}")
    if source_dir is not None:
        for action in manage.copy_config(source_dir, plan.config_dir):
            console.print(f"  [green]+[/green] {action}")
    if args.alias is not None:
        for action in manage.set_alias(plan):
            console.print(f"  [green]+[/green] {action}")

    if not args.login:
        hint = (
            f"run [magenta]{args.alias}[/magenta]"
            if args.alias
            else (f"run [magenta]CLAUDE_CONFIG_DIR={plan.config_dir} claude[/magenta]")
        )
        console.print(
            f"\n[bold]Next:[/bold] {hint} and [bold]/login[/bold] to sign in "
            f"(or re-run with --login). cctop will pick it up automatically."
        )
        return

    if run_login(plan.config_dir, console):
        console.print("\n[green]Done.[/green] Signed in; cctop will pick it up automatically.")
    else:
        console.print(
            "\n[grey50]Login not completed. The dir is set up; sign in when ready.[/grey50]"
        )


def _cmd_search(argv: list[str]) -> None:
    """`cctop search [QUERY]`: search all accounts' conversation history.

    With a terminal attached this opens the search TUI (query pre-filled when
    given) so results can be explored, read, and resumed. `--json`, or a piped
    stdout, prints instead: machine-readable JSONL (one match row per hit plus
    a summary row) or a readable table. Read-only and free either way: a
    ripgrep pass over transcript files already on disk.
    """
    import sys

    from . import histsearch

    parser = argparse.ArgumentParser(prog="cctop search")
    parser.add_argument(
        "query",
        nargs="?",
        default="",
        help="Text to search for (case-insensitive); omit to open the TUI empty.",
    )
    parser.add_argument("--regex", action="store_true", help="Treat the query as a regex.")
    parser.add_argument(
        "--account",
        action="append",
        metavar="NAME",
        default=None,
        help="Only search this account (repeatable; default: all).",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="Only sessions whose working directory is under PATH.",
    )
    parser.add_argument(
        "--path",
        default=None,
        metavar="TEXT",
        help="Only sessions whose path/cwd contains TEXT (looser than --dir).",
    )
    parser.add_argument("--limit", type=int, default=20, help="Max sessions shown (default 20).")
    parser.add_argument("--json", action="store_true", help="Emit JSONL match/summary rows.")
    args = parser.parse_args(argv)

    accounts = default_accounts()
    if args.account:
        accounts = [account for account in accounts if account.name in args.account]

    if not args.json and sys.stdout.isatty():
        from .search_screen import SearchApp

        SearchApp(
            accounts,
            initial_query=args.query,
            regex=args.regex,
            within=args.dir,
            path_filter=args.path or "",
        ).run()
        return

    if args.query:
        result = histsearch.search_history(
            args.query,
            accounts,
            regex=args.regex,
            limit_sessions=max(1, args.limit),
            within=args.dir,
            path_filter=args.path,
        )
    else:
        # No query: list recent sessions instead (still honoring the filters),
        # so `cctop search --json --path repo` doubles as a session lister.
        result = histsearch.list_sessions(
            accounts,
            limit_sessions=max(1, args.limit),
            within=args.dir,
            path_filter=args.path,
        )

    if args.json:
        for match in result.sessions:
            session_fields = {
                "account": match.account,
                "provider": match.provider,
                "project": match.project,
                "session_id": match.session_id,
                "title": match.title,
                "model": match.model,
                "live": match.live,
                "turns": match.turns,
                "started": match.started.isoformat() if match.started else None,
                "last_activity": (
                    match.last_timestamp.isoformat() if match.last_timestamp else None
                ),
                "file_path": str(match.path),
            }
            if not match.hits:
                print(json.dumps({"type": "session", **session_fields}))
            for hit in match.hits:
                row = {
                    "type": "match",
                    **session_fields,
                    "line_number": hit.line_number,
                    "role": hit.role,
                    "timestamp": hit.timestamp.isoformat() if hit.timestamp else None,
                    "content": hit.snippet,
                }
                print(json.dumps(row))
        summary = {
            "type": "summary",
            "sessions": len(result.sessions),
            "matches": result.total_hits,
            "truncated": result.truncated,
            "backend": result.backend,
        }
        print(json.dumps(summary))
        return

    from rich.text import Text

    console = Console()
    now = datetime.now(timezone.utc)
    for match in result.sessions:
        turns = f" · {match.turns} turns" if match.turns is not None else ""
        matches = f" · {len(match.hits)} match(es)" if match.hits else ""
        header = Text.assemble(
            (match.account, "bold #20B2AA"),
            (" ● " if match.live else "  ", "bold #20B2AA"),
            (f"{match.provider}  ", "grey50"),
            (match.project, "grey50"),
            ("  ", ""),
            (match.title, "default"),
            (
                f"  {_format_model(match.model)}{turns}{matches} · "
                f"{_format_age(match.last_timestamp, now)} ago",
                "grey50",
            ),
        )
        console.print(header)
        for hit in match.hits[:2]:
            line = Text.assemble(("  ", ""), (f"{hit.role}: ", "grey50"), (hit.snippet[:200], ""))
            console.print(line)
        console.print()

    if not result.sessions:
        console.print(
            "[grey50]No matches.[/grey50]" if args.query else "[grey50]No sessions.[/grey50]"
        )
        return
    tail = f"{len(result.sessions)} sessions"
    if result.total_hits:
        tail += f" · {result.total_hits} matches"
    tail += f" · {result.backend}"
    if result.truncated:
        tail += " · truncated (raise --limit or narrow the query)"
    console.print(f"[grey50]{tail}[/grey50]")


def _config_template() -> str:
    """A commented starter config pre-populated with the detected accounts."""
    from .collect import discover_accounts

    home = str(Path.home())
    lines = [
        "# cctop config. Everything here is OPTIONAL: cctop works with no config at",
        "# all (pure auto-detection). Edit only what you want to change; delete a line",
        "# to fall back to the default.",
        "",
        "[settings]",
        "# limits_refresh_seconds = 180   # how often to refetch usage limits",
        "",
        "# Accounts cctop auto-detected. Rename via `name`, hide with `hidden = true`,",
        "# reorder by moving blocks, or add your own block pointing at any config dir.",
    ]
    for account in discover_accounts():
        directory = str(account.config_dir).replace(home, "~", 1)
        lines += [
            "",
            "[[account]]",
            f'name = "{account.name}"',
            f'dir = "{directory}"',
            f'provider = "{account.provider}"',
            "# hidden = false",
        ]
    return "\n".join(lines) + "\n"


def _cmd_config(argv: list[str]) -> None:
    """`cctop config init [--force]` writes a starter config; `config path` prints it.

    Never overwrites an existing config without --force, and never creates one
    implicitly: cctop runs fine with no config file at all.
    """
    from . import config as config_module

    console = Console()
    path = config_module.config_path()
    sub = argv[0] if argv else ""

    if sub == "path":
        console.print(str(path))
        return
    if sub == "init":
        if path.exists() and "--force" not in argv:
            console.print(
                f"[grey50]{path} already exists; not overwriting (pass --force).[/grey50]"
            )
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_config_template())
        console.print(f"[green]+[/green] wrote {path}")
        console.print(
            "[grey50]Edit it to rename/hide/reorder/add accounts or tune settings.[/grey50]"
        )
        return

    console.print("usage: [bold]cctop config[/bold] [init [--force] | path]")


def _claude_available() -> bool:
    """Whether cctop can detect Claude Code: its binary or a config dir."""
    from . import authctl
    from .collect import _looks_like_config_dir

    return authctl.find_claude_binary() is not None or _looks_like_config_dir(
        Path.home() / ".claude"
    )


def _codex_available() -> bool:
    """Whether cctop can detect Codex: its binary or the ~/.codex data dir."""
    import shutil

    codex = Path.home() / ".codex"
    return (
        shutil.which("codex") is not None
        or (codex / "auth.json").exists()
        or (codex / "sessions").is_dir()
    )


def _confirm(console: Console, prompt: str) -> bool:
    """A yes/no prompt that defaults to no on anything but y/yes."""
    return console.input(f"{prompt} (y/n): ").strip().lower() in ("y", "yes")


_SETUP_PROMPT = """Help me set up cctop, a terminal monitor for my Claude Code and OpenAI Codex
accounts. It is already installed (the `cctop` command).

Goal: make sure cctop shows all my accounts, correctly labeled.

1. Run `cctop accounts` and `cctop doctor` to see what it already auto-detects
   (it finds ~/.claude, any ~/.claude-* config dir, and ~/.codex automatically).
2. Ask me how I organize my accounts if anything looks missing (config dirs in
   non-standard locations, or an account-switcher tool).
3. cctop reads an OPTIONAL config at ~/.config/cctop/config.toml. Run
   `cctop config init` to generate a starter pre-filled with detected accounts,
   then edit it. Each account block is:
     [[account]]
     name = "work"            # label shown in cctop
     dir  = "~/.claude-work"  # the account's CLAUDE_CONFIG_DIR (or ~/.codex)
     provider = "claude"      # or "codex"
     hidden = false           # true to hide it
   and [settings] supports limits_refresh_seconds and heatmap_weeks.
4. Only edit that config file, and be strictly additive: never delete or
   overwrite my credentials, sessions, or other config. Confirm before writing.
5. When done, tell me to run `cctop`.

Keep it short and interactive."""


def _launch_agent(provider: str, console: Console) -> None:
    """Launch the provider's agent, seeded with the setup prompt, to configure
    cctop interactively. cctop writes nothing itself; the agent (with the user)
    does, additively."""
    import shutil
    import subprocess

    if provider == "claude":
        from . import authctl

        binary = authctl.find_claude_binary()
    else:
        binary = shutil.which("codex")
    if binary is None:
        console.print(f"[red]{provider} binary not found on PATH.[/red]")
        return

    console.print(f"[grey50]Launching {provider} to help configure cctop...[/grey50]\n")
    try:
        subprocess.run([binary, _SETUP_PROMPT])
    except (OSError, KeyboardInterrupt):
        console.print("\n[grey50]Setup session ended.[/grey50]")


def _setup_provider(provider: str, console: Console) -> None:
    """Hand off to the provider's agent to configure cctop, behind a confirm gate.

    For the common auto-detected case the user is told they are already set; the
    hand-off is for custom layouts or signing in. cctop writes nothing itself:
    the launched claude/codex does the configuring, additively.
    """
    from .collect import discover_accounts

    label = "Claude Code" if provider == "claude" else "OpenAI Codex"
    accounts = [a for a in discover_accounts() if a.provider == provider]
    console.print(f"\n[bold]Set up {label}[/bold]\n")

    if accounts:
        console.print(f"cctop already sees your accounts: {', '.join(a.name for a in accounts)}.")
        console.print("For the common case that's all you need: just run [bold]cctop[/bold].")
        question = f"Launch {label} to help customize (rename/hide/organize, add accounts)?"
    else:
        console.print(f"No {label} account is signed in yet.")
        question = f"Launch {label} to help sign in and configure cctop?"

    if not _confirm(console, f"\n{question}"):
        console.print(
            "[grey50]Skipped. Run [/grey50]cctop[grey50], "
            "[/grey50]cctop config init[grey50], or the settings screen (,) anytime.[/grey50]"
        )
        return
    _launch_agent(provider, console)


def _cmd_setup() -> None:
    """`cctop setup`: pick a detected provider (Claude/Codex) and configure it."""
    from .setup_screen import Provider, SetupApp

    providers = [
        Provider("claude", "Claude Code", _claude_available()),
        Provider("codex", "OpenAI Codex", _codex_available()),
    ]
    app = SetupApp(providers)
    app.run()

    console = Console()
    if app.selected is None:
        console.print("[grey50]No provider set up.[/grey50]")
        return
    _setup_provider(app.selected, console)


def _resolve_accounts(args: argparse.Namespace) -> list[Account]:
    if args.account:
        accounts = []
        for spec in args.account:
            name, _, path = spec.partition("=")
            accounts.append(Account(name, Path(path).expanduser()))
        return accounts
    if args.config_dir is not None:
        return [Account("default", args.config_dir)]
    return default_accounts()


def main() -> None:
    import sys

    argv = sys.argv[1:]
    if argv[:1] == ["accounts"]:
        _cmd_accounts()
        return
    if argv[:1] == ["add-account"]:
        _cmd_add_account(argv[1:])
        return
    if argv[:1] == ["doctor"]:
        _cmd_doctor()
        return
    if argv[:1] == ["config"]:
        _cmd_config(argv[1:])
        return
    if argv[:1] == ["setup"]:
        _cmd_setup()
        return
    if argv[:1] == ["search"]:
        _cmd_search(argv[1:])
        return

    parser = argparse.ArgumentParser(
        prog="cctop",
        description="Monitor multiple Claude Code sessions across accounts.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print a single snapshot and exit (default in M0).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the snapshot as JSON instead of a table.",
    )
    parser.add_argument(
        "--account",
        action="append",
        metavar="NAME=DIR",
        help="An account as name=config_dir (repeatable). "
        "Default: cc-0=~/.claude, cc-1=~/.claude-1.",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="Single account shorthand: use this one config dir.",
    )
    parser.add_argument(
        "--include-dead",
        action="store_true",
        help="Include sessions whose process has exited.",
    )
    parser.add_argument(
        "--no-limits",
        action="store_true",
        help="Skip the free GET /api/oauth/usage fetch (no network, session table only).",
    )
    args = parser.parse_args()

    accounts = _resolve_accounts(args)

    if not args.once and not args.json:
        # Default: launch the live TUI, which polls with its own throttles.
        from . import config as config_module
        from .app import CctopApp

        cfg = config_module.load_config()
        CctopApp(
            accounts,
            limits_interval=cfg.limits_refresh_seconds(180.0),
            heatmap_weeks=cfg.heatmap_weeks(26),
            auto_refresh_tokens=cfg.auto_refresh_tokens(True),
        ).run()
        return

    now = datetime.now(timezone.utc)

    if not args.no_limits:
        # One-shot runs get the same freshness guarantee as the TUI: renew any
        # at-or-past-expiry token (delegated to the owner binary) before the
        # usage fetch, so a snapshot never opens on "token expired".
        from . import authctl
        from . import config as config_module

        if config_module.load_config().auto_refresh_tokens(True):
            for account in accounts:
                if account.provider == "claude":
                    authctl.ensure_fresh(account.name, account.config_dir, now)

    snapshot = build_snapshot(
        accounts,
        now=now,
        include_dead=args.include_dead,
        with_limits=not args.no_limits,
    )

    if args.json:
        print(json.dumps(_snapshot_to_dict(snapshot), indent=2))
        return

    _print_view(snapshot, Console())


if __name__ == "__main__":
    main()
