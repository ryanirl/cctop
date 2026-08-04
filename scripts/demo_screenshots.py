"""Generate the README/demo screenshots from fabricated data.

Builds a fictional fleet under a temp home (user "dev", invented projects and
conversations), patches the network/process probes to serve fabricated usage
limits and liveness, then drives the real TUI through Textual's test harness
and exports SVG screenshots of the dashboard and the search screen. Nothing
personal can leak because nothing real is read: every path, title, message,
and number below is made up.

Usage:
    .venv/bin/python scripts/demo_screenshots.py [output_dir]

Writes demo-dashboard.svg and demo-search.svg (rasterize with e.g.
`rsvg-convert -w 1800 demo-dashboard.svg -o demo-dashboard.png`).
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cctop import codex as codex_mod
from cctop import collect as collect_mod
from cctop import monitor as monitor_mod
from cctop import registry as registry_mod
from cctop.codex import CodexSession
from cctop.collect import Account
from cctop.models import AccountLimits, LimitWindow

NOW = datetime.now(timezone.utc)
FAKE_HOME = "/Users/dev"

# One fictional engineer's week. Sessions are (account, project dir, session
# name, registry status, model, minutes since last activity, context fraction).
SESSIONS = [
    ("cc-0", "work/api-server", "api-server", "busy", "claude-fable-5", 0.4, 0.34),
    ("cc-0", "work/webapp", "webapp", "idle", "claude-fable-5", 3, 0.12),
    ("cc-1", "oss/ml-pipeline", "ml-pipeline", "busy", "claude-opus-4-8", 1, 0.58),
    ("cc-1", "work/infra", "infra", "shell", "claude-fable-5", 6, 0.22),
    ("cc-0", "oss/cli-tools", "cli-tools", "idle", "claude-haiku-4-5", 8, 0.07),
]

# Finished sessions that only exist as history: (account, project, name,
# model, days ago, turns of filler). Several mention the demo search query.
HISTORY = [
    ("cc-1", "work/file-watcher", "file-watcher", "claude-fable-5", 2, 84),
    ("cc-0", "work/payments", "payments", "claude-opus-4-8", 3, 127),
    ("cc-1", "work/api-server", "conn-pool", "claude-fable-5", 5, 63),
    ("cc-0", "oss/vector-db", "vector-db", "claude-opus-4-8", 8, 202),
    ("cc-1", "work/infra", "docker-cache", "claude-haiku-4-5", 12, 31),
    ("cc-0", "work/api-server", "webhooks", "claude-fable-5", 16, 148),
    ("cc-1", "work/webapp", "notifs", "claude-sonnet-5", 21, 57),
]

TITLES = {
    "api-server": "Debug race condition in the job queue",
    "webapp": "Fix flaky websocket reconnect test",
    "ml-pipeline": "Profile slow feature-extraction batch",
    "infra": "Migrate deploys to blue-green rollout",
    "cli-tools": "Add retry with exponential backoff",
    "file-watcher": "Fix TOCTOU race condition in the file watcher",
    "payments": "Chase flaky integration test in payments",
    "conn-pool": "Trace deadlock in the connection pool",
    "vector-db": "Investigate OOM during index rebuild",
    "docker-cache": "Speed up Docker builds with cache mounts",
    "webhooks": "Harden webhook delivery retries",
    "notifs": "Dedupe duplicate push notifications",
}

# Searchable conversation beats; several mention the demo query on purpose.
EXCHANGES = {
    "api-server": [
        ("user", "Two workers keep grabbing the same job. Can you find the race condition?"),
        (
            "assistant",
            "The claim query reads then updates in two statements, so both workers can pass "
            "the read before either writes. Using UPDATE ... RETURNING with a status guard "
            "makes the claim atomic and the race condition disappears.",
        ),
        ("user", "Nice. Add a regression test that hammers it with 50 concurrent claims."),
    ],
    "webapp": [
        ("user", "The websocket reconnect test fails maybe once in ten runs on CI."),
        (
            "assistant",
            "The test asserts on reconnect order, but the backoff timer makes ordering "
            "nondeterministic: a classic race condition between the two mock sockets. "
            "Pinning the timer with a fake clock makes it deterministic.",
        ),
    ],
    "ml-pipeline": [
        ("user", "Feature extraction takes 40 minutes on the new dataset. Profile it?"),
        (
            "assistant",
            "80 percent of the time is pandas apply over rows. Vectorizing the two hot "
            "transforms and batching the tokenizer calls brings the run to about 6 minutes.",
        ),
    ],
    "infra": [
        ("user", "Sketch the blue-green cutover for the API deploys."),
        (
            "assistant",
            "Two target groups behind the ALB; deploy to the idle color, run smoke checks, "
            "then shift the listener. Rollback is shifting the listener back, no redeploy.",
        ),
    ],
    "cli-tools": [
        ("user", "S3 uploads flake in CI. Add retry with exponential backoff and jitter?"),
        (
            "assistant",
            "Wrapped the client with three attempts, full jitter, and a cap at 20 seconds; "
            "transient 5xx and timeout errors retry, 4xx fail fast.",
        ),
    ],
}

HISTORY_EXCHANGES = {
    "file-watcher": [
        ("user", "Saves sometimes trigger the on-change hook twice. TOCTOU?"),
        (
            "assistant",
            "Yes: stat then read is a race condition when the editor writes between them. "
            "Switching to reading first and comparing content hashes fixes the double fire.",
        ),
    ],
    "payments": [
        ("user", "The payments integration test fails only when the suite runs in parallel."),
        (
            "assistant",
            "Two tests share the same sandbox account, so the balance assertions race. It is "
            "a test-isolation race condition, not a payments bug: per-test accounts fix it.",
        ),
    ],
    "conn-pool": [
        ("user", "API requests hang under load; the pool never gives connections back."),
        (
            "assistant",
            "A failed checkout leaves the semaphore held: a race condition between the "
            "timeout path and the release. Moving the release into a finally block fixes it.",
        ),
    ],
    "vector-db": [
        ("user", "Index rebuild OOMs at about 40M vectors."),
        (
            "assistant",
            "The rebuild holds both the old and new index in memory. Building in shards and "
            "swapping segment by segment keeps the peak under 6 GB.",
        ),
    ],
    "docker-cache": [
        ("user", "CI Docker builds take 11 minutes even for a one-line change."),
        (
            "assistant",
            "Parallel jobs hit a race condition on the shared cache mount; per-branch cache "
            "keys plus BuildKit cache mounts bring warm builds to about 90 seconds.",
        ),
    ],
    "webhooks": [
        ("user", "Customers report missing webhook deliveries during deploys."),
        (
            "assistant",
            "Deliveries in flight during shutdown are dropped. Persisting attempts in an "
            "outbox table with at-least-once retries survives the restart window.",
        ),
    ],
    "notifs": [
        ("user", "Some users get the same push notification twice."),
        (
            "assistant",
            "Two schedulers claim the same batch: a classic race condition. A unique "
            "constraint on (user, event) plus idempotent sends dedupes it.",
        ),
    ],
}

CODEX_EXCHANGE = [
    ("user", "Why does the eval harness deadlock when two runners share a cache dir?"),
    (
        "assistant",
        "Both runners take the file lock and then wait on each other's queue: a lock "
        "ordering race condition. Taking the queue slot before the lock removes the cycle.",
    ),
]


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", text)


def _stamp(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _epoch_ms(minutes_ago: float) -> int:
    return int((NOW - timedelta(minutes=minutes_ago)).timestamp() * 1000)


def _session_id(index: int) -> str:
    return f"{index:08x}-4b2d-4c6e-9f1a-{index:012x}"


def _write_transcript(
    path: Path,
    session_id: str,
    cwd: str,
    name: str,
    model: str,
    minutes_ago: float,
    context_fraction: float,
    beats: list[tuple[str, str]],
    filler_turns: int = 0,
) -> None:
    window = 1_000_000 if "haiku" not in model else 200_000
    context_tokens = int(window * context_fraction)
    lines = [json.dumps({"type": "ai-title", "aiTitle": TITLES[name], "sessionId": session_id})]

    # Filler assistant beats bulk the turns column up to realistic session
    # depths without touching the searchable dialogue.
    for turn in range(filler_turns):
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": "Applied; rerunning checks."},
                    "sessionId": session_id,
                    "timestamp": _stamp(minutes_ago + 30 + turn),
                    "cwd": cwd,
                    "isSidechain": False,
                }
            )
        )
    for turn, (role, text) in enumerate(beats):
        minutes = minutes_ago + (len(beats) - turn) * 4
        record: dict = {
            "type": role,
            "message": {"role": role, "content": text},
            "sessionId": session_id,
            "timestamp": _stamp(minutes),
            "cwd": cwd,
            "isSidechain": False,
            "gitBranch": "main",
            "uuid": f"{session_id[:8]}-{turn}",
        }
        if role == "assistant":
            record["message"]["model"] = model
            record["message"]["usage"] = {
                "input_tokens": random.randint(400, 1200),
                "output_tokens": random.randint(900, 2600),
                "cache_creation_input_tokens": random.randint(8_000, 30_000),
                "cache_read_input_tokens": random.randint(350_000, 900_000),
            }
        lines.append(json.dumps(record))

    # A final assistant beat pins the context gauge to the chosen fraction.
    closing = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": "Done. Tests pass; want me to open the PR?",
            "model": model,
            "usage": {
                "input_tokens": 800,
                "output_tokens": 350,
                "cache_creation_input_tokens": 12_000,
                "cache_read_input_tokens": max(0, context_tokens - 12_800),
            },
        },
        "sessionId": session_id,
        "timestamp": _stamp(minutes_ago),
        "cwd": cwd,
        "isSidechain": False,
    }
    lines.append(json.dumps(closing))
    path.write_text("\n".join(lines) + "\n")

    # The search screen's "last" column is the file mtime; backdate it to the
    # session's age so history rows do not all read as seconds old.
    import os

    moment = (NOW - timedelta(minutes=minutes_ago)).timestamp()
    os.utime(path, (moment, moment))


def _write_stats_cache(config_dir: Path, seed: int, scale: float) -> None:
    """A believable months-long activity history (weekday-heavy, seeded)."""
    rng = random.Random(seed)
    daily = []
    model_days = []
    for day_offset in range(200, -1, -1):
        day = (NOW - timedelta(days=day_offset)).date()
        weekday = day.weekday() < 5
        if rng.random() < (0.85 if weekday else 0.35):
            messages = int(rng.randint(8, 120) * scale) + 1
            daily.append(
                {
                    "date": day.isoformat(),
                    "messageCount": messages,
                    "sessionCount": max(1, messages // 30),
                    "toolCallCount": messages * 3,
                }
            )
            model_days.append(
                {
                    "date": day.isoformat(),
                    "tokensByModel": {"claude-fable-5": messages * rng.randint(9000, 22000)},
                }
            )

    total_messages = sum(entry["messageCount"] for entry in daily)
    cache = {
        "totalSessions": sum(entry["sessionCount"] for entry in daily),
        "totalMessages": total_messages,
        "firstSessionDate": daily[0]["date"] if daily else NOW.date().isoformat(),
        "dailyActivity": daily,
        "dailyModelTokens": model_days,
        "modelUsage": {
            "claude-fable-5": {
                "inputTokens": int(210_000_000 * scale),
                "outputTokens": int(48_000_000 * scale),
                "cacheReadInputTokens": int(1_400_000_000 * scale),
                "cacheCreationInputTokens": int(160_000_000 * scale),
            },
            "claude-opus-4-8": {
                "inputTokens": int(60_000_000 * scale),
                "outputTokens": int(9_000_000 * scale),
                "cacheReadInputTokens": int(310_000_000 * scale),
                "cacheCreationInputTokens": int(40_000_000 * scale),
            },
            "claude-haiku-4-5-20251001": {
                "inputTokens": int(15_000_000 * scale),
                "outputTokens": int(2_000_000 * scale),
                "cacheReadInputTokens": int(41_000_000 * scale),
                "cacheCreationInputTokens": int(6_000_000 * scale),
            },
        },
    }
    (config_dir / "stats-cache.json").write_text(json.dumps(cache))


def _write_codex(config_dir: Path) -> str:
    """Rollout history for the stats heatmap plus one searchable session."""
    rng = random.Random(7)
    live_id = "c0de0000-4b2d-4c6e-9f1a-0000000c0de0"
    for day_offset in range(160, 0, -1):
        day = NOW - timedelta(days=day_offset)
        if day.weekday() >= 5 and rng.random() < 0.7:
            continue
        if rng.random() < 0.45:
            continue
        folder = config_dir / "sessions" / day.strftime("%Y/%m/%d")
        folder.mkdir(parents=True, exist_ok=True)
        session = f"{day_offset:08x}-4b2d-4c6e-9f1a-{day_offset:012x}"
        stamp = day.strftime("%Y-%m-%dT10:0%S.000Z")
        lines = [
            json.dumps(
                {
                    "type": "session_meta",
                    "timestamp": stamp,
                    "payload": {"id": session, "cwd": f"{FAKE_HOME}/work/eval-harness"},
                }
            )
        ]
        for _ in range(rng.randint(2, 14)):
            lines.append(
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": stamp,
                        "payload": {"type": "user_message", "message": "run the sweep"},
                    }
                )
            )
        lines.append(
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": stamp,
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"total_tokens": rng.randint(2, 9) * 100_000}
                        },
                    },
                }
            )
        )
        (folder / f"rollout-{day.strftime('%Y-%m-%dT10-00-00')}-{session}.jsonl").write_text(
            "\n".join(lines) + "\n"
        )

    folder = config_dir / "sessions" / NOW.strftime("%Y/%m/%d")
    folder.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "type": "session_meta",
                "timestamp": _stamp(90),
                "payload": {"id": live_id, "cwd": f"{FAKE_HOME}/work/eval-harness"},
            }
        ),
        json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.3-codex"}}),
    ]
    for _ in range(46):
        lines.append(
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": _stamp(80),
                    "payload": {"type": "agent_message", "message": "Sweep step finished."},
                }
            )
        )
    for turn, (role, text) in enumerate(CODEX_EXCHANGE):
        kind = "user_message" if role == "user" else "agent_message"
        lines.append(
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": _stamp(8 - turn * 3),
                    "payload": {"type": kind, "message": text},
                }
            )
        )
    import os

    live_rollout = folder / f"rollout-{NOW.strftime('%Y-%m-%dT09-00-00')}-{live_id}.jsonl"
    live_rollout.write_text("\n".join(lines) + "\n")
    moment = (NOW - timedelta(minutes=2)).timestamp()
    os.utime(live_rollout, (moment, moment))
    return live_id


def build_demo_home(root: Path) -> tuple[list[Account], list[int], str]:
    """The whole fictional fleet on disk; returns accounts, pids, codex id."""
    random.seed(11)
    accounts = [
        Account("cc-0", root / ".claude"),
        Account("cc-1", root / ".claude-1"),
        Account("cx-0", root / ".codex", provider="codex"),
    ]

    pids = []
    for index, (name, project, session_name, status, model, age, context) in enumerate(SESSIONS):
        config_dir = root / (".claude" if name == "cc-0" else ".claude-1")
        cwd = f"{FAKE_HOME}/{project}"
        session_id = _session_id(index + 1)
        pid = 84000 + index
        pids.append(pid)

        registry = config_dir / "sessions"
        registry.mkdir(parents=True, exist_ok=True)
        (registry / f"{pid}.json").write_text(
            json.dumps(
                {
                    "pid": pid,
                    "sessionId": session_id,
                    "cwd": cwd,
                    "name": session_name,
                    "status": status,
                    "kind": "repl",
                    "version": "2.1.197",
                    "startedAt": _epoch_ms(age + 95),
                    "updatedAt": _epoch_ms(age),
                    "statusUpdatedAt": _epoch_ms(age),
                }
            )
        )

        project_dir = config_dir / "projects" / _slug(cwd)
        project_dir.mkdir(parents=True, exist_ok=True)
        _write_transcript(
            project_dir / f"{session_id}.jsonl",
            session_id,
            cwd,
            session_name,
            model,
            age,
            context,
            beats=EXCHANGES[session_name],
            filler_turns=random.randint(60, 320),
        )

    for index, (name, project, session_name, model, days, filler) in enumerate(HISTORY):
        config_dir = root / (".claude" if name == "cc-0" else ".claude-1")
        cwd = f"{FAKE_HOME}/{project}"
        session_id = _session_id(100 + index)
        project_dir = config_dir / "projects" / _slug(cwd)
        project_dir.mkdir(parents=True, exist_ok=True)
        _write_transcript(
            project_dir / f"{session_id}.jsonl",
            session_id,
            cwd,
            session_name,
            model,
            days * 24 * 60.0,
            0.2,
            beats=HISTORY_EXCHANGES[session_name],
            filler_turns=filler,
        )

    _write_stats_cache(root / ".claude", seed=3, scale=1.0)
    _write_stats_cache(root / ".claude-1", seed=5, scale=0.6)
    codex_live_id = _write_codex(root / ".codex")
    return accounts, pids, codex_live_id


def _window(kind: str, label: str, percent: float, hours: float, active: bool) -> LimitWindow:
    return LimitWindow(
        kind=kind,
        label=label,
        percent=percent,
        resets_at=NOW + timedelta(hours=hours),
        severity="normal",
        is_active=active,
    )


DEMO_LIMITS = {
    "cc-0": AccountLimits(
        "cc-0",
        "default_claude_max_20x",
        [
            _window("session", "5h", 63, 2.2, True),
            _window("weekly_all", "week (all)", 38, 76, False),
            _window("weekly_scoped", "week (Fable)", 21, 76, False),
        ],
        "api",
        NOW - timedelta(seconds=42),
    ),
    "cc-1": AccountLimits(
        "cc-1",
        "default_claude_max_5x",
        [
            _window("session", "5h", 17, 3.8, False),
            _window("weekly_all", "week (all)", 52, 31, True),
            _window("weekly_scoped", "week (Fable)", 44, 31, False),
        ],
        "api",
        NOW - timedelta(seconds=42),
    ),
    "cx-0": AccountLimits(
        "cx-0",
        "pro",
        [
            _window("primary_window", "5h", 27, 1.4, False),
            _window("secondary_window", "week", 41, 52, True),
        ],
        "api",
        NOW - timedelta(seconds=42),
    ),
}


def _patch_probes(pids: list[int], codex_live_id: str, codex_dir: Path) -> None:
    alive = set(pids)

    def fake_alive(pid: int) -> bool:
        return pid in alive

    registry_mod.process_alive = fake_alive
    collect_mod.process_alive = fake_alive
    monitor_mod.process_alive = fake_alive
    monitor_mod.account_limits = lambda account: DEMO_LIMITS[account.name]

    def fake_codex_sessions() -> list[CodexSession]:
        return [
            CodexSession(
                pid=84990,
                session_id=codex_live_id,
                name="eval-harness",
                cwd=f"{FAKE_HOME}/work/eval-harness",
                model="gpt-5.3-codex",
                total_tokens=1_840_000,
                context_used=61_000,
                context_window=258_400,
                last_activity=NOW - timedelta(minutes=2),
            )
        ]

    codex_mod.discover_sessions = fake_codex_sessions


async def shoot_dashboard(accounts: list[Account], out: Path) -> None:
    from cctop.app import CctopApp

    app = CctopApp(accounts, auto_refresh_tokens=False)
    async with app.run_test(size=(150, 40)) as pilot:
        await asyncio.sleep(2.5)  # sessions tick, limits fetch, stats worker
        await pilot.pause()
        app.save_screenshot(str(out))
        await pilot.press("q")


async def shoot_search(accounts: list[Account], out: Path) -> None:
    from cctop.search_screen import SearchApp

    app = SearchApp(accounts, initial_query="race condition")
    async with app.run_test(size=(150, 40)) as pilot:
        await asyncio.sleep(2.0)
        await pilot.press("down")  # search bar -> path bar
        await pilot.press("down")  # path bar -> first result
        await pilot.press("shift+tab")  # park the text cursor back in the query
        await pilot.press("end")  # clear the select-on-focus highlight
        await asyncio.sleep(0.5)
        await pilot.pause()
        app.save_screenshot(str(out))
        await pilot.press("escape")


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs")
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="cctop-demo-") as tmp:
        root = Path(tmp)
        accounts, pids, codex_live_id = build_demo_home(root)
        _patch_probes(pids, codex_live_id, root / ".codex")

        asyncio.run(shoot_dashboard(accounts, out_dir / "demo-dashboard.svg"))
        asyncio.run(shoot_search(accounts, out_dir / "demo-search.svg"))

    print(f"wrote {out_dir}/demo-dashboard.svg and {out_dir}/demo-search.svg")


if __name__ == "__main__":
    main()
