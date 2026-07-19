"""Fixture tests for the undocumented, version-internal formats.

Registry files, transcript lines, the usage JSON, and Codex usage JSON are the
surfaces most likely to drift on a Claude Code / Codex update. These pin the
current parsing so a format change fails a test rather than silently degrading.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from cctop import codex_usage, pricing
from cctop.registry import process_alive, read_registry
from cctop.transcript import TranscriptTailer, find_transcript
from cctop.usage import parse_windows

# -- registry ------------------------------------------------------------------


def test_read_registry_parses_and_skips_malformed(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "100.json").write_text(
        json.dumps(
            {
                "pid": 100,
                "sessionId": "sess-1",
                "cwd": "/work",
                "name": "task",
                "status": "busy",
                "version": "2.1.197",
                "updatedAt": 1_780_000_000_000,
            }
        )
    )
    (sessions / "bad.json").write_text("{not json")
    (sessions / "nopid.json").write_text(json.dumps({"cwd": "/x"}))

    result = read_registry(tmp_path)

    assert len(result) == 1
    session = result[0]
    assert session.pid == 100
    assert session.session_id == "sess-1"
    assert session.raw_status == "busy"
    assert isinstance(session.updated_at, datetime)


def test_read_registry_missing_dir_is_empty(tmp_path: Path) -> None:
    assert read_registry(tmp_path) == []


def test_process_alive_for_self() -> None:
    assert process_alive(os.getpid()) is True


# -- pricing -------------------------------------------------------------------


def test_pricing_lookup_and_context_window() -> None:
    assert pricing.pricing_for("claude-opus-4-8") is not None
    assert pricing.pricing_for("claude-opus-4-8[1m]") is not None  # tier suffix stripped
    assert pricing.pricing_for("claude-unknown-9") is None
    assert pricing.context_window_for("claude-haiku-4-5") == 200_000
    assert pricing.context_window_for("nope") == pricing.DEFAULT_CONTEXT_WINDOW


def test_cost_for_usage_known_model() -> None:
    usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 10,
        "cache_creation_input_tokens": 20,
    }
    cost = pricing.cost_for_usage("claude-opus-4-8", usage)
    # 100*5e-6 + 50*25e-6 + 10*5e-6*0.1 + 20*5e-6*1.25
    assert cost is not None
    assert abs(cost - 0.00188) < 1e-9


def test_cost_for_unpriced_model_is_none() -> None:
    assert pricing.cost_for_usage("claude-unknown-9", {"input_tokens": 100}) is None


# -- transcript ----------------------------------------------------------------


def _assistant_line(model: str, **usage: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-07-17T12:00:00Z",
            "message": {"model": model, "usage": usage},
        }
    )


def test_tailer_accumulates_incrementally(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(_assistant_line("claude-opus-4-8", input_tokens=100, output_tokens=50) + "\n")
    tailer = TranscriptTailer(path)
    tailer.poll()
    assert tailer.model == "claude-opus-4-8"
    assert tailer.totals().input_tokens == 100

    with path.open("a") as handle:
        handle.write(_assistant_line("claude-opus-4-8", input_tokens=5, output_tokens=1) + "\n")
    tailer.poll()
    assert tailer.totals().input_tokens == 105
    assert tailer.totals().output_tokens == 51


def test_tailer_leaves_trailing_partial_line(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    full = _assistant_line("claude-opus-4-8", input_tokens=10)
    path.write_text(full + "\n" + full[:20])  # second line has no newline yet
    tailer = TranscriptTailer(path)
    tailer.poll()
    assert tailer.totals().input_tokens == 10  # only the completed line counted

    with path.open("a") as handle:
        handle.write(full[20:] + "\n")  # complete the second line
    tailer.poll()
    assert tailer.totals().input_tokens == 20


def test_tailer_skips_synthetic_and_keeps_cost(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(
        _assistant_line("claude-opus-4-8", input_tokens=100)
        + "\n"
        + _assistant_line("<synthetic>", input_tokens=999)
        + "\n"
    )
    tailer = TranscriptTailer(path)
    tailer.poll()
    assert tailer.model == "claude-opus-4-8"  # synthetic did not overwrite
    assert tailer.totals().input_tokens == 100  # synthetic usage not counted
    assert tailer.totals().cost_usd is not None  # cost not nulled


def test_tailer_unpriced_model_nulls_cost(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(_assistant_line("claude-unknown-9", input_tokens=100) + "\n")
    tailer = TranscriptTailer(path)
    tailer.poll()
    assert tailer.totals().cost_usd is None


def test_find_transcript_by_session_id(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "some-slug"
    project.mkdir(parents=True)
    (project / "sess-42.jsonl").write_text("")
    assert find_transcript(tmp_path, "sess-42") == project / "sess-42.jsonl"
    assert find_transcript(tmp_path, "missing") is None


# -- usage JSON ----------------------------------------------------------------


def test_parse_windows_labels_and_skips_bad() -> None:
    payload = {
        "limits": [
            {
                "kind": "session",
                "percent": 12.5,
                "resets_at": "2026-07-17T17:00:00Z",
                "severity": "normal",
                "is_active": True,
            },
            {"kind": "weekly_all", "percent": 30, "severity": "warning"},
            {"kind": "weekly_scoped", "percent": 5, "scope": {"model": {"display_name": "Fable"}}},
            "garbage",
            {"kind": "session", "percent": "not-a-number"},
        ]
    }
    windows = parse_windows(payload)
    labels = [w.label for w in windows]
    assert labels == ["5h", "week (all)", "week (Fable)"]
    assert windows[0].is_active is True
    assert windows[0].resets_at is not None
    assert all(w.has_data for w in windows)


def test_parse_windows_no_data_and_percent_guard() -> None:
    payload = {
        "limits": [
            {"kind": "session", "percent": 0},  # 0% + no reset -> not started
            {"kind": "weekly_all", "percent": 1784329334},  # epoch leak -> unusable
            {"kind": "weekly_scoped", "percent": 100.4, "resets_at": "2026-07-18T00:00:00Z"},
        ]
    }
    windows = parse_windows(payload)
    assert windows[0].has_data is False  # never started
    assert windows[1].has_data is False and windows[1].percent == 0.0  # guarded
    assert windows[2].has_data is True and windows[2].percent == 100.0  # clamped


# -- Codex usage JSON ----------------------------------------------------------


def test_codex_window_label_by_duration() -> None:
    assert codex_usage._window_label(18000) == "5h"
    assert codex_usage._window_label(604800) == "week"
    assert codex_usage._window_label(86400) == "1d"
    assert codex_usage._window_label(3600) == "1h"


def test_codex_windows_shorten_and_mark_peak() -> None:
    payload = {
        "rate_limit": {
            "primary_window": {"used_percent": 20, "limit_window_seconds": 18000},
            "secondary_window": {"used_percent": 40, "limit_window_seconds": 604800},
        },
        "additional_rate_limits": [
            {
                "limit_name": "GPT-5.3-Codex-Spark",
                "rate_limit": {
                    "primary_window": {"used_percent": 60, "limit_window_seconds": 18000}
                },
            }
        ],
    }
    windows = codex_usage._windows(payload)
    labels = [w.label for w in windows]
    assert labels == ["5h", "week", "Spark 5h"]  # verbose model name shortened
    active = [w for w in windows if w.is_active]
    assert len(active) == 1 and active[0].percent == 60  # peak marked binding
