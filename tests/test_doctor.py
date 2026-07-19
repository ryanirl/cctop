"""The doctor command is read-only and must run cleanly on any account set."""

from __future__ import annotations

from pathlib import Path

from cctop.cli import _cmd_doctor
from cctop.collect import Account


def test_doctor_runs_on_given_accounts(tmp_path: Path, capsys) -> None:
    config_dir = tmp_path / ".claude-x"
    config_dir.mkdir()
    _cmd_doctor([Account("cc-x", config_dir, "claude")])

    out = capsys.readouterr().out
    assert "cctop doctor" in out
    assert "cc-x" in out


def test_doctor_handles_no_accounts(tmp_path: Path, capsys) -> None:
    _cmd_doctor([])
    assert "cctop doctor" in capsys.readouterr().out
