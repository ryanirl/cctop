"""The one-turn claude probe for long-lived logins: parse, schedule, fall back."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cctop.monitor as monitor_mod
from cctop import quota_probe, statusline
from cctop.collect import Account
from cctop.models import AccountLimits
from cctop.monitor import _BACKOFF_BASE, FleetMonitor

T0 = datetime(2026, 9, 11, 21, 0, 0, tzinfo=timezone.utc)

STREAM = "\n".join(
    json.dumps(e)
    for e in [
        {"type": "system", "subtype": "init"},
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "unifiedWindows": {
                    "five_hour": {"utilization": 0.27, "resetsAt": 1789182000},
                    "seven_day": {"utilization": 0.21, "resetsAt": 1789390800},
                    "seven_day_overage_included": {"utilization": 0.16, "resetsAt": 1789390800},
                },
            },
        },
        {"type": "assistant", "message": {}},
        {"type": "result", "usage": {"input_tokens": 448, "output_tokens": 81}},
    ]
)


def test_parse_stream_and_windows(tmp_path: Path) -> None:
    unified, usage = quota_probe.parse_stream(STREAM + "\nnot json\n")
    assert usage == {"input_tokens": 448, "output_tokens": 81}
    windows = quota_probe.windows_from_unified(unified, "Fable")
    by_kind = {w.kind: w for w in windows}
    assert by_kind["session"].percent == 27.0
    assert by_kind["weekly_all"].percent == 21.0
    assert (
        by_kind["weekly_scoped"].label == "week (Fable)"
        and by_kind["weekly_scoped"].percent == 16.0
    )
    assert by_kind["session"].is_active and not by_kind["weekly_scoped"].is_active
    assert by_kind["weekly_scoped"].resets_at == datetime.fromtimestamp(1789390800, tz=timezone.utc)


def test_scoped_label_from_identity_allowlist(tmp_path: Path) -> None:
    cfg = tmp_path / ".claude-2"
    cfg.mkdir()
    (cfg / ".claude.json").write_text(
        json.dumps(
            {
                "cachedGrowthBookFeatures": {
                    "tengu_usage_overage_included_models": ["Fable", "Fable 5"]
                }
            }
        )
    )
    assert quota_probe.scoped_label(cfg, "claude-fable-5-1") == "Fable"
    assert quota_probe.scoped_label(tmp_path / ".claude-9", "claude-fable-5-1") == "Fable"
    assert quota_probe.scoped_label(tmp_path / ".claude-9", "claude-opus-5") == "Opus"


def test_run_probe_uses_the_claude_binary(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(quota_probe.authctl, "find_claude_binary", lambda: "/fake/claude")
    calls = []

    class Done:
        stdout, stderr, returncode = STREAM, "", 0

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return Done()

    monkeypatch.setattr(quota_probe.subprocess, "run", fake_run)
    result = quota_probe.run_probe(
        "cc-2", tmp_path / ".claude-2", "claude-fable-5-1", T0, email="me@x"
    )
    assert result.source == "probe" and result.email == "me@x" and result.fetched_at == T0
    assert {w.kind for w in result.windows} == {"session", "weekly_all", "weekly_scoped"}
    argv, kw = calls[0]
    assert argv[0] == "/fake/claude" and "--no-session-persistence" in argv and "--tools" in argv
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1"
    assert kw["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".claude-2")
    assert kw["cwd"] == str(quota_probe.probe_dir())


def test_run_probe_failure_is_retriable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(quota_probe.authctl, "find_claude_binary", lambda: "/fake/claude")

    class Failed:
        stdout, stderr, returncode = "", "Not logged in", 1

    monkeypatch.setattr(quota_probe.subprocess, "run", lambda *a, **k: Failed())
    result = quota_probe.run_probe("cc-2", tmp_path / ".claude-2")
    assert result.source == "none" and result.retriable and "Not logged in" in (result.error or "")


# -- monitor policy for long-lived logins ------------------------------------

PAYLOAD = {
    "rate_limits": {
        "five_hour": {"used_percentage": 30, "resets_at": 1789182000},
        "seven_day": {"used_percentage": 22, "resets_at": 1789390800},
    }
}


def _probe_ok(now: datetime) -> AccountLimits:
    windows = quota_probe.windows_from_unified(quota_probe.parse_stream(STREAM)[0], "Fable")
    return AccountLimits("cc-2", "max", windows, "probe", now)


def _monitor(monkeypatch, tmp_path: Path, responses, quota_probe_on: bool = True):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    calls = [0]
    stream = iter(responses)

    def stub(name, config_dir, model, now, **identity):
        calls[0] += 1
        item = next(stream)
        return item(now) if callable(item) else item

    monkeypatch.setattr(monitor_mod.quota_probe, "run_probe", stub)
    monkeypatch.setattr(monitor_mod.authctl, "is_long_lived_token", lambda d: True)
    monkeypatch.setattr(
        monitor_mod,
        "account_limits",
        lambda a: (_ for _ in ()).throw(AssertionError("endpoint must not be called")),
    )
    monkeypatch.setattr("cctop.usage.oauth_account", lambda d: {})
    monitor = FleetMonitor([Account("cc-2", Path("/x"), "claude")], quota_probe=quota_probe_on)
    return monitor, calls


def test_probe_runs_on_its_own_cadence(monkeypatch, tmp_path: Path) -> None:
    monitor, calls = _monitor(monkeypatch, tmp_path, [_probe_ok, _probe_ok])
    first = monitor.poll_limits(T0, force=True)[0]
    assert first.source == "probe" and calls[0] == 1
    assert {w.kind: w.percent for w in first.windows}["weekly_scoped"] == 16.0

    again = monitor.poll_limits(T0 + timedelta(seconds=200), force=True)[0]
    assert calls[0] == 1 and again.source == "probe"  # not due yet: cached

    monitor.poll_limits(T0 + timedelta(seconds=301), force=True)
    assert calls[0] == 2


def test_statusline_refreshes_between_probes(monkeypatch, tmp_path: Path) -> None:
    monitor, calls = _monitor(monkeypatch, tmp_path, [_probe_ok])
    monitor.poll_limits(T0, force=True)
    statusline.record(PAYLOAD, Path("/x"), T0 + timedelta(seconds=60))
    result = monitor.poll_limits(T0 + timedelta(seconds=90), force=True)[0]
    by_kind = {w.kind: w.percent for w in result.windows}
    assert by_kind == {"session": 30.0, "weekly_all": 22.0, "weekly_scoped": 16.0}
    assert result.source == "probe" and result.statusline_at == T0 + timedelta(seconds=60)
    assert calls[0] == 1


def test_probe_failure_backs_off_and_falls_back(monkeypatch, tmp_path: Path) -> None:
    failed = AccountLimits("cc-2", None, [], "none", None, error="probe: boom", retriable=True)
    monitor, calls = _monitor(monkeypatch, tmp_path, [failed, _probe_ok])
    result = monitor.poll_limits(T0, force=True)[0]
    assert result.source == "none" and "boom" in (result.error or "")
    assert monitor._cooldown_until["cc-2"] == T0 + _BACKOFF_BASE

    statusline.record(PAYLOAD, Path("/x"), T0)
    during = monitor.poll_limits(T0 + timedelta(seconds=30), force=True)[0]
    assert during.source == "statusline" and calls[0] == 1  # cooling down: statusline stands in

    after = monitor.poll_limits(T0 + timedelta(seconds=400), force=True)[0]
    assert after.source == "probe" and calls[0] == 2


def test_probe_disabled_means_statusline_only(monkeypatch, tmp_path: Path) -> None:
    monitor, calls = _monitor(monkeypatch, tmp_path, [_probe_ok], quota_probe_on=False)
    result = monitor.poll_limits(T0, force=True)[0]
    assert result.source == "none" and "statusline install" in (result.error or "")
    statusline.record(PAYLOAD, Path("/x"), T0)
    assert monitor.poll_limits(T0 + timedelta(seconds=1), force=True)[0].source == "statusline"
    assert calls[0] == 0


def test_probe_sessions_are_hidden(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert monitor_mod._is_probe_session(str(quota_probe.probe_dir()))
    assert not monitor_mod._is_probe_session("/Users/me/proj")
