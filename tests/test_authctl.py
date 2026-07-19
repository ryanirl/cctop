"""Credential handling must never leak one account's login into another.

The load-bearing guarantee: has_credentials / read_expiry consult only the
config dir's OWN store (its credentials file and per-dir Keychain service),
never the shared default service, so an empty dir reads as logged out even on a
machine where the default account is signed in. The delegated refresh must also
fail safe when the owner binary is absent.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cctop import authctl


def test_has_credentials_true_from_credentials_file(tmp_path: Path) -> None:
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok-abc"}})
    )
    assert authctl.has_credentials(tmp_path) is True


def test_has_credentials_false_for_empty_dir_no_default_fallback(tmp_path: Path) -> None:
    # No credentials file and a per-dir Keychain service derived from this tmp
    # path (which cannot exist), so even if the machine's default Claude Code
    # account is logged in, this must report False.
    assert authctl.has_credentials(tmp_path) is False


def test_has_credentials_false_when_oauth_block_lacks_token(tmp_path: Path) -> None:
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {}}))
    assert authctl.has_credentials(tmp_path) is False


def test_read_expiry_none_for_unknown_dir(tmp_path: Path) -> None:
    assert authctl.read_expiry(tmp_path) is None


def test_find_claude_binary_returns_str_or_none() -> None:
    result = authctl.find_claude_binary()
    assert result is None or isinstance(result, str)


def test_auth_status_errors_when_binary_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(authctl, "find_claude_binary", lambda: None)
    status = authctl.auth_status(tmp_path)
    assert status.logged_in is False
    assert status.error == "claude binary not found"


def test_refresh_fails_safe_when_binary_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(authctl, "find_claude_binary", lambda: None)
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    result = authctl.refresh("cc-9", tmp_path, now)
    assert result.ok is False
    assert "not found" in result.message
    assert result.account == "cc-9"


def test_refresh_renews_expired_token(tmp_path: Path, monkeypatch) -> None:
    now = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    expiries = iter([now - timedelta(hours=2), now + timedelta(hours=12)])  # before, after
    monkeypatch.setattr(authctl, "_run_refresh_trigger", lambda config_dir: None)
    monkeypatch.setattr(authctl, "read_expiry", lambda config_dir: next(expiries))

    result = authctl.refresh("cc-9", tmp_path, now)
    assert result.ok is True
    assert result.message == "refreshed"


def test_refresh_dead_token_needs_relogin(tmp_path: Path, monkeypatch) -> None:
    now = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(authctl, "_run_refresh_trigger", lambda config_dir: None)
    monkeypatch.setattr(authctl, "read_expiry", lambda config_dir: now - timedelta(hours=1))

    result = authctl.refresh("cc-9", tmp_path, now)
    assert result.ok is False
    assert "re-login" in result.message
