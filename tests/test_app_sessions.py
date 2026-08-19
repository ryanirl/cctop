"""The sessions table must tolerate two live processes sharing one session id.

`claude --resume <id>` while the original process is still running (hung, or in
another terminal) yields two registry files with the same sessionId. Keying
rows by session id raised DataTable's DuplicateKey and crashed the whole TUI.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from textual.widgets import DataTable

from cctop.app import CctopApp
from cctop.models import Session, SessionState, SessionStatus, UsageTotals


def _state(pid: int, session_id: str, status: SessionStatus, account: str = "cc-1") -> SessionState:
    now = datetime(2026, 8, 19, 16, 14, tzinfo=timezone.utc)
    return SessionState(
        session=Session(
            pid=pid,
            session_id=session_id,
            cwd="/Users/ryan/master/emulated",
            name=f"emulated-{pid}",
            raw_status="idle",
            kind="interactive",
            version="2.1.235",
            started_at=now,
            updated_at=now,
            status_updated_at=now,
        ),
        account=account,
        alive=True,
        status=status,
        model="claude-fable-5",
        context=None,
        totals=UsageTotals(0, 0, 0, 0, 0.0),
        last_activity=now,
    )


def test_two_processes_sharing_a_session_id_both_render() -> None:
    shared = "d0881df5-ad79-4aff-bf6c-d4b119b7af2d"
    states = [
        _state(73865, shared, SessionStatus.IDLE),
        _state(80044, shared, SessionStatus.STALE),
        _state(42547, "80f70e61-ba6d-411f-a291-b1aa48c8d2ba", SessionStatus.STALE, account="cc-0"),
    ]

    async def inner():
        app = CctopApp([])
        async with app.run_test():
            now = datetime.now(timezone.utc)
            app._render_sessions(states, now)
            table = app.query_one("#sessions", DataTable)
            row_count = table.row_count
            # Selecting either duplicate resolves to *its own* process, not the
            # other one with the same session id.
            app._selected_row_key = "cc-1:80044"
            app._render_detail(now)
            return row_count, app._states_by_key["cc-1:80044"].session.pid

    assert asyncio.run(inner()) == (3, 80044)
