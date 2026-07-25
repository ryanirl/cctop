"""Claude Code-compatible advisory locks for credential/config switching."""

from __future__ import annotations

import os
import random
import threading
import time
from contextlib import contextmanager
from pathlib import Path

STALE_SECONDS = 10.0
TOUCH_SECONDS = 3.0
TIMEOUT_SECONDS = 9.0


@contextmanager
def claude_lock(target: Path, timeout: float = TIMEOUT_SECONDS):
    """Hold the same ``<target>.lock`` directory protocol Claude Code uses."""
    lock_dir = target.parent / f"{target.name}.lock"
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    while True:
        try:
            os.mkdir(lock_dir)
            break
        except FileExistsError:
            pass
        if time.monotonic() - started > timeout:
            raise TimeoutError(f"timed out waiting for {lock_dir}")
        try:
            stale = time.time() - lock_dir.stat().st_mtime > STALE_SECONDS
        except FileNotFoundError:
            continue
        if stale:
            try:
                os.rmdir(lock_dir)
            except OSError:
                pass
            continue
        time.sleep(0.25 + random.random() * 0.25)

    stopped = threading.Event()

    def touch() -> None:
        while not stopped.wait(TOUCH_SECONDS):
            try:
                os.utime(lock_dir)
            except OSError:
                return

    thread = threading.Thread(target=touch, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)
        try:
            os.rmdir(lock_dir)
        except FileNotFoundError:
            pass
