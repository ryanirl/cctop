"""The default account's stores are the home-level ones; explicit dirs never
borrow them unverified.

Claude Code keeps the default (~/.claude) account's identity in ~/.claude.json
and its credential under the plain Keychain service; only an explicit
CLAUDE_CONFIG_DIR uses the per-dir stores. These lock in the split: cctop must
never probe the default account through the per-dir stores (which forks a
parallel login), and must never present the default account's token or numbers
as another account's.
"""

from __future__ import annotations

import json
from pathlib import Path

from cctop import authctl, usage


def _fake_home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_dir = tmp_path / ".claude"
    default_dir.mkdir()
    return default_dir


# -- which stores belong to which account --------------------------------------


def test_default_dir_is_recognized(tmp_path: Path, monkeypatch) -> None:
    default_dir = _fake_home(monkeypatch, tmp_path)
    assert authctl.is_default_config_dir(default_dir) is True
    assert authctl.is_default_config_dir(tmp_path / ".claude-1") is False


def test_keychain_services_default_prefers_plain_service(tmp_path: Path, monkeypatch) -> None:
    default_dir = _fake_home(monkeypatch, tmp_path)
    services = authctl.keychain_services(default_dir)
    assert services[0] == "Claude Code-credentials"
    assert services[1].startswith("Claude Code-credentials-")


def test_keychain_services_explicit_dir_only_its_own(tmp_path: Path, monkeypatch) -> None:
    _fake_home(monkeypatch, tmp_path)
    services = authctl.keychain_services(tmp_path / ".claude-1")
    assert len(services) == 1
    assert services[0].startswith("Claude Code-credentials-")
    assert services[0] != "Claude Code-credentials"


# -- delegated claude runs must not fork the default identity ------------------


def test_claude_env_pins_explicit_dir(tmp_path: Path, monkeypatch) -> None:
    _fake_home(monkeypatch, tmp_path)
    env = authctl.claude_env(tmp_path / ".claude-1")
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".claude-1")


def test_claude_env_drops_variable_for_default_dir(tmp_path: Path, monkeypatch) -> None:
    default_dir = _fake_home(monkeypatch, tmp_path)
    # Even an inherited CLAUDE_CONFIG_DIR must not leak into the default run:
    # claude with the variable set (to anything) uses the per-dir stores.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(default_dir))
    env = authctl.claude_env(default_dir)
    assert "CLAUDE_CONFIG_DIR" not in env


# -- identity: home-level ~/.claude.json is the default's truth ----------------


def _write_identity(path: Path, email: str, uuid: str) -> None:
    path.write_text(json.dumps({"oauthAccount": {"emailAddress": email, "accountUuid": uuid}}))


def test_oauth_account_default_prefers_home_level(tmp_path: Path, monkeypatch) -> None:
    default_dir = _fake_home(monkeypatch, tmp_path)
    _write_identity(tmp_path / ".claude.json", "real@default", "uuid-real")
    _write_identity(default_dir / ".claude.json", "forked@per-dir", "uuid-fork")

    assert usage.oauth_account(default_dir)["accountUuid"] == "uuid-real"


def test_oauth_account_default_falls_back_to_in_dir(tmp_path: Path, monkeypatch) -> None:
    default_dir = _fake_home(monkeypatch, tmp_path)
    _write_identity(default_dir / ".claude.json", "only@per-dir", "uuid-fork")

    assert usage.oauth_account(default_dir)["accountUuid"] == "uuid-fork"


def test_oauth_account_explicit_dir_reads_its_own(tmp_path: Path, monkeypatch) -> None:
    _fake_home(monkeypatch, tmp_path)
    account_dir = tmp_path / ".claude-1"
    account_dir.mkdir()
    _write_identity(tmp_path / ".claude.json", "default@home", "uuid-default")
    _write_identity(account_dir / ".claude.json", "second@own", "uuid-second")

    assert usage.oauth_account(account_dir)["accountUuid"] == "uuid-second"


# -- tokens: a borrowed default token is flagged and never shown unverified ----


def test_resolve_token_marks_default_fallback_as_borrowed(tmp_path: Path, monkeypatch) -> None:
    _fake_home(monkeypatch, tmp_path)
    account_dir = tmp_path / ".claude-1"
    account_dir.mkdir()
    lookups = {"Claude Code-credentials": "default-token"}
    monkeypatch.setattr(usage, "_keychain_lookup", lambda service: lookups.get(service))

    token, borrowed = usage.resolve_token(account_dir)
    assert token == "default-token"
    assert borrowed is True


def test_resolve_token_own_keychain_is_not_borrowed(tmp_path: Path, monkeypatch) -> None:
    _fake_home(monkeypatch, tmp_path)
    account_dir = tmp_path / ".claude-1"
    account_dir.mkdir()
    own_service = authctl.keychain_services(account_dir)[0]
    lookups = {own_service: "own-token", "Claude Code-credentials": "default-token"}
    monkeypatch.setattr(usage, "_keychain_lookup", lambda service: lookups.get(service))

    assert usage.resolve_token(account_dir) == ("own-token", False)


def test_resolve_token_default_dir_never_borrows(tmp_path: Path, monkeypatch) -> None:
    default_dir = _fake_home(monkeypatch, tmp_path)
    lookups = {"Claude Code-credentials": "default-token"}
    monkeypatch.setattr(usage, "_keychain_lookup", lambda service: lookups.get(service))

    # The plain service IS the default account's own store, not a fallback.
    assert usage.resolve_token(default_dir) == ("default-token", False)


def test_fetch_limits_refuses_borrowed_token_without_identity(tmp_path: Path, monkeypatch) -> None:
    _fake_home(monkeypatch, tmp_path)
    account_dir = tmp_path / ".claude-1"
    account_dir.mkdir()
    monkeypatch.setattr(usage, "resolve_token", lambda config_dir: ("default-token", True))

    def _never_called(url, token):
        raise AssertionError("must not fetch with an unverifiable borrowed token")

    monkeypatch.setattr(usage, "_get", _never_called)

    limits = usage.fetch_account_limits("cc-1", account_dir)
    assert limits.source == "none"
    assert "no own credential" in (limits.error or "")
