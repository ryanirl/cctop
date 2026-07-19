"""Account reuse/resolution: fill in a logged-out dir, don't strand it."""

from __future__ import annotations

import json
from pathlib import Path

from cctop import cli


def _login(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
    )


def test_reusable_dir_picks_lowest_logged_out(tmp_path: Path) -> None:
    _login(tmp_path / ".claude-1")  # signed in -> skip
    (tmp_path / ".claude-2").mkdir()  # logged out -> reuse
    (tmp_path / ".claude-3").mkdir()  # logged out but higher index

    assert cli.reusable_logged_out_dir(tmp_path) == tmp_path / ".claude-2"


def test_reusable_dir_none_when_all_logged_in(tmp_path: Path) -> None:
    _login(tmp_path / ".claude-1")
    _login(tmp_path / ".claude-2")

    assert cli.reusable_logged_out_dir(tmp_path) is None


def test_reusable_dir_ignores_non_matching_dirs(tmp_path: Path) -> None:
    (tmp_path / ".claude-notanumber").mkdir()
    (tmp_path / "unrelated").mkdir()

    assert cli.reusable_logged_out_dir(tmp_path) is None


def test_resolve_config_dir_by_path(tmp_path: Path) -> None:
    assert cli.resolve_config_dir(str(tmp_path)) == tmp_path


def test_resolve_config_dir_unknown_is_none(tmp_path: Path) -> None:
    assert cli.resolve_config_dir("no-such-account-or-path-xyz") is None
