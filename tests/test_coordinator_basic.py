"""Baseline tests for the websocket coordinator (behaviour of origin/main)."""
from __future__ import annotations

import asyncio
import copy
import socket
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.const import STATE_UNAVAILABLE
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gaggimate.const import DOMAIN, SERVICE_TRIM_SHOT_HISTORY

from .conftest import SetupIntegration, settle, wait_until
from .fake_server import FakeGaggiMate, load_json

TEMP_SENSOR = "sensor.gaggimate_test_current_temperature"


async def test_setup_primes_data_and_unload_cleans_up(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
) -> None:
    """Connecting requests OTA settings + profiles; unload leaves nothing running."""
    coordinator = await setup_integration()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert coordinator.available
    assert len(fake_gaggimate.requests_of("req:ota-settings")) == 1
    assert len(fake_gaggimate.requests_of("req:profiles:list")) == 1
    assert coordinator.profiles == {"Default": "default", "Flush Utility": "flush", "Lever Style": "lever"}
    assert coordinator.ota_settings["hardware"] == "GaggiMate Pro"
    assert hass.services.has_service(DOMAIN, SERVICE_TRIM_SHOT_HISTORY)
    assert len(fake_gaggimate.connections) == 1

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert DOMAIN not in hass.data or mock_config_entry.entry_id not in hass.data[DOMAIN]
    assert not hass.services.has_service(DOMAIN, SERVICE_TRIM_SHOT_HISTORY)
    # no lingering tasks / sessions / sockets
    assert coordinator._availability_check_task is None
    assert coordinator._listen_task is None
    assert coordinator._reconnect_task is None
    assert coordinator._ws is None
    assert coordinator._session is None
    await wait_until(lambda: not fake_gaggimate.connections)
    # server saw exactly one connection: no reconnect after unload
    await asyncio.sleep(0.3)
    assert fake_gaggimate.connect_count == 1
    # (PHACC's verify_cleanup fixture additionally fails the test on lingering tasks/timers)


async def test_connect_failure_raises_not_ready(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate
) -> None:
    """Config entry goes to SETUP_RETRY when the device is unreachable."""
    port = fake_gaggimate.port
    await fake_gaggimate.stop()  # port now closed
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "127.0.0.1", "port": port})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_partial_frames_are_merged(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """State frame + telemetry frame are merged; later partial frames only touch their keys."""
    coordinator = await setup_integration()
    data = coordinator.data
    # keys from the slow-state frame AND the telemetry frame are both present
    assert data["sys"] == {"s": "ready", "m": "", "c": 0}
    assert data["p"] == "Default"
    assert data["ct"] == 93.4

    # a telemetry-only frame updates its keys and keeps the slow-state ones
    await fake_gaggimate.push({"tp": "evt:status", "ct": 95.5})
    await wait_until(lambda: coordinator.data["ct"] == 95.5)
    assert coordinator.data["p"] == "Default"
    assert coordinator.data["sys"]["s"] == "ready"
    assert coordinator.data["tt"] == 93

    # a state-only frame keeps telemetry
    await fake_gaggimate.push({"tp": "evt:status", "p": "Lever Style"})
    await wait_until(lambda: coordinator.data["p"] == "Lever Style")
    assert coordinator.data["ct"] == 95.5


async def test_null_value_clears_key_but_keeps_it_present(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """A key sent as null stays in the dict with value None (gating must use `key in data`)."""
    coordinator = await setup_integration()

    await fake_gaggimate.push({"tp": "evt:status", "sbat": 80, "process": {"a": 1, "s": "brew"}})
    await wait_until(lambda: coordinator.data.get("sbat") == 80)
    assert coordinator.data["process"]["a"] == 1

    await fake_gaggimate.push({"tp": "evt:status", "sbat": None, "process": None})
    await wait_until(lambda: coordinator.data.get("sbat") is None and coordinator.data["process"] is None)
    assert "sbat" in coordinator.data
    assert "process" in coordinator.data


async def test_non_status_events_and_garbage_do_not_disturb(
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """evt:* frames other than status are ignored today; invalid JSON is logged and survived."""
    coordinator = await setup_integration()
    before = copy.deepcopy(coordinator.data)

    for frame in load_json("events")["events"].values():
        await fake_gaggimate.push(frame)
    await fake_gaggimate.push_raw("{not json")
    await asyncio.sleep(0.1)

    assert "Invalid WebSocket JSON" in caplog.text
    assert coordinator.available
    assert {k: v for k, v in coordinator.data.items() if k not in ("tp",)} == {
        k: v for k, v in before.items() if k not in ("tp",)
    }
    # and the connection still works for later frames
    await fake_gaggimate.push({"tp": "evt:status", "ct": 90.1})
    await wait_until(lambda: coordinator.data["ct"] == 90.1)
    assert fake_gaggimate.connect_count == 1


async def test_unavailable_after_timeout_and_reconnect_restores(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """No status frames within WS_UNAVAILABLE_TIMEOUT -> entities unavailable; reconnect restores."""
    with patch("custom_components.gaggimate.coordinator.WS_UNAVAILABLE_TIMEOUT", 0.5):
        coordinator = await setup_integration()
        assert hass.states.get(TEMP_SENSOR).state == "93.4"

        fake_gaggimate.keepalive = False  # firmware goes silent
        await wait_until(lambda: not coordinator.available, timeout=5)
        # the watchdog marked everything unavailable (observed at that moment, before the
        # reconnect re-sends the initial frames)
        assert hass.states.get(TEMP_SENSOR).state == STATE_UNAVAILABLE
        assert hass.states.get("switch.gaggimate_test_machine_active").state == STATE_UNAVAILABLE

        # firmware talks again; the coordinator closed the socket itself and reconnects
        # (fast delay patched in conftest)
        fake_gaggimate.keepalive = True
        await wait_until(lambda: coordinator.available, timeout=5)
        await settle()
        assert fake_gaggimate.connect_count >= 2
        assert hass.states.get(TEMP_SENSOR).state == "93.4"
        assert hass.states.get("switch.gaggimate_test_machine_active").state == "on"


async def test_server_disconnect_triggers_reconnect(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """Dropped connection -> entities unavailable, reconnect, data again."""
    coordinator = await setup_integration()
    fake_gaggimate.keepalive = False
    await fake_gaggimate.drop_connections()
    await wait_until(lambda: fake_gaggimate.connect_count >= 2)
    await wait_until(lambda: coordinator.available)
    await settle()
    assert hass.states.get(TEMP_SENSOR).state == "93.4"
    assert len(fake_gaggimate.requests_of("req:ota-settings")) >= 2  # primed again after reconnect


async def test_flush_request_is_answered_by_rid(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """req:flush:start carries a rid; the res:flush:start answer resolves the call quickly."""
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()

    async with asyncio.timeout(3):
        await coordinator.start_flush()

    [request] = fake_gaggimate.requests_of("req:flush:start")
    assert isinstance(request["rid"], str) and request["rid"]
    assert request["rid"] not in coordinator._pending_requests


async def test_temperature_steps_on_the_wire(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """set_temperature sends one raise/lower message per degree and nothing for no change."""
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()

    await coordinator.set_temperature(96)  # target is 93
    await coordinator.set_temperature(93)  # no change -> nothing
    await fake_gaggimate.wait_for_request("req:raise-temp", 3)
    assert len(fake_gaggimate.requests_of("req:raise-temp")) == 3
    assert fake_gaggimate.requests_of("req:lower-temp") == []

    await fake_gaggimate.push({"tp": "evt:status", "tt": 96})
    await wait_until(lambda: coordinator.data["tt"] == 96)
    await coordinator.set_temperature(94)
    await fake_gaggimate.wait_for_request("req:lower-temp", 2)
    assert len(fake_gaggimate.requests_of("req:lower-temp")) == 2


async def test_known_bug_profiles_list_request_never_resolved(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """Pins a known defect of origin/main (plan 2a "Existing client bugs" (1)).

    `res:profiles:list` updates the profile list but is consumed before its `rid` future is
    resolved, so the pending request only ends by timeout (30 s) or connection loss.
    Step 2 fixes this; this test must then be inverted.
    """
    coordinator = await setup_integration()
    assert coordinator.profiles
    assert len(coordinator._pending_requests) == 1


async def test_no_real_network(fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration) -> None:
    """The harness only reaches 127.0.0.1; other hosts are blocked by the socket guard."""
    await setup_integration()
    with pytest.raises(Exception) as excinfo:
        socket.create_connection(("192.0.2.1", 80), timeout=1)
    assert "blocked" in type(excinfo.value).__name__.lower() or "blocked" in str(excinfo.value).lower()
    # the deliberate violation must not trip PHACC's "the test opens sockets" teardown assertion
    from pytest_homeassistant_custom_component.plugins import HASocketBlockedError

    HASocketBlockedError.instances.clear()
