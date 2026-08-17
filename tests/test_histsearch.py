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
    model: str | None = None,
) -> str:
    message: dict = {"role": role, "content": text}
    if model is not None:
        message["model"] = model
    return json.dumps(
        {
            "type": role,
            "message": message,
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
    import os

    config_dir = tmp_path / ".claude"
    project = config_dir / "projects" / "-tmp-project"
    project.mkdir(parents=True)
    # Search results order by session last-activity (file mtime), the same
    # ordering the browse listing uses; set explicit mtimes to make it
    # deterministic. The older stamp goes on the file written LAST so the test
    # would catch accidental write-order sorting.
    sessions = [
        ("eeee5555-0000-0000-0000-000000000000", 2_000_000_000),
        ("eeee5555-0000-0000-0000-000000000001", 1_000_000_000),
    ]
    for session, mtime in sessions:
        path = project / f"{session}.jsonl"
        path.write_text(_claude_line("needle", session_id=session) + "\n")
        os.utime(path, (mtime, mtime))

    account = Account("cc-0", config_dir)
    result = histsearch.search_history("needle", [account], backend=PY_BACKEND)
    assert [match.session_id for match in result.sessions] == [name for name, _ in sessions]
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


def test_resume_plan_default_account_drops_config_dir_env(tmp_path: Path, monkeypatch) -> None:
    # The default account resumes WITHOUT CLAUDE_CONFIG_DIR (even inherited):
    # pinning it to ~/.claude forks Claude Code onto a parallel per-dir
    # identity instead of the login the user's own `claude` command uses.
    account = _write_claude_account(
        tmp_path, "one", "needle", "aaaa1111-0000-0000-0000-000000000016"
    )
    result = histsearch.search_history("needle", [account], backend=PY_BACKEND)
    match = result.sessions[0]

    default_account = Account(account.name, tmp_path / ".claude")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("cctop.authctl.find_claude_binary", lambda: "/usr/local/bin/claude")
    plan = histsearch.resume_plan(match, [default_account])

    assert plan is not None
    assert plan.env_extra == {}
    assert plan.env_drop == ("CLAUDE_CONFIG_DIR",)
    assert "unset CLAUDE_CONFIG_DIR" in histsearch._new_terminal_script(plan)


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


def test_within_dir_filters_by_session_cwd(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    project = config_dir / "projects" / "-tmp-project"
    project.mkdir(parents=True)

    inside = "aaaa9999-0000-0000-0000-000000000001"
    (project / f"{inside}.jsonl").write_text(_claude_line("needle here", session_id=inside) + "\n")
    outside = "aaaa9999-0000-0000-0000-000000000002"
    line = json.loads(_claude_line("needle there", session_id=outside))
    line["cwd"] = "/somewhere/else"
    (project / f"{outside}.jsonl").write_text(json.dumps(line) + "\n")

    account = Account("cc-0", config_dir)
    result = histsearch.search_history(
        "needle", [account], within=Path("/tmp/project"), backend=PY_BACKEND
    )

    assert [match.session_id for match in result.sessions] == [inside]

    # A subdirectory cwd still counts as within the parent.
    sub = histsearch.search_history("needle", [account], within=Path("/tmp"), backend=PY_BACKEND)
    assert len(sub.sessions) == 1


def test_within_dir_filters_codex_via_head_read(tmp_path: Path) -> None:
    account = _write_codex_account(tmp_path)

    hit = histsearch.search_history(
        "codex needle", [account], within=Path("/tmp/codex-project"), backend=PY_BACKEND
    )
    miss = histsearch.search_history(
        "codex needle", [account], within=Path("/nowhere"), backend=PY_BACKEND
    )

    assert len(hit.sessions) == 1
    assert miss.sessions == []


def test_read_conversation_returns_dialogue_in_order(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    lines = [
        json.dumps({"type": "ai-title", "aiTitle": "t"}),
        _claude_line("first question"),
        _claude_line("the answer", role="assistant"),
        # Tool-only content must not appear in the dialogue view.
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
                },
                "timestamp": "2026-07-01T12:00:02Z",
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n")

    messages, truncated = histsearch.read_conversation(path, "claude")

    assert [(m.role, m.text) for m in messages] == [
        ("user", "first question"),
        ("assistant", "the answer"),
    ]
    assert not truncated


def test_read_conversation_caps_keep_the_tail(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    lines = [_claude_line(f"message {index}") for index in range(10)]
    path.write_text("\n".join(lines) + "\n")

    messages, truncated = histsearch.read_conversation(path, "claude", max_messages=3)

    assert truncated
    assert [m.text for m in messages] == ["message 7", "message 8", "message 9"]


def test_tail_messages_returns_last_dialogue(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    lines = [_claude_line(f"message {index}") for index in range(10)]
    path.write_text("\n".join(lines) + "\n")

    tail = histsearch.tail_messages(path, "claude", count=3)

    assert [message.text for message in tail] == ["message 7", "message 8", "message 9"]


def test_tail_messages_bounded_window_on_large_file(tmp_path: Path) -> None:
    # A transcript bigger than the tail window: the (partial) first line of
    # the window is dropped and only the trailing messages are parsed.
    path = tmp_path / "big.jsonl"
    padding = json.dumps({"type": "file-history-snapshot", "snapshot": "x" * 400_000})
    lines = [padding, _claude_line("early message"), padding, _claude_line("final message")]
    path.write_text("\n".join(lines) + "\n")

    tail = histsearch.tail_messages(path, "claude", count=4)

    assert [message.text for message in tail] == ["final message"]


def test_new_terminal_script_pins_account_and_cwd(tmp_path: Path) -> None:
    plan = histsearch.ResumePlan(
        argv=["/usr/local/bin/claude", "--resume", "abc"],
        env_extra={"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-1")},
        cwd=tmp_path,
    )

    script = histsearch._new_terminal_script(plan)

    assert f"cd {tmp_path}" in script
    assert f"export CLAUDE_CONFIG_DIR={tmp_path}/.claude-1" in script
    assert script.rstrip().endswith("exec /usr/local/bin/claude --resume abc")


def test_path_filter_matches_cwd_substring(tmp_path: Path) -> None:
    account = _write_claude_account(
        tmp_path, "pf", "needle text", "abcd0000-0000-0000-0000-000000000010"
    )

    hit = histsearch.search_history("needle", [account], path_filter="Project", backend=PY_BACKEND)
    miss = histsearch.search_history(
        "needle", [account], path_filter="unrelated-repo", backend=PY_BACKEND
    )

    assert len(hit.sessions) == 1  # case-insensitive substring of /tmp/project
    assert miss.sessions == []


def test_path_filter_combines_with_dir_scope(tmp_path: Path) -> None:
    account = _write_claude_account(
        tmp_path, "pfd", "needle text", "abcd0000-0000-0000-0000-000000000011"
    )

    both = histsearch.search_history(
        "needle",
        [account],
        within=Path("/tmp"),
        path_filter="project",
        backend=PY_BACKEND,
    )
    conflicting = histsearch.search_history(
        "needle",
        [account],
        within=Path("/tmp"),
        path_filter="elsewhere",
        backend=PY_BACKEND,
    )

    assert len(both.sessions) == 1
    assert conflicting.sessions == []


def test_list_sessions_browses_newest_first(tmp_path: Path) -> None:
    import os

    account = _write_claude_account(
        tmp_path, "old", "old talk", "abcd0000-0000-0000-0000-000000000020"
    )
    newer = _write_claude_account(
        tmp_path, "new", "new talk", "abcd0000-0000-0000-0000-000000000021"
    )
    old_file = next((account.config_dir / "projects").rglob("*.jsonl"))
    os.utime(old_file, (1_000_000_000, 1_000_000_000))

    result = histsearch.list_sessions([account, newer], backend=PY_BACKEND)

    assert result.total_hits == 0
    assert [match.account for match in result.sessions] == ["cc-new", "cc-old"]
    assert all(match.hits == [] for match in result.sessions)
    assert result.sessions[0].title == "new title"
    assert result.sessions[0].last_timestamp is not None


def test_list_sessions_path_filter_and_dir_scope(tmp_path: Path) -> None:
    account = _write_claude_account(
        tmp_path, "browse", "text", "abcd0000-0000-0000-0000-000000000022"
    )

    by_name = histsearch.list_sessions([account], path_filter="project", backend=PY_BACKEND)
    by_path = histsearch.list_sessions([account], path_filter="/tmp/project", backend=PY_BACKEND)
    miss = histsearch.list_sessions([account], path_filter="other-repo", backend=PY_BACKEND)
    scoped = histsearch.list_sessions([account], within=Path("/tmp/project"), backend=PY_BACKEND)
    out_of_scope = histsearch.list_sessions([account], within=Path("/nowhere"), backend=PY_BACKEND)

    assert len(by_name.sessions) == 1
    assert len(by_path.sessions) == 1  # slug-matched against the dir name
    assert miss.sessions == []
    assert len(scoped.sessions) == 1
    assert out_of_scope.sessions == []


def test_list_sessions_includes_codex(tmp_path: Path) -> None:
    account = _write_codex_account(tmp_path)

    result = histsearch.list_sessions([account], path_filter="codex-project", backend=PY_BACKEND)

    assert len(result.sessions) == 1
    assert result.sessions[0].provider == "codex"


def test_session_metadata_columns(tmp_path: Path) -> None:
    # model from the matched assistant line, started from the head, turns from
    # the assistant-line count, provider tag from the account.
    config_dir = tmp_path / ".claude"
    project = config_dir / "projects" / "-tmp-project"
    project.mkdir(parents=True)
    session = "abcd0000-0000-0000-0000-000000000001"
    lines = [
        _claude_line("what about the needle?", session_id=session),
        _claude_line(
            "needle answer one",
            session_id=session,
            role="assistant",
            timestamp="2026-07-01T12:01:00Z",
            model="claude-opus-4-8",
        ),
        _claude_line(
            "closing remark",
            session_id=session,
            role="assistant",
            timestamp="2026-07-01T12:02:00Z",
            model="claude-opus-4-8",
        ),
    ]
    (project / f"{session}.jsonl").write_text("\n".join(lines) + "\n")

    account = Account("cc-0", config_dir)
    result = histsearch.search_history("needle", [account], backend=PY_BACKEND)

    match = result.sessions[0]
    assert match.provider == "claude"
    assert match.model == "claude-opus-4-8"
    assert match.turns == 2
    assert match.started is not None and match.started.hour == 12
    assert match.live is False


def test_live_marker_from_registry(tmp_path: Path) -> None:
    import os

    config_dir = tmp_path / ".claude"
    project = config_dir / "projects" / "-tmp-project"
    project.mkdir(parents=True)
    registry = config_dir / "sessions"
    registry.mkdir()

    alive = "abcd0000-0000-0000-0000-000000000002"
    dead = "abcd0000-0000-0000-0000-000000000003"
    for session in (alive, dead):
        (project / f"{session}.jsonl").write_text(_claude_line("needle", session_id=session) + "\n")
    # This test process's own pid is definitionally alive; 2**30 is not a
    # plausible pid on macOS.
    (registry / "1.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": alive}))
    (registry / "2.json").write_text(json.dumps({"pid": 2**30, "sessionId": dead}))

    account = Account("cc-0", config_dir)
    result = histsearch.search_history("needle", [account], backend=PY_BACKEND)

    live_by_id = {match.session_id: match.live for match in result.sessions}
    assert live_by_id == {alive: True, dead: False}


def test_codex_model_from_turn_context(tmp_path: Path) -> None:
    account = _write_codex_account(tmp_path)
    day = account.config_dir / "sessions" / "2026" / "07" / "01"
    rollout = next(day.glob("rollout-*.jsonl"))
    turn_context = json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.3-codex"}})
    rollout.write_text(rollout.read_text() + turn_context + "\n")

    result = histsearch.search_history("codex needle", [account], backend=PY_BACKEND)

    assert result.sessions[0].model == "gpt-5.3-codex"


def test_subagent_transcripts_are_skipped(tmp_path: Path) -> None:
    config_dir = tmp_path / ".claude"
    session = "ffff6666-0000-0000-0000-000000000000"
    subagents = config_dir / "projects" / "-tmp-project" / session / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-abc.jsonl").write_text(_claude_line("needle", session_id=session) + "\n")

    account = Account("cc-0", config_dir)
    assert histsearch.search_history("needle", [account], backend=PY_BACKEND).sessions == []
