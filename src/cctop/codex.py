"""Read-only discovery of live Codex (OpenAI) sessions.

Codex stores each session as a rollout JSONL file under
~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl. There is no per-pid
registry like Claude's, so live sessions are found from `ps` (the codex
processes) and matched to their rollout by the `resume <id>` argument, or by
working directory. The rollout carries the cwd, model, cumulative token usage,
and context window.

This module only ever reads: process listing, `lsof` for a pid's cwd, and the
rollout/index files. It is the first slice of Codex support; usage limits and
aggregated stats come later (see the roadmap).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

CODEX_DIR = Path.home() / ".codex"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


@dataclass(frozen=True)
class CodexSession:
    pid: int
    session_id: str
    name: str
    cwd: str
    model: str | None
    total_tokens: int
    context_used: int
    context_window: int
    last_activity: datetime | None


def _run(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _codex_processes() -> list[tuple[int, str]]:
    """Live codex CLI processes as (pid, command), excluding helper subprocesses."""
    processes = []
    for line in _run(["ps", "-Ao", "pid=,command="]).splitlines():
        match = re.match(r"\s*(\d+)\s+(.*)", line)
        if not match:
            continue
        pid, command = int(match.group(1)), match.group(2)
        if "codex-code-mode-host" in command or "node_repl" in command:
            continue
        if "Updater" in command or "com.openai.codex" in command:
            continue
        # Skip the desktop app / app-server; we want interactive coding sessions.
        if "app-server" in command or "/Codex.app/" in command:
            continue
        if re.search(r"(^|/)codex(\s|$)", command) or "/bin/codex" in command:
            processes.append((pid, command))
    return processes


def _lsof_cwd(pid: int) -> str | None:
    for line in _run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]).splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def _rollout_for_id(session_id: str) -> Path | None:
    matches = list(CODEX_DIR.glob(f"sessions/**/rollout-*-{session_id}.jsonl"))
    return matches[0] if matches else None


def _newest_rollout_for_cwd(cwd: str) -> Path | None:
    """The most recently modified rollout whose session_meta cwd matches."""
    best: tuple[float, Path] | None = None
    for path in CODEX_DIR.glob("sessions/**/rollout-*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if best is not None and mtime <= best[0]:
            continue
        meta = _read_session_meta(path)
        if meta and meta.get("cwd") == cwd:
            best = (mtime, path)
    return best[1] if best else None


def _read_session_meta(path: Path) -> dict | None:
    try:
        with path.open() as handle:
            first = handle.readline()
    except OSError:
        return None
    try:
        record = json.loads(first)
    except json.JSONDecodeError:
        return None
    if record.get("type") == "session_meta":
        payload = record.get("payload")
        return payload if isinstance(payload, dict) else None
    return None


def _index_names() -> dict[str, str]:
    names: dict[str, str] = {}
    try:
        text = (CODEX_DIR / "session_index.jsonl").read_text()
    except OSError:
        return names
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("id") and record.get("thread_name"):
            names[record["id"]] = record["thread_name"]
    return names


def _parse_rollout(
    path: Path,
) -> tuple[dict | None, int, int, int, str | None, datetime | None]:
    """Return (session_meta, total_tokens, context_used, context_window, model, last)."""
    meta = None
    total_tokens = 0
    context_used = 0
    context_window = 0
    model = None
    last_activity = None
    try:
        handle = path.open()
    except OSError:
        return None, 0, 0, 0, None, None
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = record.get("type")
            payload = record.get("payload") or {}
            if kind == "session_meta":
                meta = payload
            elif kind == "turn_context" and isinstance(payload.get("model"), str):
                model = payload["model"]
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                cumulative = info.get("total_token_usage") or {}
                total_tokens = cumulative.get("total_tokens", total_tokens)
                last = info.get("last_token_usage") or {}
                context_used = last.get("input_tokens", 0) + last.get("cached_input_tokens", 0)
                context_window = info.get("model_context_window") or context_window
            timestamp = record.get("timestamp")
            if isinstance(timestamp, str):
                try:
                    last_activity = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError:
                    pass
    return meta, total_tokens, context_used, context_window, model, last_activity


def discover_sessions() -> list[CodexSession]:
    """Live Codex sessions, matched to their rollout for cwd/model/tokens."""
    names = _index_names()
    sessions: dict[str, CodexSession] = {}

    for pid, command in _codex_processes():
        id_match = re.search(rf"resume\s+({_UUID})", command)
        session_id = id_match.group(1) if id_match else None

        rollout = _rollout_for_id(session_id) if session_id else None
        if rollout is None:
            cwd = _lsof_cwd(pid)
            rollout = _newest_rollout_for_cwd(cwd) if cwd else None

        meta = _read_session_meta(rollout) if rollout else None
        if rollout is not None:
            meta, total_tokens, context_used, window, model, last = _parse_rollout(rollout)
        else:
            total_tokens = context_used = window = 0
            model = last = None

        resolved_id = (meta or {}).get("id") or session_id or f"pid-{pid}"
        if resolved_id in sessions:
            continue
        cwd = (meta or {}).get("cwd") or _lsof_cwd(pid) or ""
        name = names.get(resolved_id) or (Path(cwd).name if cwd else "") or resolved_id[:8]
        sessions[resolved_id] = CodexSession(
            pid=pid,
            session_id=resolved_id,
            name=name,
            cwd=cwd,
            model=model or (meta or {}).get("model_provider"),
            total_tokens=total_tokens,
            context_used=context_used,
            context_window=window,
            last_activity=last,
        )

    return list(sessions.values())
