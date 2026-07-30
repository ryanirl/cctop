"""Shared last-good usage readings for the TUI and autoswitch supervisor."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from .models import AccountLimits, LimitWindow


def load(path: Path) -> dict[str, AccountLimits]:
    """Read cached API results, ignoring an absent or invalid cache."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return {}

    results: dict[str, AccountLimits] = {}
    for entry in payload.get("accounts", []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("account")
        fetched_at = _datetime(entry.get("fetched_at"))
        if not isinstance(name, str) or fetched_at is None:
            continue
        windows = []
        for item in entry.get("windows", []):
            if not isinstance(item, dict):
                continue
            try:
                windows.append(
                    LimitWindow(
                        kind=str(item["kind"]),
                        label=str(item["label"]),
                        percent=float(item["percent"]),
                        resets_at=_datetime(item.get("resets_at")),
                        severity=str(item["severity"]),
                        is_active=bool(item["is_active"]),
                        has_data=bool(item["has_data"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        results[name] = AccountLimits(
            name,
            entry.get("tier") if isinstance(entry.get("tier"), str) else None,
            windows,
            "api",
            fetched_at,
        )
    return results


def save(path: Path, limits: dict[str, AccountLimits]) -> None:
    """Atomically persist only successful, non-secret usage metadata."""
    accounts = []
    for item in limits.values():
        if item.source != "api" or item.fetched_at is None:
            continue
        accounts.append(
            {
                "account": item.account,
                "tier": item.tier,
                "fetched_at": item.fetched_at.isoformat(),
                "windows": [
                    {
                        "kind": window.kind,
                        "label": window.label,
                        "percent": window.percent,
                        "resets_at": (
                            window.resets_at.isoformat() if window.resets_at is not None else None
                        ),
                        "severity": window.severity,
                        "is_active": window.is_active,
                        "has_data": window.has_data,
                    }
                    for window in item.windows
                ],
            }
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.cctop-new")
    temporary.write_text(json.dumps({"version": 1, "accounts": accounts}, separators=(",", ":")))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
