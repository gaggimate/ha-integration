"""Shared fixtures for the GaggiMate integration tests."""
from __future__ import annotations

import asyncio
import os
import socket
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gaggimate.const import DOMAIN
from custom_components.gaggimate.coordinator import GaggiMateCoordinator

from .fake_server import FakeGaggiMate

DEFAULT_FRAMES = "single_boiler_idle"

if sys.platform == "win32":
    # Local Windows runs only (PHACC does not officially support Windows). The asyncio proactor
    # event loop builds its self-pipe with a TCP loopback ``socket.socketpair()``, which PHACC's
    # socket guard blocks when pytest-asyncio creates the loop. Build that one socketpair with the
    # real socket class; everything else stays guarded. Never active on Linux/CI.
    _real_socket_class = socket.socket
    _real_socketpair = socket.socketpair

    def _socketpair_unguarded(*args: Any, **kwargs: Any) -> Any:
        guarded = socket.socket
        socket.socket = _real_socket_class
        try:
            return _real_socketpair(*args, **kwargs)
        finally:
            socket.socket = guarded

    socket.socketpair = _socketpair_unguarded

    # Second Windows-only problem: HA locates the calling integration by looking for
    # "custom_components/" (forward slash) in frame file names, which never matches on Windows.
    # Then HA treats the coordinator's missing ``config_entry`` argument (tolerated for custom
    # integrations) as an error. Apply the "custom integration" behaviour ourselves.
    from homeassistant.helpers import frame as _ha_frame

    _real_report_usage = _ha_frame.report_usage

    def _report_usage_windows(what: str, *args: Any, **kwargs: Any) -> None:
        try:
            _real_report_usage(what, *args, **kwargs)
        except RuntimeError:
            behavior = kwargs.get("custom_integration_behavior")
            caller = sys._getframe(1)
            while caller is not None:
                if "custom_components" + os.sep in caller.f_code.co_filename:
                    if behavior is _ha_frame.ReportBehavior.IGNORE:
                        return
                    break
                caller = caller.f_back
            raise

    _ha_frame.report_usage = _report_usage_windows


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Make Home Assistant load custom_components/gaggimate."""


@pytest.fixture(autouse=True)
def _fast_reconnect() -> Any:
    """Reconnect after 50 ms instead of 1-30 s."""
    with patch("custom_components.gaggimate.coordinator.WS_RECONNECT_DELAYS", [0.05]):
        yield


@pytest.fixture
async def fake_gaggimate(request: pytest.FixtureRequest, socket_enabled: None) -> AsyncGenerator[FakeGaggiMate]:
    """Fake firmware server on 127.0.0.1.

    Select the frame set with ``@pytest.mark.frames("dual_boiler_idle")``
    (default: single_boiler_idle). ``socket_enabled`` is only needed so the
    loopback listener may be created; tests never talk to any other host.
    """
    marker = request.node.get_closest_marker("frames")
    name = marker.args[0] if marker else DEFAULT_FRAMES
    server = FakeGaggiMate(name)
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
def mock_config_entry(fake_gaggimate: FakeGaggiMate) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="GaggiMate Test",
        unique_id=fake_gaggimate.host,
        data={CONF_HOST: fake_gaggimate.host, CONF_PORT: fake_gaggimate.port},
    )


async def wait_until(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.02) -> None:
    """Poll ``predicate`` (real time) until true or raise TimeoutError."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(interval)


async def settle(delay: float = 0.05) -> None:
    """Let queued callbacks run (short real-time sleep)."""
    await asyncio.sleep(delay)


SetupIntegration = Callable[[], Awaitable[GaggiMateCoordinator]]


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake_gaggimate: FakeGaggiMate
) -> AsyncGenerator[SetupIntegration]:
    """Return a coroutine that sets up the config entry and waits for the first data.

    Waits until the coordinator is available and the profile list and OTA
    settings (requested right after connect) have arrived.
    """

    async def _setup() -> GaggiMateCoordinator:
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        coordinator: GaggiMateCoordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
        await wait_until(lambda: coordinator.available and bool(coordinator.profiles) and bool(coordinator.ota_settings))
        await settle()
        return coordinator

    yield _setup

    # Unload explicitly so teardown order is deterministic.
    if mock_config_entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
