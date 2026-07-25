"""The optional config.toml layers over auto-detection: rename, hide, add.

Also covers relaxed discovery (named ~/.claude-* dirs, not just numeric) and
that `config init` writes a valid, re-parseable starter without clobbering.
"""

from __future__ import annotations

from pathlib import Path

from cctop import cli, collect
from cctop import config as cfg
from cctop.collect import Account

# -- config file loading -------------------------------------------------------


def test_load_config_parses_settings_and_accounts(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[settings]\n"
        "limits_refresh_seconds = 300\n\n"
        'main_config_dir = "~/.claude-main"\n'
        "hot_switch = true\n"
        "auto_switch_remaining_percent = 1\n\n"
        '[[account]]\nname = "work"\ndir = "~/.claude"\n\n'
        'switch_dir = "~/.claude-work-saved"\n\n'
        '[[account]]\ndir = "~/.claude-9"\nhidden = true\n'
    )
    config = cfg.load_config(path)
    assert config.limits_refresh_seconds(180.0) == 300.0
    assert config.main_config_dir() == Path.home() / ".claude-main"
    assert config.hot_switch() is True
    assert config.auto_switch_remaining_percent() == 1.0
    assert len(config.accounts) == 2
    assert config.accounts[0].name == "work"
    assert config.accounts[0].switch_dir == Path.home() / ".claude-work-saved"
    assert config.accounts[1].hidden is True


def test_load_config_missing_or_bad_is_empty(tmp_path: Path) -> None:
    assert cfg.load_config(tmp_path / "nope.toml").accounts == []
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = not [valid toml")
    assert cfg.load_config(bad).accounts == []


def test_limits_refresh_default_fallback(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text("[settings]\nlimits_refresh_seconds = -5\n")  # invalid -> default
    assert cfg.load_config(path).limits_refresh_seconds(180.0) == 180.0


# -- resolve: config overrides layered over auto-detection ---------------------


def _detected() -> list[Account]:
    home = Path.home()
    return [
        Account("cc-0", home / ".claude", "claude"),
        Account("cc-9", home / ".claude-9", "claude"),
        Account("cx-0", home / ".codex", "codex"),
    ]


def test_resolve_renames_hides_and_adds(monkeypatch) -> None:
    monkeypatch.setattr(collect, "discover_accounts", _detected)
    home = Path.home()
    config = cfg.Config(
        accounts=[
            cfg.AccountOverride(
                dir=home / ".claude",
                name="work",
                switch_dir=home / ".claude-work-saved",
            ),
            cfg.AccountOverride(dir=home / ".claude-9", hidden=True),  # hide
            cfg.AccountOverride(dir=Path("/tmp/extra"), name="extra"),  # add
        ]
    )
    result = collect.resolve_accounts(config)
    names = [a.name for a in result]
    assert names == ["work", "cx-0", "extra"]  # renamed, hidden dropped, addition appended
    assert result[0].auth_dir == home / ".claude-work-saved"
    assert result[-1].config_dir == Path("/tmp/extra")


def test_resolve_no_config_is_pure_detection(monkeypatch) -> None:
    monkeypatch.setattr(collect, "discover_accounts", _detected)
    result = collect.resolve_accounts(cfg.Config())
    assert [a.name for a in result] == ["cc-0", "cc-9", "cx-0"]


def test_hot_switch_matches_duplicate_logins_without_account_mapping(
    tmp_path: Path, monkeypatch
) -> None:
    main = tmp_path / ".claude"
    saved = tmp_path / ".claude-saved"
    other = tmp_path / ".claude-other"
    for path, org, email in (
        (main, "org-a", "a@example.com"),
        (saved, "org-a", "a@example.com"),
        (other, "org-b", "b@example.com"),
    ):
        path.mkdir()
        (path / ".claude.json").write_text(
            '{"oauthAccount":{"organizationUuid":"' + org + '","emailAddress":"' + email + '"}}'
        )
        (path / ".credentials.json").write_text('{"claudeAiOauth":{"accessToken":"token"}}')
    monkeypatch.setattr(
        collect,
        "discover_accounts",
        lambda: [
            Account("cc-0", main),
            Account("cc-saved", saved),
            Account("cc-other", other),
        ],
    )

    result = collect.resolve_accounts(
        cfg.Config(settings={"hot_switch": True, "main_config_dir": str(main)})
    )

    assert result == [
        Account("a@example.com", main, credential_dir=saved),
        Account("b@example.com", other, credential_dir=other),
    ]


# -- relaxed discovery ---------------------------------------------------------


def test_discover_catches_named_dirs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (".claude", ".claude-1", ".claude-work"):
        (tmp_path / name / "sessions").mkdir(parents=True)  # looks like a config dir

    names = [a.name for a in collect.discover_accounts()]
    assert "cc-0" in names and "cc-1" in names and "cc-work" in names


# -- config init ---------------------------------------------------------------


def test_config_init_writes_and_refuses_overwrite(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = cfg.config_path()

    cli._cmd_config(["init"])
    assert path.exists()
    # the generated file must itself be valid, re-parseable TOML
    assert cfg.load_config(path) is not None
    capsys.readouterr()

    cli._cmd_config(["init"])  # second time: refuse without --force
    assert "overwriting" in capsys.readouterr().out.replace("\n", " ")


def test_save_config_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    accounts = [
        cfg.AccountOverride(
            dir=Path("/x/.claude"),
            name="work",
            provider="claude",
            switch_dir=Path("/x/.claude-work-saved"),
        ),
        cfg.AccountOverride(dir=Path("/x/.claude-1"), name="alt", provider="claude", hidden=True),
    ]
    cfg.save_config(
        {
            "limits_refresh_seconds": 90,
            "heatmap_weeks": 10,
            "main_config_dir": "~/.claude",
            "hot_switch": True,
        },
        accounts,
        path,
    )

    loaded = cfg.load_config(path)
    assert loaded.limits_refresh_seconds(180.0) == 90.0
    assert loaded.heatmap_weeks(26) == 10
    assert loaded.main_config_dir() == Path.home() / ".claude"
    assert loaded.hot_switch() is True
    assert loaded.accounts[0].switch_dir == Path("/x/.claude-work-saved")
    assert {o.name: o.hidden for o in loaded.accounts} == {"work": False, "alt": True}


def test_build_rows_reflects_config() -> None:
    from cctop.settings_screen import build_rows

    detected = [
        Account("cc-0", Path("/x/.claude"), "claude"),
        Account("cc-1", Path("/x/.claude-1"), "claude"),
    ]
    config = cfg.Config(
        accounts=[cfg.AccountOverride(dir=Path("/x/.claude"), name="renamed", hidden=True)]
    )
    rows = build_rows(detected, config)
    assert rows[0].name == "renamed" and rows[0].hidden is True
    assert rows[1].name == "cc-1" and rows[1].hidden is False
