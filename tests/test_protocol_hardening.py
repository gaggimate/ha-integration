"""Step 2 tests: coordinator protocol hardening and mode fixes."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gaggimate.const import DOMAIN, MODE_NAMES, MachineMode
from custom_components.gaggimate.coordinator import GaggiMateCoordinator

from .conftest import SetupIntegration, settle, wait_until
from .fake_server import FakeGaggiMate

MODE_SELECT = "select.gaggimate_test_mode"
PROFILE_SELECT = "select.gaggimate_test_profile"
POWER_SWITCH = "switch.gaggimate_test_machine_active"
BREW_BUTTON = "button.gaggimate_test_start_brew"

STANDARD_MODES = ["Standby", "Brew", "Steam", "Hot Water", "Grind"]


def _without_sys(server: FakeGaggiMate) -> None:
    server.state = {k: v for k, v in server.state.items() if k != "sys"}


# -- (a) profiles list ---------------------------------------------------------------------


async def test_profiles_list_error_does_not_wipe_profiles(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    before = dict(coordinator.profiles)
    assert before

    fake_gaggimate.profiles_list_error = "Update in progress"
    await coordinator.request_profiles_list()  # swallows UpdateFailed itself
    assert coordinator.profiles == before
    assert coordinator._pending_requests == {}

    # a response without `profiles` and without error also keeps the list
    await fake_gaggimate.push({"tp": "res:profiles:list", "rid": "unknown"})
    await settle()
    assert coordinator.profiles == before


# -- (b) res:* errors ----------------------------------------------------------------------


async def test_request_error_raises_update_failed_with_firmware_text(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    with pytest.raises(UpdateFailed, match="Profile not found"):
        await coordinator.select_profile("does-not-exist")
    assert coordinator._pending_requests == {}


async def test_history_delete_error_raises(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    from custom_components.gaggimate.const import MSG_TYPE_HISTORY_DELETE

    coordinator = await setup_integration()
    fake_gaggimate.history_delete_errors["7"] = "Delete failed"
    with pytest.raises(UpdateFailed, match="Delete failed"):
        await coordinator._request({"tp": MSG_TYPE_HISTORY_DELETE, "id": "7"})


async def test_select_profile_service_surfaces_firmware_error(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """Unknown profile id: the firmware's text reaches the service caller."""
    coordinator = await setup_integration()
    # make the entity accept an option that the firmware does not know
    coordinator._profiles["Ghost"] = "ghost-id"
    with pytest.raises(HomeAssistantError, match="Profile not found"):
        await hass.services.async_call(
            "select", "select_option", {"entity_id": PROFILE_SELECT, "option": "Ghost"}, blocking=True
        )


async def test_profile_select_happy_path(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.clear_requests()
    await hass.services.async_call(
        "select", "select_option", {"entity_id": PROFILE_SELECT, "option": "Lever Style"}, blocking=True
    )
    [req] = fake_gaggimate.requests_of("req:profiles:select")
    assert req["id"] == "lever"


async def test_late_response_after_timeout_is_harmless(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await fake_gaggimate.push({"tp": "res:flush:start", "rid": "nobody-waits", "success": True})
    await settle()
    assert coordinator.available
    assert coordinator._pending_requests == {}


# -- (c) readiness guard -------------------------------------------------------------------


@pytest.mark.frames("waiting_controller")
async def test_set_mode_and_start_brew_raise_when_not_ready(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()

    with pytest.raises(UpdateFailed) as exc:
        await coordinator.set_mode(MachineMode.BREW)
    assert "waiting" in str(exc.value)
    assert "Waiting for controller" in str(exc.value)
    with pytest.raises(UpdateFailed, match="waiting"):
        await coordinator.start_brew()

    await settle()
    assert fake_gaggimate.requests_of("req:change-mode") == []
    assert fake_gaggimate.requests_of("req:process:activate") == []


@pytest.mark.frames("waiting_controller")
async def test_power_switch_and_mode_select_inherit_guard(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.clear_requests()

    with pytest.raises(HomeAssistantError, match="not ready"):
        await hass.services.async_call("switch", "turn_on", {"entity_id": POWER_SWITCH}, blocking=True)
    with pytest.raises(HomeAssistantError, match="not ready"):
        await hass.services.async_call("switch", "turn_off", {"entity_id": POWER_SWITCH}, blocking=True)
    with pytest.raises(HomeAssistantError, match="not ready"):
        await hass.services.async_call(
            "select", "select_option", {"entity_id": MODE_SELECT, "option": "Brew"}, blocking=True
        )
    with pytest.raises(HomeAssistantError, match="not ready"):
        await hass.services.async_call("button", "press", {"entity_id": BREW_BUTTON}, blocking=True)
    await settle()
    assert fake_gaggimate.requests_of("req:change-mode") == []
    assert fake_gaggimate.requests_of("req:process:activate") == []


async def test_commands_sent_when_ready(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    assert coordinator.data["sys"]["s"] == "ready"
    fake_gaggimate.clear_requests()
    await coordinator.set_mode(MachineMode.BREW)
    await coordinator.start_brew()
    [mode] = await fake_gaggimate.wait_for_request("req:change-mode")
    await fake_gaggimate.wait_for_request("req:process:activate")
    assert mode["mode"] == 1


async def test_guard_state_follows_frames(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await fake_gaggimate.push_state(sys={"s": "updating", "m": "OTA", "c": 0})
    await wait_until(lambda: coordinator.data["sys"]["s"] == "updating")
    with pytest.raises(UpdateFailed, match="updating"):
        await coordinator.set_mode(MachineMode.STANDBY)
    await fake_gaggimate.push_state(sys={"s": "ready", "m": "", "c": 0})
    await wait_until(lambda: coordinator.data["sys"]["s"] == "ready")
    fake_gaggimate.clear_requests()
    await coordinator.set_mode(MachineMode.STANDBY)
    await fake_gaggimate.wait_for_request("req:change-mode")


async def test_set_mode_and_start_brew_still_send_when_sys_absent(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _without_sys(fake_gaggimate)
    coordinator = await setup_integration()
    assert "sys" not in coordinator.data
    fake_gaggimate.clear_requests()
    await coordinator.set_mode(MachineMode.BREW)
    await coordinator.start_brew()
    await fake_gaggimate.wait_for_request("req:change-mode")
    await fake_gaggimate.wait_for_request("req:process:activate")


async def test_set_mode_and_start_brew_still_send_when_sys_state_null(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await fake_gaggimate.push_state(sys={"s": None, "m": "", "c": 0})
    await wait_until(lambda: coordinator.data["sys"]["s"] is None)
    # also `sys: null`
    fake_gaggimate.clear_requests()
    await coordinator.set_mode(MachineMode.BREW)
    await coordinator.start_brew()
    await fake_gaggimate.wait_for_request("req:change-mode")
    await fake_gaggimate.wait_for_request("req:process:activate")

    await fake_gaggimate.push_state(sys=None)
    await wait_until(lambda: coordinator.data["sys"] is None)
    fake_gaggimate.clear_requests()
    await coordinator.set_mode(MachineMode.STANDBY)
    await fake_gaggimate.wait_for_request("req:change-mode")


# -- (d) mode select options ----------------------------------------------------------------


async def test_mode_select_options_single_boiler(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    assert list(MODE_NAMES.values()) == STANDARD_MODES
    state = hass.states.get(MODE_SELECT)
    assert state.attributes["options"] == STANDARD_MODES


@pytest.mark.frames("dual_boiler_idle")
async def test_mode_select_options_dual_boiler(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    assert coordinator.data["db"] is True
    state = hass.states.get(MODE_SELECT)
    assert state.attributes["options"] == ["Standby", "Brew", "Grind"]

    # a stale mode value that the options no longer contain never leaks out as current option
    for mode in (MachineMode.STEAM, MachineMode.WATER):
        await fake_gaggimate.push({"tp": "evt:status", "m": int(mode)})
        await wait_until(lambda: coordinator.data["m"] == int(mode))
        await settle()
        state = hass.states.get(MODE_SELECT)
        assert state.state not in ("Steam", "Hot Water")
        assert state.state in state.attributes["options"] or state.state in ("unknown", "unavailable")

    await fake_gaggimate.push({"tp": "evt:status", "m": int(MachineMode.GRIND)})
    await wait_until(lambda: hass.states.get(MODE_SELECT).state == "Grind")


async def test_mode_select_current_option_always_within_options(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    entity = next(
        e for e in hass.data["select"].entities if e.entity_id == MODE_SELECT
    )
    for db in (False, True):
        await fake_gaggimate.push({"tp": "evt:status", "db": db})
        await wait_until(lambda: coordinator.data["db"] is db)
        for mode in list(range(0, 5)) + [99]:
            await fake_gaggimate.push({"tp": "evt:status", "m": mode})
            await wait_until(lambda: coordinator.data["m"] == mode)
            assert entity.current_option is None or entity.current_option in entity.options


@pytest.mark.frames("dual_boiler_idle")
async def test_dual_boiler_rejects_steam_option(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.clear_requests()
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "select", "select_option", {"entity_id": MODE_SELECT, "option": "Steam"}, blocking=True
        )
    await settle()
    assert fake_gaggimate.requests_of("req:change-mode") == []


async def test_select_mode_sends_integer(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    expected = {"Standby": 0, "Brew": 1, "Steam": 2, "Hot Water": 3, "Grind": 4}
    for option, value in expected.items():
        fake_gaggimate.clear_requests()
        await hass.services.async_call(
            "select", "select_option", {"entity_id": MODE_SELECT, "option": option}, blocking=True
        )
        [req] = await fake_gaggimate.wait_for_request("req:change-mode")
        assert type(req["mode"]) is int
        assert req["mode"] == value


async def test_power_switch_sends_integer_modes(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.clear_requests()
    await hass.services.async_call("switch", "turn_on", {"entity_id": POWER_SWITCH}, blocking=True)
    await hass.services.async_call("switch", "turn_off", {"entity_id": POWER_SWITCH}, blocking=True)
    reqs = await fake_gaggimate.wait_for_request("req:change-mode", 2)
    assert [r["mode"] for r in reqs] == [1, 0]
    assert all(type(r["mode"]) is int for r in reqs)


async def test_coordinator_set_mode_coerces_to_int(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    await coordinator.set_mode(MachineMode.GRIND)
    await coordinator.set_mode(2.0)  # type: ignore[arg-type]
    reqs = await fake_gaggimate.wait_for_request("req:change-mode", 2)
    assert [r["mode"] for r in reqs] == [4, 2]
    assert all(type(r["mode"]) is int for r in reqs)


# -- connect failure / timeout / cleanup -----------------------------------------------------


async def test_connect_timeout_message_is_not_empty(hass: HomeAssistant, socket_enabled: None) -> None:
    """A server that accepts TCP but never completes the websocket upgrade."""
    connections: list[asyncio.StreamWriter] = []

    async def _hang(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append(writer)
        await reader.read()  # returns when the client closes
        writer.close()

    server = await asyncio.start_server(_hang, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    coordinator = GaggiMateCoordinator(hass, "127.0.0.1", port)
    try:
        with patch("custom_components.gaggimate.coordinator.WS_CONNECT_TIMEOUT", 0.3):
            with pytest.raises(UpdateFailed) as exc:
                await coordinator.async_start()
        message = str(exc.value)
        assert message.startswith("Failed to connect:")
        assert message.removeprefix("Failed to connect:").strip() != ""
        # failed first connect closes its session
        assert coordinator._session is None
    finally:
        await coordinator.async_shutdown()
        for writer in connections:
            writer.close()
        server.close()
        await server.wait_closed()
        await asyncio.sleep(0.1)


async def test_failed_first_connect_leaves_no_session(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate
) -> None:
    port = fake_gaggimate.port
    await fake_gaggimate.stop()
    coordinator = GaggiMateCoordinator(hass, "127.0.0.1", port)
    created: list = []
    import aiohttp

    real_cls = aiohttp.ClientSession

    class _Spy(real_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created.append(self)

    with patch("custom_components.gaggimate.coordinator.aiohttp.ClientSession", _Spy):
        with pytest.raises(UpdateFailed) as exc:
            await coordinator.async_start()
    assert str(exc.value).removeprefix("Failed to connect:").strip() != ""
    assert created, "a session should have been created"
    assert all(s.closed for s in created)
    assert coordinator._session is None
    assert coordinator._availability_check_task is None
    assert coordinator._listen_task is None


async def test_failed_setup_entry_retry_leaves_no_session(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate
) -> None:
    port = fake_gaggimate.port
    await fake_gaggimate.stop()
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "127.0.0.1", "port": port})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# -- silent device grace period ---------------------------------------------------------------


async def test_silent_device_reconnects_after_timeout_not_before(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, fake_gaggimate: FakeGaggiMate
) -> None:
    """No heartbeat: a device that connects but never sends status gets the full timeout."""
    timeout = 2.5
    fake_gaggimate.send_initial = False
    fake_gaggimate.keepalive = False
    with patch("custom_components.gaggimate.coordinator.WS_UNAVAILABLE_TIMEOUT", timeout):
        mock_config_entry.add_to_hass(hass)
        start = time.monotonic()
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        try:
            await wait_until(lambda: fake_gaggimate.connect_count >= 1)
            first_connect = time.monotonic()
            await asyncio.sleep(timeout - 0.8)
            assert fake_gaggimate.connect_count == 1, "reconnected inside the grace period"
            await wait_until(lambda: fake_gaggimate.connect_count >= 2, timeout=timeout + 4)
            elapsed = time.monotonic() - first_connect
            assert elapsed >= timeout - 0.2, f"reconnect after {elapsed:.2f}s, before timeout {timeout}s"
            assert elapsed <= timeout + 3.0, f"reconnect took {elapsed:.2f}s"
            assert time.monotonic() - start < 15
        finally:
            await hass.config_entries.async_unload(mock_config_entry.entry_id)
            await hass.async_block_till_done()


# -- unload ordering --------------------------------------------------------------------------


async def test_unload_during_traffic_leaves_nothing_behind(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
) -> None:
    coordinator = await setup_integration()

    stop = False

    async def _flood() -> None:
        i = 0
        while not stop:
            await fake_gaggimate.push({"tp": "evt:status", "ct": 90 + (i % 5)})
            i += 1
            await asyncio.sleep(0.005)

    flood = asyncio.create_task(_flood())
    await asyncio.sleep(0.2)
    # an unanswered request in flight at unload time
    fake_gaggimate.profiles_list_error = None
    pending = asyncio.create_task(coordinator._request({"tp": "req:never-answered"}))
    await asyncio.sleep(0.05)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    stop = True
    await flood
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert coordinator._listen_task is None
    assert coordinator._availability_check_task is None
    assert coordinator._reconnect_task is None
    assert coordinator._ws is None
    assert coordinator._session is None
    # the in-flight request was failed, not left hanging for 30 s
    with pytest.raises(UpdateFailed):
        await asyncio.wait_for(pending, 2)
    await wait_until(lambda: not fake_gaggimate.connections)
    await asyncio.sleep(0.3)
    assert fake_gaggimate.connect_count == 1


async def test_unload_after_reconnect_and_reload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
) -> None:
    coordinator = await setup_integration()
    await fake_gaggimate.drop_connections()
    await wait_until(lambda: fake_gaggimate.connect_count >= 2 and coordinator.available)
    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    assert len(er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)) > 0
