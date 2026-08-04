"""History search: multi-account grouping, honest post-filtering, resume plans.

The load-bearing guarantees: a hit is only reported when the query occurs in
real message text (never in metadata like a sessionId), every result is tagged
with the account that owns it, and a resume plan pins that account's config
dir. All tests force the pure-Python backend so they run without ripgrep.
"""

from __future__ import annotations

import json
from pathlib import Path

from cctop import histsearch, ripgrep
from cctop.collect import Account

PY_BACKEND = ripgrep.Backend(name="python")


def _claude_line(
    text: str,
    session_id: str = "aaaa1111-0000-0000-0000-000000000000",
    role: str = "user",
    timestamp: str = "2026-07-01T12:00:00Z",
    sidechain: bool = False,
) -> str:
    return json.dumps(
        {
            "type": role,
            "message": {"role": role, "content": text},
            "sessionId": session_id,
            "timestamp": timestamp,
            "cwd": "/tmp/project",
            "isSidechain": sidechain,
        }
    )


def _write_claude_account(home: Path, name: str, text: str, session: str) -> Account:
    config_dir = home / f".claude-{name}"
    project = config_dir / "projects" / "-tmp-project"
    project.mkdir(parents=True)
    lines = [
        json.dumps({"type": "ai-title", "aiTitle": f"{name} title", "sessionId": session}),
        _claude_line(text, session_id=session),
        _claude_line("an unrelated reply", session_id=session, role="assistant"),
    ]
    (project / f"{session}.jsonl").write_text("\n".join(lines) + "\n")
    return Account(f"cc-{name}", config_dir)


def _write_codex_account(home: Path) -> Account:
    config_dir = home / ".codex"
    day = config_dir / "sessions" / "2026" / "07" / "01"
    day.mkdir(parents=True)
    session = "bbbb2222-0000-0000-0000-000000000000"
    lines = [
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session, "cwd": "/tmp/codex-project"},
            }
        ),
        json.dumps(
            {
                "type": "event_msg",
                "timestamp": "2026-07-01T13:00:00Z",
                "payload": {"type": "user_message", "message": "codex needle question"},
            }
        ),
    ]
    (day / f"rollout-2026-07-01T13-00-00-{session}.jsonl").write_text("\n".join(lines) + "\n")
    return Account("cx-0", config_dir, provider="codex")


def test_search_groups_by_session_and_tags_accounts(tmp_path: Path) -> None:
    one = _write_claude_account(
        tmp_path, "one", "the needle is here", "aaaa1111-0000-0000-0000-000000000001"
    )
    two = _write_claude_account(
        tmp_path, "two", "another needle appears", "aaaa1111-0000-0000-0000-000000000002"
    )

    result = histsearch.search_history("needle", [one, two], backend=PY_BACKEND)

    assert {match.account for match in result.sessions} == {"cc-one", "cc-two"}
    assert all(len(match.hits) == 1 for match in result.sessions)
    assert result.backend == "python"


def test_metadata_only_match_is_dropped(tmp_path: Path) -> None:
    # The query occurs only in the sessionId, not in any message text, so the
    # raw line matches but the post-filter must reject it.
    account = _write_claude_account(
        tmp_path, "meta", "ordinary text", "deadbeef-0000-0000-0000-000000000000"
    )

    result = histsearch.search_history("deadbeef", [account], backend=PY_BACKEND)

    assert result.sessions == []


def test_sidechain_messages_are_excluded(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    project = config_dir / "projects" / "-tmp-project"
    project.mkdir(parents=True)
    session = "cccc3333-0000-0000-0000-000000000000"
    (project / f"{session}.jsonl").write_text(
        _claude_line("needle in a sidechain", session_id=session, sidechain=True) + "\n"
    )

    account = Account("cc-0", config_dir)
    result = histsearch.search_history("needle", [account], backend=PY_BACKEND)

    assert result.sessions == []


def test_codex_rollouts_are_searched(tmp_path: Path) -> None:
    account = _write_codex_account(tmp_path)

    result = histsearch.search_history("codex needle", [account], backend=PY_BACKEND)

    assert len(result.sessions) == 1
    match = result.sessions[0]
    assert match.account == "cx-0"
    assert match.provider == "codex"
    assert match.session_id == "bbbb2222-0000-0000-0000-000000000000"
    assert match.cwd == "/tmp/codex-project"


def test_title_prefers_ai_title_and_falls_back_to_first_user(tmp_path: Path) -> None:
    account = _write_claude_account(
        tmp_path, "titled", "needle text", "aaaa1111-0000-0000-0000-000000000003"
    )
    result = histsearch.search_history("needle", [account], backend=PY_BACKEND)
    assert result.sessions[0].title == "titled title"

    config_dir = tmp_path / ".claude-untitled"
    project = config_dir / "projects" / "-tmp-project"
    project.mkdir(parents=True)
    session = "dddd4444-0000-0000-0000-000000000000"
    (project / f"{session}.jsonl").write_text(
        _claude_line("first real question with needle", session_id=session) + "\n"
    )
    bare = Account("cc-untitled", config_dir)
    result = histsearch.search_history("needle", [bare], backend=PY_BACKEND)
    assert result.sessions[0].title == "first real question with needle"


def test_regex_search(tmp_path: Path) -> None:
    account = _write_claude_account(
        tmp_path, "re", "error: OOM at step 42", "aaaa1111-0000-0000-0000-000000000004"
    )

    result = histsearch.search_history(
        r"OOM|OutOfMemory", [account], regex=True, backend=PY_BACKEND
    )

    assert len(result.sessions) == 1
    assert "OOM" in result.sessions[0].hits[0].snippet


def test_sessions_sort_newest_first_and_limit_marks_truncated(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    project = config_dir / "projects" / "-tmp-project"
    project.mkdir(parents=True)
    for index, stamp in enumerate(["2026-07-01T12:00:00Z", "2026-07-02T12:00:00Z"]):
        session = f"eeee5555-0000-0000-0000-00000000000{index}"
        (project / f"{session}.jsonl").write_text(
            _claude_line("needle", session_id=session, timestamp=stamp) + "\n"
        )

    account = Account("cc-0", config_dir)
    result = histsearch.search_history("needle", [account], backend=PY_BACKEND)
    stamps = [match.last_timestamp for match in result.sessions]
    assert stamps == sorted(stamps, reverse=True)

    limited = histsearch.search_history("needle", [account], limit_sessions=1, backend=PY_BACKEND)
    assert len(limited.sessions) == 1
    assert limited.truncated


def test_short_query_returns_nothing(tmp_path: Path) -> None:
    account = _write_claude_account(
        tmp_path, "short", "x marks", "aaaa1111-0000-0000-0000-000000000005"
    )
    assert histsearch.search_history("x", [account], backend=PY_BACKEND).sessions == []


def test_resume_plan_pins_owning_account(tmp_path: Path, monkeypatch) -> None:
    account = _write_claude_account(
        tmp_path, "one", "needle", "aaaa1111-0000-0000-0000-000000000006"
    )
    result = histsearch.search_history("needle", [account], backend=PY_BACKEND)
    match = result.sessions[0]

    monkeypatch.setattr("cctop.authctl.find_claude_binary", lambda: "/usr/local/bin/claude")
    plan = histsearch.resume_plan(match, [account])

    assert plan is not None
    assert plan.argv == ["/usr/local/bin/claude", "--resume", match.session_id]
    assert plan.env_extra == {"CLAUDE_CONFIG_DIR": str(account.config_dir)}


def test_resume_plan_none_without_binary(tmp_path: Path, monkeypatch) -> None:
    account = _write_claude_account(
        tmp_path, "one", "needle", "aaaa1111-0000-0000-0000-000000000007"
    )
    result = histsearch.search_history("needle", [account], backend=PY_BACKEND)

    monkeypatch.setattr("cctop.authctl.find_claude_binary", lambda: None)
    assert histsearch.resume_plan(result.sessions[0], [account]) is None


def test_json_escaped_query_prefilters_quoted_text(tmp_path: Path) -> None:
    # A query containing a double quote appears escaped inside the raw JSON
    # line; the prefilter must still find it and the post-filter must match the
    # decoded text.
    account = _write_claude_account(
        tmp_path, "quote", 'he said "hello there" loudly', "aaaa1111-0000-0000-0000-000000000008"
    )

    result = histsearch.search_history('"hello there"', [account], backend=PY_BACKEND)

    assert len(result.sessions) == 1


def test_subagent_transcripts_are_skipped(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    session = "ffff6666-0000-0000-0000-000000000000"
    subagents = config_dir / "projects" / "-tmp-project" / session / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-abc.jsonl").write_text(_claude_line("needle", session_id=session) + "\n")

    account = Account("cc-0", config_dir)
    assert histsearch.search_history("needle", [account], backend=PY_BACKEND).sessions == []
