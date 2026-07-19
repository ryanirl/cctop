"""The setup selector only lets you pick a detected provider."""

from __future__ import annotations

import asyncio

from cctop.setup_screen import Provider, SetupApp


def _run(coro):
    return asyncio.run(coro)


def test_cursor_starts_on_first_available() -> None:
    async def inner():
        app = SetupApp([Provider("claude", "Claude", False), Provider("codex", "Codex", True)])
        async with app.run_test():
            return app._cursor  # only Codex (index 1) is selectable

    assert _run(inner()) == 1


def test_enter_selects_the_highlighted_available_provider() -> None:
    async def inner():
        app = SetupApp([Provider("claude", "Claude", True), Provider("codex", "Codex", True)])
        async with app.run_test() as pilot:
            await pilot.press("down")  # move to Codex
            await pilot.press("enter")
        return app.selected

    assert _run(inner()) == "codex"


def test_none_available_selects_nothing() -> None:
    async def inner():
        app = SetupApp([Provider("claude", "Claude", False), Provider("codex", "Codex", False)])
        async with app.run_test() as pilot:
            await pilot.press("enter")  # no-op: nothing is selectable
            await pilot.press("q")
        return app.selected

    assert _run(inner()) is None
