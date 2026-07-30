"""Hot switching preserves saved logins and rotates only at the configured edge."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cctop.collect import Account
from cctop.models import AccountLimits, LimitWindow
from cctop.switcher import (
    active_account,
    auth_recovery_account,
    auto_switch,
    best_account,
    ensure_stable_accounts,
    recover_auth,
    switch_account,
    sync_active_profile,
)


def _profile(
    path: Path,
    org: str,
    token: str,
    expires_at: datetime | None = None,
) -> None:
    expires_at = expires_at or datetime(2030, 1, 1, tzinfo=timezone.utc)
    path.mkdir()
    (path / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"organizationUuid": org, "emailAddress": f"{org}@x"}})
    )
    (path / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": token,
                    "refreshToken": f"refresh-{token}",
                    "expiresAt": int(expires_at.timestamp() * 1000),
                }
            }
        )
    )


def _limits(name: str, percent: float) -> AccountLimits:
    return AccountLimits(
        name,
        "max",
        [LimitWindow("session", "5h", percent, None, "normal", True)],
        "api",
        datetime.now(timezone.utc),
    )


def test_switch_syncs_rotated_main_credential_back_to_previous_profile(tmp_path: Path) -> None:
    main = tmp_path / "main"
    previous = tmp_path / "saved-a"
    target = tmp_path / "saved-b"
    _profile(main, "org-a", "rotated-a")
    _profile(previous, "org-a", "stale-a")
    _profile(target, "org-b", "token-b")
    (main / ".claude.json").write_text(
        json.dumps({"keep": True, "oauthAccount": {"organizationUuid": "org-a"}})
    )
    accounts = [Account("a", previous), Account("b", target)]

    result = switch_account(accounts, accounts[1], main)

    assert result.ok is True
    assert (
        json.loads((previous / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"]
        == "rotated-a"
    )
    assert (
        json.loads((main / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"]
        == "token-b"
    )
    identity = json.loads((main / ".claude.json").read_text())
    assert identity["keep"] is True
    assert identity["oauthAccount"]["organizationUuid"] == "org-b"
    assert active_account(accounts, main) == accounts[1]


def test_sync_active_profile_persists_main_token_rotation(tmp_path: Path) -> None:
    main = tmp_path / "main"
    saved = tmp_path / "saved"
    _profile(main, "org-a", "rotated-a")
    _profile(saved, "org-a", "stale-a")
    accounts = [Account("a", saved)]

    result = sync_active_profile(accounts, main)

    assert result is not None and result.ok is True
    assert (
        json.loads((saved / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"]
        == "rotated-a"
    )


def test_switch_refuses_mutable_main_as_only_saved_profile(tmp_path: Path) -> None:
    main = tmp_path / "main"
    target = tmp_path / "saved-b"
    _profile(main, "org-a", "token-a")
    _profile(target, "org-b", "token-b")
    accounts = [Account("a", main), Account("b", target)]

    result = switch_account(accounts, accounts[1], main)

    assert result.ok is False
    assert "mutable main directory" in result.message
    assert active_account(accounts, main) == accounts[0]


def test_main_only_login_gets_automatic_managed_profile(tmp_path: Path, monkeypatch) -> None:
    main = tmp_path / "main"
    _profile(main, "org-a", "token-a")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    accounts = [Account("org-a@x", main)]

    prepared = ensure_stable_accounts(accounts, main)

    managed = tmp_path / "config" / "cctop" / "profiles" / "org-a"
    assert prepared == [Account("org-a@x", main, credential_dir=managed)]
    assert (
        json.loads((managed / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"]
        == "token-a"
    )
    assert (
        json.loads((managed / ".claude.json").read_text())["oauthAccount"]["organizationUuid"]
        == "org-a"
    )


def test_best_account_chooses_most_headroom_and_skips_active(tmp_path: Path) -> None:
    main = tmp_path / "main"
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    _profile(main, "org-a", "main-a")
    _profile(a, "org-a", "a")
    _profile(b, "org-b", "b")
    _profile(c, "org-c", "c")
    accounts = [Account("a", a), Account("b", b), Account("c", c)]

    chosen = best_account(accounts, [_limits("a", 99), _limits("b", 40), _limits("c", 10)], main)

    assert chosen == accounts[2]


def test_best_account_rejects_stale_usage_for_an_expired_profile(tmp_path: Path) -> None:
    main = tmp_path / "main"
    active = tmp_path / "active"
    expired = tmp_path / "expired"
    ready = tmp_path / "ready"
    now = datetime.now(timezone.utc)
    _profile(main, "org-a", "main-a")
    _profile(active, "org-a", "active-a")
    _profile(expired, "org-b", "expired-b", now - timedelta(minutes=1))
    _profile(ready, "org-c", "ready-c", now + timedelta(hours=6))
    accounts = [
        Account("active", active),
        Account("expired", expired),
        Account("ready", ready),
    ]

    chosen = best_account(
        accounts,
        [_limits("active", 92), _limits("expired", 10), _limits("ready", 20)],
        main,
        now=now,
    )

    assert chosen == accounts[2]


def test_best_account_selects_successful_profile_with_no_usage_yet(tmp_path: Path) -> None:
    main = tmp_path / "main"
    active = tmp_path / "active"
    fresh = tmp_path / "fresh"
    _profile(main, "org-a", "main-a")
    _profile(active, "org-a", "saved-a")
    _profile(fresh, "org-b", "fresh-b")
    accounts = [Account("active", active), Account("fresh", fresh)]
    no_usage_yet = AccountLimits(
        "fresh",
        "max",
        [LimitWindow("session", "5h", 0, None, "normal", True, has_data=False)],
        "api",
        datetime.now(timezone.utc),
    )

    chosen = best_account(accounts, [_limits("active", 90), no_usage_yet], main)

    assert chosen == accounts[1]


def test_auto_switch_fires_at_one_percent_remaining(tmp_path: Path) -> None:
    main = tmp_path / "main"
    a, b = tmp_path / "a", tmp_path / "b"
    _profile(main, "org-a", "rotated-a")
    _profile(a, "org-a", "saved-a")
    _profile(b, "org-b", "token-b")
    accounts = [Account("a", a), Account("b", b)]

    assert auto_switch(accounts, [_limits("a", 98.9), _limits("b", 10)], main, 1.0) is None
    result = auto_switch(accounts, [_limits("a", 99.0), _limits("b", 10)], main, 1.0)

    assert result is not None and result.ok is True
    assert result.active == "b"


def test_auto_switch_refuses_a_target_with_less_headroom(tmp_path: Path) -> None:
    """A worse candidate would re-trigger rotation and is not an escape."""
    main = tmp_path / "main"
    a, b = tmp_path / "a", tmp_path / "b"
    _profile(main, "org-a", "rotated-a")
    _profile(a, "org-a", "saved-a")
    _profile(b, "org-b", "token-b")
    accounts = [Account("a", a), Account("b", b)]

    result = auto_switch(accounts, [_limits("a", 92), _limits("b", 95)], main, 10.0)

    assert result is not None and result.ok is False
    assert "no healthy profile" in result.message
    assert active_account(accounts, main) == accounts[0]


def test_exhausted_account_switches_to_imperfect_but_better_profile(tmp_path: Path) -> None:
    main = tmp_path / "main"
    exhausted = tmp_path / "exhausted"
    better = tmp_path / "better"
    _profile(main, "org-a", "main-a")
    _profile(exhausted, "org-a", "saved-a")
    _profile(better, "org-b", "token-b")
    accounts = [Account("exhausted", exhausted), Account("better", better)]

    result = auto_switch(
        accounts,
        [_limits("exhausted", 100), _limits("better", 93)],
        main,
        10.0,
    )

    assert result is not None and result.ok is True
    assert result.active == "better"


def test_switch_leaves_credential_and_identity_agreeing_when_it_fails(tmp_path: Path) -> None:
    """A target with no identity must not strand the target's token in main."""
    main = tmp_path / "main"
    previous = tmp_path / "saved-a"
    target = tmp_path / "saved-b"
    _profile(main, "org-a", "rotated-a")
    _profile(previous, "org-a", "stale-a")
    _profile(target, "org-b", "token-b")
    (target / ".claude.json").write_text(json.dumps({"oauthAccount": {}}))
    accounts = [Account("a", previous), Account("b", target)]

    result = switch_account(accounts, accounts[1], main)

    assert result.ok is False
    assert active_account(accounts, main) == accounts[0]
    # Main still holds account a's token, matching the identity it still claims,
    # so the next sync cannot copy b's token over a's saved profile.
    assert (
        json.loads((main / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"]
        == "rotated-a"
    )
    assert not list(main.glob("*.cctop-new"))


def test_sync_skips_the_write_when_the_profile_is_already_current(tmp_path: Path) -> None:
    main = tmp_path / "main"
    saved = tmp_path / "saved"
    _profile(main, "org-a", "token-a")
    _profile(saved, "org-a", "token-a")
    credentials = saved / ".credentials.json"
    before = credentials.stat().st_mtime_ns

    assert sync_active_profile([Account("a", saved)], main) is not None
    assert credentials.stat().st_mtime_ns == before


def test_auth_recovery_uses_longest_lived_profile_when_usage_is_rate_limited(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main"
    short = tmp_path / "short"
    long = tmp_path / "long"
    now = datetime.now(timezone.utc)
    _profile(main, "org-a", "dead", now - timedelta(minutes=1))
    _profile(short, "org-b", "short", now + timedelta(minutes=10))
    _profile(long, "org-c", "long", now + timedelta(hours=6))
    accounts = [Account("short", short), Account("long", long)]
    unavailable = [
        AccountLimits(
            account.name,
            "max",
            [],
            "none",
            None,
            error="usage: rate limited (429)",
            retriable=True,
        )
        for account in accounts
    ]

    assert auth_recovery_account(accounts, unavailable, main, now) == accounts[1]


def test_auth_recovery_does_not_copy_dead_main_over_saved_profile(tmp_path: Path) -> None:
    main = tmp_path / "main"
    saved = tmp_path / "saved"
    now = datetime.now(timezone.utc)
    _profile(main, "org-a", "dead-main", now - timedelta(minutes=1))
    _profile(saved, "org-a", "valid-saved", now + timedelta(hours=6))
    accounts = [Account("a", saved)]
    unavailable = [
        AccountLimits(
            "a",
            "max",
            [],
            "none",
            None,
            error="usage: rate limited (429)",
            retriable=True,
        )
    ]

    result = recover_auth(accounts, unavailable, main, "needs re-login", now)

    assert result.ok is True
    assert (
        json.loads((main / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"]
        == "valid-saved"
    )
    assert (
        json.loads((saved / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"]
        == "valid-saved"
    )
