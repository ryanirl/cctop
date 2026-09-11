"""Usage limits from Claude Code's statusline feed: no network, no token.

Claude Code hands its statusline command a JSON document after every API
response. Since the 2.1 line that document carries `rate_limits`: the 5-hour
and 7-day windows as `used_percentage` + `resets_at`, taken straight from the
`anthropic-ratelimit-unified-*` headers of the response that just came back.
That is the same data the /usage screen shows, and it is what Claude Code
itself falls back on when GET /api/oauth/usage is unavailable.

Why this exists: the usage endpoint refuses long-lived tokens from
`claude setup-token` (it answers 429 with a one-hour retry-after from the
token's very first use, so it never "recovers"). For those logins the
statusline feed is the only source of real numbers. For normal logins it is
simply fresher and immune to that endpoint's rate limiting.

The hook (`cctop-statusline`, wired into settings.json by `cctop statusline
install`) records each document's rate_limits under cctop's own state dir,
keyed by config dir. It writes nothing into a Claude config dir, and the only
Claude file cctop touches for this is settings.json, on explicit request, with
a one-time backup.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import AccountLimits, LimitWindow

# A record is "fresh" while a session on that account is plausibly still
# talking to the API: within this age it is preferred over the endpoint, and
# it does not go stale abruptly because the windows themselves move slowly.
FRESH = timedelta(minutes=15)
HOOK_NAME = "cctop-statusline"
_BACKUP_SUFFIX = ".cctop.bak"


# -- state file --------------------------------------------------------------


def state_dir() -> Path:
    """cctop's own state dir for limit records, honoring XDG_STATE_HOME."""
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "cctop" / "limits"


def record_path(config_dir: Path) -> Path:
    digest = hashlib.sha256(str(config_dir).encode()).hexdigest()[:12]
    return state_dir() / f"{digest}.json"


def config_dir_from_payload(payload: dict, env: dict | None = None) -> Path:
    """Which account a statusline document belongs to.

    Claude Code runs the statusline command with its own environment, so
    CLAUDE_CONFIG_DIR is set for a non-default account. Failing that, the
    transcript path lives at <config dir>/projects/<slug>/<session>.jsonl.
    """
    env = os.environ if env is None else env
    override = env.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        path = Path(transcript)
        if len(path.parents) >= 3 and path.parents[1].name == "projects":
            return path.parents[2]
    return Path.home() / ".claude"


def _window(entry: object) -> dict | None:
    if not isinstance(entry, dict):
        return None
    pct = entry.get("used_percentage")
    if not isinstance(pct, (int, float)):
        return None
    reset = entry.get("resets_at")
    return {
        "used_percentage": float(pct),
        "resets_at": int(reset) if isinstance(reset, (int, float)) else None,
    }


def record(payload: dict, config_dir: Path, now: datetime | None = None) -> dict | None:
    """Store the document's rate_limits for this config dir; None if it has none."""
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    windows = {}
    for key in ("five_hour", "seven_day"):
        window = _window(limits.get(key))
        if window is not None:
            windows[key] = window
    if not windows:
        return None
    now = now or datetime.now(timezone.utc)
    entry = {
        "config_dir": str(config_dir),
        "recorded_at": now.isoformat(),
        "session_id": payload.get("session_id"),
        "rate_limits": windows,
    }
    path = record_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry))
    os.replace(tmp, path)
    return entry


def read_record(config_dir: Path) -> dict | None:
    try:
        data = json.loads(record_path(config_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("rate_limits"), dict) else None


# -- conversion to the shared models ----------------------------------------

_KINDS = {"five_hour": ("session", "5h"), "seven_day": ("weekly_all", "week (all)")}


def windows_from_record(entry: dict) -> list[LimitWindow]:
    windows = []
    limits = entry.get("rate_limits") or {}
    for key, (kind, label) in _KINDS.items():
        window = _window(limits.get(key))
        if window is None:
            continue
        reset = window["resets_at"]
        windows.append(
            LimitWindow(
                kind=kind,
                label=label,
                percent=max(0.0, min(100.0, window["used_percentage"])),
                resets_at=datetime.fromtimestamp(reset, tz=timezone.utc) if reset else None,
                severity="normal",
                is_active=False,
            )
        )
    if windows:
        # The binding window is the fullest one, as the endpoint's is_active does.
        fullest = max(windows, key=lambda w: w.percent)
        windows = [
            LimitWindow(
                w.kind, w.label, w.percent, w.resets_at, w.severity, w is fullest, w.has_data
            )
            for w in windows
        ]
    return windows


def _recorded_at(entry: dict) -> datetime | None:
    value = entry.get("recorded_at")
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def limits_from_record(
    account_name: str,
    config_dir: Path,
    now: datetime | None = None,
    tier: str | None = None,
    email: str | None = None,
) -> AccountLimits | None:
    """The account's limits as last seen by its own sessions, or None."""
    entry = read_record(config_dir)
    if entry is None:
        return None
    windows = windows_from_record(entry)
    recorded = _recorded_at(entry)
    if not windows or recorded is None:
        return None
    return AccountLimits(
        account=account_name,
        tier=tier,
        windows=windows,
        source="statusline",
        fetched_at=recorded,
        email=email,
    )


def is_fresh(limits: AccountLimits | None, now: datetime) -> bool:
    return limits is not None and limits.fetched_at is not None and now - limits.fetched_at <= FRESH


# -- the hook itself -----------------------------------------------------------


def format_line(payload: dict) -> str:
    """A compact default status line: model, directory, and the two windows."""
    parts = []
    model = payload.get("model") or {}
    name = model.get("display_name") if isinstance(model, dict) else None
    if isinstance(name, str) and name:
        parts.append(name)
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        parts.append(Path(cwd).name or cwd)
    limits = payload.get("rate_limits") or {}
    for key, label in (("five_hour", "5h"), ("seven_day", "wk")):
        window = _window(limits.get(key)) if isinstance(limits, dict) else None
        if window is not None:
            parts.append(f"{label} {window['used_percentage']:.0f}%")
    return " │ ".join(parts)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `cctop-statusline` hook: record, then print a line.

    With `--exec CMD`, the same document is piped to CMD and its output is
    printed instead, so an existing statusline keeps working unchanged.
    """
    argv = sys.argv[1:] if argv is None else argv
    exec_cmd = None
    if argv[:1] == ["--exec"] and len(argv) >= 2:
        exec_cmd = argv[1]
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        try:
            record(payload, config_dir_from_payload(payload))
        except OSError:
            pass  # a status line must never fail because the state dir is unwritable
    else:
        payload = {}
    if exec_cmd:
        try:
            result = subprocess.run(
                exec_cmd, shell=True, input=raw, capture_output=True, text=True, timeout=5
            )
            sys.stdout.write(result.stdout)
            return result.returncode
        except (OSError, subprocess.SubprocessError):
            return 1
    print(format_line(payload))
    return 0


# -- settings.json wiring (explicit, backed up, reversible) ------------------


def hook_command(exec_cmd: str | None = None) -> str:
    """The absolute hook command, so it works whatever PATH Claude Code has."""
    here = Path(sys.argv[0]).resolve().parent / HOOK_NAME
    binary = str(here) if here.exists() else (shutil.which(HOOK_NAME) or HOOK_NAME)
    command = shlex.quote(binary)
    if exec_cmd:
        command += f" --exec {shlex.quote(exec_cmd)}"
    return command


def _is_ours(command: object) -> bool:
    return isinstance(command, str) and HOOK_NAME in command


def _wrapped_command(command: str) -> str | None:
    """The original command a hook line wraps via --exec, if any."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if "--exec" in parts:
        idx = parts.index("--exec")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def _settings_path(config_dir: Path) -> Path:
    return config_dir / "settings.json"


def _load_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_settings(path: Path, data: dict) -> None:
    if path.exists():
        backup = path.with_name(path.name + _BACKUP_SUFFIX)
        if not backup.exists():
            shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def install(config_dir: Path) -> str:
    """Point this account's statusLine at the hook, wrapping any existing one."""
    path = _settings_path(config_dir)
    data = _load_settings(path)
    current = data.get("statusLine")
    existing = current.get("command") if isinstance(current, dict) else None
    if _is_ours(existing):
        return f"{path}: hook already installed"
    data["statusLine"] = {"type": "command", "command": hook_command(existing)}
    _save_settings(path, data)
    note = f" (wrapping your existing statusline: {existing})" if existing else ""
    return f"{path}: statusLine set to the cctop hook{note}"


def uninstall(config_dir: Path) -> str:
    """Remove the hook; restore a wrapped statusline if there was one."""
    path = _settings_path(config_dir)
    data = _load_settings(path)
    current = data.get("statusLine")
    command = current.get("command") if isinstance(current, dict) else None
    if not _is_ours(command):
        return f"{path}: hook not installed"
    wrapped = _wrapped_command(command)
    if wrapped:
        data["statusLine"] = {"type": "command", "command": wrapped}
    else:
        del data["statusLine"]
    _save_settings(path, data)
    return f"{path}: hook removed" + (f", restored: {wrapped}" if wrapped else "")


def status(config_dir: Path, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    path = _settings_path(config_dir)
    current = _load_settings(path).get("statusLine")
    command = current.get("command") if isinstance(current, dict) else None
    installed = "installed" if _is_ours(command) else "not installed"
    entry = read_record(config_dir)
    if entry is None:
        seen = "no record yet"
    else:
        recorded = _recorded_at(entry)
        age = f"{int((now - recorded).total_seconds() // 60)}m ago" if recorded else "?"
        seen = f"last record {age}: " + ", ".join(
            f"{k} {v['used_percentage']:.0f}%" for k, v in entry["rate_limits"].items()
        )
    return f"{config_dir}: hook {installed}; {seen}"
