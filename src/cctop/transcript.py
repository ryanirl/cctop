"""Incrementally tail a session transcript and aggregate token usage.

The transcript lives at <config_dir>/projects/<cwd-slug>/<session_id>.jsonl and
is append-only. We track a byte offset and only parse newly appended bytes each
poll, so cost is O(new bytes) rather than O(file size): the transcripts are
already multi-megabyte, and re-reading them every tick is the one thing that
would not scale. The line schema is undocumented and version-internal, so each
line is parsed defensively.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import pricing
from .models import ContextWindow, UsageTotals


def find_transcript(config_dir: Path, session_id: str) -> Path | None:
    """Locate a session's transcript by id under projects/*/.

    We glob on the (unique) session id rather than recomputing the cwd-slug
    directory name, which is more robust to how Claude Code derives the slug.
    """
    if not session_id:
        return None
    matches = list((config_dir / "projects").glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def _parse_timestamp(value: object) -> datetime | None:
    """Parse a transcript ISO-8601 timestamp (may end in 'Z') to a datetime."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class TranscriptTailer:
    """Stateful, offset-tracking reader for one session's transcript.

    Call poll() repeatedly; each call folds any newly appended assistant-message
    usage into the running totals. A fresh tailer with offset 0 reads the whole
    file, which is what the one-shot CLI does.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0

        self._input_tokens = 0
        self._output_tokens = 0
        self._cache_creation_tokens = 0
        self._cache_read_tokens = 0
        self._cost_usd: float | None = 0.0

        self.model: str | None = None
        self.context: ContextWindow | None = None
        self.last_activity: datetime | None = None

    def poll(self) -> None:
        """Read bytes appended since the last poll and fold them into totals."""
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
        except OSError:
            return

        # Only consume up to the last complete line; leave a trailing partial
        # line for the next poll by not advancing the offset past it.
        last_newline = chunk.rfind(b"\n")
        if last_newline == -1:
            return
        consumed = chunk[: last_newline + 1]
        self.offset += len(consumed)

        for line in consumed.splitlines():
            self._fold_line(line)

    def _fold_line(self, line: bytes) -> None:
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(record, dict) or record.get("type") != "assistant":
            return

        message = record.get("message")
        if not isinstance(message, dict):
            return

        model = message.get("model")
        if isinstance(model, str) and not model.startswith("<"):
            # "<synthetic>" and similar are Claude Code sentinels for injected
            # or interrupted turns, not real models: skip them so they neither
            # overwrite the detected model nor null out cost as "unpriced".
            self.model = model
        elif isinstance(model, str):
            return

        usage = message.get("usage")
        if not isinstance(usage, dict):
            return

        self._fold_usage(usage)

        timestamp = _parse_timestamp(record.get("timestamp"))
        if timestamp is not None:
            self.last_activity = timestamp

        self.context = self._context_from_usage(usage)

    def _fold_usage(self, usage: dict) -> None:
        self._input_tokens += usage.get("input_tokens", 0)
        self._output_tokens += usage.get("output_tokens", 0)
        self._cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
        self._cache_read_tokens += usage.get("cache_read_input_tokens", 0)

        message_cost = pricing.cost_for_usage(self.model, usage)
        if message_cost is None:
            self._cost_usd = None
        elif self._cost_usd is not None:
            self._cost_usd += message_cost

    def _context_from_usage(self, usage: dict) -> ContextWindow:
        """The prompt size of this turn: everything sent to the model."""
        used = (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
        window = pricing.context_window_for(self.model)
        return ContextWindow(used_tokens=used, window_tokens=window)

    def totals(self) -> UsageTotals:
        return UsageTotals(
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            cache_creation_tokens=self._cache_creation_tokens,
            cache_read_tokens=self._cache_read_tokens,
            cost_usd=self._cost_usd,
        )
