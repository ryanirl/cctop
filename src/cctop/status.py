"""Normalize the raw registry `status` string into a typed enum.

The registry writes a free-form status string that is undocumented and can gain
new values between Claude Code releases. We map the values we know about and
pass anything else through as UNKNOWN rather than crashing, so a new status in a
future release degrades to a visible label instead of a stack trace.
"""

from __future__ import annotations

from enum import Enum


class SessionStatus(Enum):
    """A session's live activity, normalized across registry string variants."""

    IDLE = "idle"
    SHELL = "shell"
    GENERATING = "generating"
    WAITING_PERMISSION = "waiting_permission"
    STALE = "stale"
    DEAD = "dead"
    UNKNOWN = "unknown"


# Raw registry strings we have observed or reasonably expect, mapped to the
# normalized enum. Observed on 2.1.197: "idle", "shell", "busy".
_RAW_TO_STATUS: dict[str, SessionStatus] = {
    "idle": SessionStatus.IDLE,
    "shell": SessionStatus.SHELL,
    "tool": SessionStatus.SHELL,
    "bash": SessionStatus.SHELL,
    "busy": SessionStatus.GENERATING,
    "running": SessionStatus.GENERATING,
    "generating": SessionStatus.GENERATING,
    "thinking": SessionStatus.GENERATING,
    "working": SessionStatus.GENERATING,
    "active": SessionStatus.GENERATING,
    "compacting": SessionStatus.GENERATING,
    "waiting": SessionStatus.WAITING_PERMISSION,
    "waiting_permission": SessionStatus.WAITING_PERMISSION,
    "permission": SessionStatus.WAITING_PERMISSION,
}


def normalize_status(raw: str | None) -> SessionStatus:
    """Best-effort map of the registry's raw status string to SessionStatus."""
    if not raw:
        return SessionStatus.UNKNOWN
    return _RAW_TO_STATUS.get(raw.lower(), SessionStatus.UNKNOWN)
