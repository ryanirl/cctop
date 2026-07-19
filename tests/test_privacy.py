"""The user-facing exports must never carry a credential.

A regression guard for the trust guarantee: if someone later threads a token
into a model or the JSON snapshot, this fails loudly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from cctop.cli import _snapshot_to_dict
from cctop.models import (
    AccountLimits,
    FleetSnapshot,
    LimitWindow,
    Session,
    SessionState,
    UsageTotals,
)
from cctop.status import SessionStatus

_FORBIDDEN = ("accesstoken", "refreshtoken", "bearer", "authorization", "credential")


def _snapshot() -> FleetSnapshot:
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    session = Session(
        pid=1,
        session_id="abc123",
        cwd="/x",
        name="work",
        raw_status="idle",
        kind="claude",
        version="1",
        started_at=None,
        updated_at=now,
        status_updated_at=now,
    )
    state = SessionState(
        session=session,
        account="cc-0",
        alive=True,
        status=SessionStatus.IDLE,
        model="claude-x",
        context=None,
        totals=UsageTotals(1, 1, 1, 1, 0.0),
        last_activity=now,
    )
    window = LimitWindow("session", "5h", 10.0, now, "normal", True)
    limits = AccountLimits("cc-0", "max", [window], "api", now)
    return FleetSnapshot(taken_at=now, sessions=[state], limits=[limits])


def test_json_export_carries_no_credential() -> None:
    blob = json.dumps(_snapshot_to_dict(_snapshot())).lower()
    for needle in _FORBIDDEN:
        assert needle not in blob


def test_models_have_no_token_fields() -> None:
    for model in (AccountLimits, SessionState, Session, UsageTotals, LimitWindow):
        fields = getattr(model, "__dataclass_fields__", {})
        for name in fields:
            assert "token" not in name.lower() or name == "total_tokens" or name.endswith("_tokens")
