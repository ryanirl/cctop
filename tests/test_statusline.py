"""Statusline-fed limits: record, read, prefer when fresh, fall back sanely.

The hook records what Claude Code's statusline document says; the monitor and
one-shot snapshot prefer a fresh record, never call the usage endpoint for a
long-lived token (it refuses them), and keep a stale record over an error.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cctop.monitor as monitor_mod
from cctop import collect, statusline
from cctop.collect import Account
from cctop.models import AccountLimits
from cctop.monitor import FleetMonitor

T0 = datetime(2026, 9, 11, 21, 0, 0, tzinfo=timezone.utc)

PAYLOAD = {
    "session_id": "abc",
    "cwd": "/Users/me/proj",
    "model": {"display_name": "Fable 5.1"},
    "transcript_path": "/Users/me/.claude-2/projects/-Users-me-proj/abc.jsonl",
    "rate_limits": {
        "five_hour": {"used_percentage": 37, "resets_at": 1789164000},
        "seven_day": {"used_percentage": 14.4, "resets_at": 1789390800},
    },
}


def _state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def test_record_roundtrip_and_windows(monkeypatch, tmp_path: Path) -> None:
    _state(monkeypatch, tmp_path)
    cfg = tmp_path / ".claude-2"
    entry = statusline.record(PAYLOAD, cfg, T0)
    assert entry and entry["rate_limits"]["five_hour"]["used_percentage"] == 37.0

    limits = statusline.limits_from_record("cc-2", cfg, T0, email="me@x")
    assert limits is not None and limits.source == "statusline"
    assert limits.fetched_at == T0 and limits.email == "me@x"
    by_kind = {w.kind: w for w in limits.windows}
    assert by_kind["session"].percent == 37.0 and by_kind["session"].is_active
    assert by_kind["weekly_all"].percent == 14.4 and not by_kind["weekly_all"].is_active
    assert by_kind["session"].resets_at == datetime.fromtimestamp(1789164000, tz=timezone.utc)


def test_record_ignores_documents_without_limits(monkeypatch, tmp_path: Path) -> None:
    _state(monkeypatch, tmp_path)
    assert statusline.record({"cwd": "/x"}, tmp_path / ".claude", T0) is None
    assert statusline.read_record(tmp_path / ".claude") is None


def test_config_dir_resolution(monkeypatch) -> None:
    assert statusline.config_dir_from_payload(
        PAYLOAD, {"CLAUDE_CONFIG_DIR": "/h/.claude-9"}
    ) == Path("/h/.claude-9")
    assert statusline.config_dir_from_payload(PAYLOAD, {}) == Path("/Users/me/.claude-2")
    assert statusline.config_dir_from_payload({}, {}) == Path.home() / ".claude"


def test_freshness(monkeypatch, tmp_path: Path) -> None:
    _state(monkeypatch, tmp_path)
    cfg = tmp_path / ".claude"
    statusline.record(PAYLOAD, cfg, T0)
    limits = statusline.limits_from_record("cc-0", cfg, T0)
    assert statusline.is_fresh(limits, T0 + timedelta(minutes=5))
    assert not statusline.is_fresh(limits, T0 + statusline.FRESH + timedelta(seconds=1))
    assert not statusline.is_fresh(None, T0)


def test_format_line() -> None:
    assert statusline.format_line(PAYLOAD) == "Fable 5.1 │ proj │ 5h 37% │ wk 14%"
    assert statusline.format_line({}) == ""


def test_hook_main_records_and_prints(monkeypatch, tmp_path: Path, capsys) -> None:
    import io

    _state(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude-2"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(PAYLOAD)))
    assert statusline.main([]) == 0
    assert capsys.readouterr().out.strip() == "Fable 5.1 │ proj │ 5h 37% │ wk 14%"
    assert statusline.read_record(tmp_path / ".claude-2") is not None


def test_hook_exec_passes_document_through(monkeypatch, tmp_path: Path, capsys) -> None:
    import io

    _state(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(PAYLOAD)))
    assert (
        statusline.main(
            ["--exec", "python3 -c 'import sys,json; print(json.load(sys.stdin)[\"cwd\"])'"]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == "/Users/me/proj"
    assert statusline.read_record(tmp_path / ".claude") is not None  # recorded before delegating


# -- settings.json wiring ------------------------------------------------------


def test_install_wraps_existing_and_uninstall_restores(tmp_path: Path) -> None:
    cfg = tmp_path / ".claude"
    cfg.mkdir()
    settings = cfg / "settings.json"
    settings.write_text(
        json.dumps(
            {"model": "opus", "statusLine": {"type": "command", "command": "my-status --fancy"}}
        )
    )

    statusline.install(cfg)
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"
    assert statusline.HOOK_NAME in data["statusLine"]["command"]
    assert "--exec 'my-status --fancy'" in data["statusLine"]["command"]
    assert (cfg / "settings.json.cctop.bak").exists()
    assert "already installed" in statusline.install(cfg)

    statusline.uninstall(cfg)
    assert json.loads(settings.read_text())["statusLine"] == {
        "type": "command",
        "command": "my-status --fancy",
    }


def test_install_from_nothing_and_uninstall_removes(tmp_path: Path) -> None:
    cfg = tmp_path / ".claude-1"
    cfg.mkdir()
    statusline.install(cfg)
    data = json.loads((cfg / "settings.json").read_text())
    assert data["statusLine"]["type"] == "command" and "--exec" not in data["statusLine"]["command"]
    statusline.uninstall(cfg)
    assert "statusLine" not in json.loads((cfg / "settings.json").read_text())
    assert "not installed" in statusline.uninstall(cfg)


# -- monitor policy --------------------------------------------------------------


def _api(pct: float) -> AccountLimits:
    from cctop.models import LimitWindow

    return AccountLimits(
        "cc-0", "max", [LimitWindow("session", "5h", pct, None, "normal", True)], "api", T0
    )


def _monitor(monkeypatch, responses: list[AccountLimits], long_lived: bool = False):
    calls = [0]
    stream = iter(responses)

    def stub(account: Account) -> AccountLimits:
        calls[0] += 1
        return next(stream)

    monkeypatch.setattr(monitor_mod, "account_limits", stub)
    monkeypatch.setattr(monitor_mod.authctl, "is_long_lived_token", lambda d: long_lived)
    monkeypatch.setattr(monitor_mod.authctl, "ensure_fresh", lambda *a, **k: None)
    monkeypatch.setattr("cctop.usage.oauth_account", lambda d: {"emailAddress": "me@x"})
    return FleetMonitor([Account("cc-0", Path("/x"), "claude")]), calls


def _api_full(pct: float) -> AccountLimits:
    from cctop.models import LimitWindow

    windows = [
        LimitWindow("session", "5h", pct, None, "normal", False),
        LimitWindow("weekly_all", "week (all)", pct, None, "normal", False),
        LimitWindow("weekly_scoped", "week (Fable)", 71.0, None, "normal", True),
    ]
    return AccountLimits("cc-0", "max", windows, "api", T0)


def test_normal_login_keeps_endpoint_and_takes_fresher_numbers(monkeypatch, tmp_path: Path) -> None:
    _state(monkeypatch, tmp_path)
    statusline.record(PAYLOAD, Path("/x"), T0 + timedelta(minutes=1))  # newer than the fetch
    monitor, calls = _monitor(monkeypatch, [_api_full(50.0)])
    result = monitor.poll_limits(T0 + timedelta(minutes=2), force=True)[0]
    assert calls[0] == 1 and result.source == "api"
    by_kind = {w.kind: w for w in result.windows}
    assert by_kind["session"].percent == 37.0  # refreshed from the statusline
    assert by_kind["weekly_all"].percent == 14.4
    assert by_kind["weekly_scoped"].percent == 71.0  # only the endpoint has this
    assert result.statusline_at == T0 + timedelta(minutes=1)


def test_older_record_does_not_override_endpoint(monkeypatch, tmp_path: Path) -> None:
    _state(monkeypatch, tmp_path)
    statusline.record(PAYLOAD, Path("/x"), T0 - timedelta(minutes=5))
    monitor, _ = _monitor(monkeypatch, [_api_full(50.0)])
    result = monitor.poll_limits(T0, force=True)[0]
    assert {w.kind: w.percent for w in result.windows}["session"] == 50.0
    assert result.statusline_at is None


def test_stale_record_yields_to_api(monkeypatch, tmp_path: Path) -> None:
    _state(monkeypatch, tmp_path)
    statusline.record(PAYLOAD, Path("/x"), T0 - timedelta(hours=1))
    monitor, calls = _monitor(monkeypatch, [_api(50.0)])
    result = monitor.poll_limits(T0, force=True)
    assert result[0].source == "api" and result[0].windows[0].percent == 50.0
    assert result[0].statusline_at is None
    assert calls[0] == 1


def test_long_lived_token_never_fetches(monkeypatch, tmp_path: Path) -> None:
    _state(monkeypatch, tmp_path)
    monitor, calls = _monitor(monkeypatch, [_api(50.0)], long_lived=True)
    result = monitor.poll_limits(T0, force=True)
    assert result[0].source == "none" and "statusline install" in (result[0].error or "")
    assert calls[0] == 0

    statusline.record(PAYLOAD, Path("/x"), T0)  # even an old record beats the error
    result = monitor.poll_limits(T0 + timedelta(hours=3), force=True)
    assert result[0].source == "statusline" and calls[0] == 0


def test_failed_fetch_falls_back_to_stale_record(monkeypatch, tmp_path: Path) -> None:
    _state(monkeypatch, tmp_path)
    statusline.record(PAYLOAD, Path("/x"), T0)
    limited = AccountLimits(
        "cc-0", None, [], "none", None, error="usage: rate limited (429)", retriable=True
    )
    monitor, _ = _monitor(monkeypatch, [limited])
    result = monitor.poll_limits(T0 + timedelta(hours=1), force=True)
    assert result[0].source == "statusline"


def test_snapshot_uses_record(monkeypatch, tmp_path: Path) -> None:
    _state(monkeypatch, tmp_path)
    cfg = tmp_path / ".claude"
    cfg.mkdir()
    statusline.record(PAYLOAD, cfg, T0 + timedelta(minutes=1))  # newer than the fetch at T0
    monkeypatch.setattr(collect, "account_limits", lambda a: _api_full(1.0))
    monkeypatch.setattr("cctop.authctl.is_long_lived_token", lambda d: False)
    result = collect.resolve_limits(Account("cc-0", cfg, "claude"), T0 + timedelta(minutes=2))
    assert result.source == "api"
    assert {w.kind: w.percent for w in result.windows} == {
        "session": 37.0,
        "weekly_all": 14.4,
        "weekly_scoped": 71.0,
    }
    assert result.statusline_at == T0 + timedelta(minutes=1)
