"""Read the live-process registry at ~/.claude/sessions/*.json.

Each file is named by the process id and describes one running Claude Code
process. This is the load-bearing data source: it gives us the process table
(pid, cwd, name, status) for free, with no configuration and no hooks. The
format is undocumented and version-internal, so every field is read defensively.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .models import Session


def _epoch_ms_to_datetime(value: object) -> datetime | None:
    """Parse a millisecond epoch (the registry's timestamp format) to UTC."""
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _parse_session_file(path: Path) -> Session | None:
    """Parse one registry file, returning None if it is malformed."""
    try:
        record = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or "pid" not in record:
        return None

    return Session(
        pid=int(record["pid"]),
        session_id=str(record.get("sessionId", "")),
        cwd=str(record.get("cwd", "")),
        name=str(record.get("name", "")),
        raw_status=str(record.get("status", "")),
        kind=str(record.get("kind", "")),
        version=str(record.get("version", "")),
        started_at=_epoch_ms_to_datetime(record.get("startedAt")),
        updated_at=_epoch_ms_to_datetime(record.get("updatedAt")),
        status_updated_at=_epoch_ms_to_datetime(record.get("statusUpdatedAt")),
    )


def read_registry(config_dir: Path) -> list[Session]:
    """Every parseable session record under <config_dir>/sessions/."""
    sessions_dir = config_dir / "sessions"
    if not sessions_dir.is_dir():
        return []

    sessions = []
    for path in sessions_dir.glob("*.json"):
        session = _parse_session_file(path)
        if session is not None:
            sessions.append(session)

    return sessions


def process_alive(pid: int) -> bool:
    """Whether a pid names a live process (signal 0 probes without killing)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but is owned by another user; still alive.
        return True
    return True
