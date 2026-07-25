"""Hot switching preserves saved logins and rotates only at the configured edge."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from cctop.collect import Account
from cctop.models import AccountLimits, LimitWindow
from cctop.switcher import (
    active_account,
    auto_switch,
    best_account,
    ensure_stable_accounts,
    switch_account,
    sync_active_profile,
)


def _profile(path: Path, org: str, token: str) -> None:
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

    assert (
        best_account(accounts, [_limits("a", 99), _limits("b", 40), _limits("c", 10)], main)
        == (accounts[2])
    )


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
