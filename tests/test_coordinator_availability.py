"""Characterization tests for the availability watchdog (``GaggiMateCoordinator._check_availability``).

The loop is driven deterministically: ``asyncio`` inside the coordinator module is replaced by a proxy
whose ``sleep`` blocks on a queue, so each test releases exactly one watchdog cycle at a time and no
test depends on wall-clock timing. A "stale" status is simulated by backdating ``_last_status_time``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.gaggimate.coordinator import GaggiMateCoordinator

from .conftest import wait_until

MODULE = "custom_components.gaggimate.coordinator"
STALE_AGE = 10_000.0  # seconds; far above any timeout, independent of the patched value


class _Driver:
    """Runs ``_check_availability`` one cycle at a time."""

    def __init__(self, coordinator: GaggiMateCoordinator) -> None:
        self.coordinator = coordinator
        self.queue: asyncio.Queue[None] = asyncio.Queue()
        self.sleeping = 0  # number of times the loop has (re-)entered its sleep
        self.task: asyncio.Task | None = None
        driver = self

        class _AsyncioProxy:
            def __getattr__(self, name: str) -> Any:
                return getattr(asyncio, name)

            async def sleep(self, delay: float, *a: Any, **kw: Any) -> None:
                assert delay == 1
                driver.sleeping += 1
                await driver.queue.get()

        self._patch = patch(f"{MODULE}.asyncio", _AsyncioProxy())

    async def start(self) -> None:
        self._patch.start()
        self.task = asyncio.get_running_loop().create_task(self.coordinator._check_availability())
        await wait_until(lambda: self.sleeping == 1)

    async def tick(self) -> None:
        """Release one cycle and wait until the loop is back at its sleep."""
        before = self.sleeping
        self.queue.put_nowait(None)
        await wait_until(lambda: self.sleeping == before + 1)

    async def stop(self) -> None:
        assert self.task is not None
        self.coordinator._shutting_down = True
        self.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await self.task
        self._patch.stop()


def _make(hass: HomeAssistant) -> GaggiMateCoordinator:
    coordinator = GaggiMateCoordinator(hass, "127.0.0.1", 1)
    coordinator._available = True
    coordinator._last_status_time = time.monotonic() - STALE_AGE
    return coordinator


def _make_ws(events: list[str], close_error: Exception | None = None, closed: bool = False) -> MagicMock:
    ws = MagicMock()
    ws.closed = closed

    async def _close() -> None:
        events.append("ws.close")
        if close_error is not None:
            raise close_error

    ws.close = MagicMock(side_effect=_close)
    return ws


def _spy(coordinator: GaggiMateCoordinator, events: list[str]) -> None:
    """Record the order of the calls the stale path makes (and the state at each call)."""
    real_clear = coordinator._clear_brew_confirm

    def clear(notify: bool = True) -> None:
        events.append(f"clear(notify={notify},last_status={coordinator._last_status_time},"
                      f"pending={coordinator._brew_confirm is not None})")
        real_clear(notify=notify)

    def set_data(data: Any) -> None:
        events.append(f"set_updated_data({data},available={coordinator._available})")

    def update_listeners() -> None:
        events.append(f"update_listeners(available={coordinator._available})")

    coordinator._clear_brew_confirm = clear  # type: ignore[method-assign]
    coordinator.async_set_updated_data = set_data  # type: ignore[method-assign]
    coordinator.async_update_listeners = update_listeners  # type: ignore[method-assign]


@pytest.fixture
def events() -> list[str]:
    return []


async def test_last_status_time_none_triggers_no_action(hass: HomeAssistant, events: list[str]) -> None:
    coordinator = _make(hass)
    coordinator._last_status_time = None
    coordinator._brew_confirm = [{"k": "x", "l": 2}]
    coordinator._ws = _make_ws(events)
    _spy(coordinator, events)
    driver = _Driver(coordinator)
    await driver.start()
    try:
        await driver.tick()
        await driver.tick()
    finally:
        await driver.stop()
    assert events == []
    assert coordinator._available is True
    assert coordinator._brew_confirm == [{"k": "x", "l": 2}]
    assert coordinator._last_status_time is None


async def test_fresh_status_triggers_no_action(hass: HomeAssistant, events: list[str]) -> None:
    coordinator = _make(hass)
    coordinator._last_status_time = time.monotonic()
    coordinator._ws = _make_ws(events)
    _spy(coordinator, events)
    driver = _Driver(coordinator)
    with patch(f"{MODULE}.WS_UNAVAILABLE_TIMEOUT", STALE_AGE * 10):
        await driver.start()
        try:
            await driver.tick()
        finally:
            await driver.stop()
    assert events == []
    assert coordinator._available is True
    assert coordinator._last_status_time is not None


async def test_stale_path_ordering_and_second_cycle_does_not_renotify(
    hass: HomeAssistant, events: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _make(hass)
    coordinator._brew_confirm = [{"k": "x", "l": 2}]
    coordinator._ws = _make_ws(events)
    _spy(coordinator, events)
    driver = _Driver(coordinator)
    caplog.set_level(logging.WARNING, logger="custom_components.gaggimate")
    with patch(f"{MODULE}.WS_UNAVAILABLE_TIMEOUT", 1.5):
        await driver.start()
        try:
            await driver.tick()
            first = list(events)
            assert first == [
                "clear(notify=False,last_status=None,pending=True)",
                "set_updated_data({},available=False)",
                "update_listeners(available=False)",
                "ws.close",
            ]
            assert coordinator._last_status_time is None
            assert coordinator._brew_confirm is None
            assert coordinator._available is False
            assert any("marking unavailable" in r.getMessage() for r in caplog.records)

            # A second stale status while already unavailable: no re-notify, but the ws is closed again.
            coordinator._last_status_time = time.monotonic() - STALE_AGE
            await driver.tick()
        finally:
            await driver.stop()
    assert events[len(first):] == ["clear(notify=False,last_status=None,pending=False)", "ws.close"]
    assert coordinator._available is False
    assert coordinator._last_status_time is None


async def test_stale_path_skips_close_when_ws_missing_or_closed(hass: HomeAssistant, events: list[str]) -> None:
    coordinator = _make(hass)
    coordinator._ws = None
    _spy(coordinator, events)
    driver = _Driver(coordinator)
    await driver.start()
    try:
        await driver.tick()
        assert coordinator._available is False
        closed_ws = _make_ws(events, closed=True)
        coordinator._ws = closed_ws
        coordinator._last_status_time = time.monotonic() - STALE_AGE
        await driver.tick()
    finally:
        await driver.stop()
    assert "ws.close" not in events
    closed_ws.close.assert_not_called()


async def test_ws_close_error_is_caught_logged_and_loop_keeps_running(
    hass: HomeAssistant, events: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _make(hass)
    ws = _make_ws(events, close_error=RuntimeError("close boom"))
    coordinator._ws = ws
    _spy(coordinator, events)
    driver = _Driver(coordinator)
    caplog.set_level(logging.ERROR, logger="custom_components.gaggimate")
    await driver.start()
    try:
        await driver.tick()
        # the state change completed before close raised, and the loop survived
        assert coordinator._available is False
        assert coordinator._last_status_time is None
        assert not driver.task.done()
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert errors == ["Availability check error: close boom"]

        # a later stale cycle still happens
        coordinator._last_status_time = time.monotonic() - STALE_AGE
        await driver.tick()
        assert not driver.task.done()
    finally:
        await driver.stop()
    assert ws.close.call_count == 2
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == ["Availability check error: close boom"] * 2
    # notified exactly once (second cycle was already unavailable)
    assert [e for e in events if e.startswith("set_updated_data")] == ["set_updated_data({},available=False)"]


async def test_loop_exits_when_shutting_down(hass: HomeAssistant, events: list[str]) -> None:
    coordinator = _make(hass)
    coordinator._last_status_time = None
    driver = _Driver(coordinator)
    await driver.start()
    coordinator._shutting_down = True
    driver.queue.put_nowait(None)
    assert driver.task is not None
    await asyncio.wait_for(driver.task, 5)  # returns without raising
    driver._patch.stop()
