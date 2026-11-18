"""Baseline tests pinning the entities and commands of origin/main."""
from __future__ import annotations

import asyncio

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import SetupIntegration, settle, wait_until
from .fake_server import FakeGaggiMate, load_json

HOST = "127.0.0.1"

EXPECTED_KEYS = {
    # sensors
    "current_temperature", "target_temperature", "status", "process_phase", "mode",
    "selected_profile", "scale_connection_status", "current_weight", "current_pressure",
    "target_pressure", "pump_flow", "target_shot_volume", "shot_volume_progress",
    "hardware_model", "software_display_version", "display_software_update",
    "software_controller_version", "controller_software_update", "latest_software_version",
    "water_level", "tof_distance", "warning_water", "warning_temperature", "warning_switch",
    "warning_flush", "warning_scale_connected", "warning_scale_battery",
    # other platforms
    "power", "mode_select", "profile_select", "brew_start", "brew_stop", "flush",
    "target_temperature_setpoint",
}
EXPECTED_UNIQUE_IDS = {f"{HOST}_{key}" for key in EXPECTED_KEYS}

PLATFORM_OF = {
    "power": "switch",
    "mode_select": "select",
    "profile_select": "select",
    "brew_start": "button",
    "brew_stop": "button",
    "flush": "button",
    "target_temperature_setpoint": "number",
}


def _entry_entities(hass: HomeAssistant, entry: MockConfigEntry) -> list[er.RegistryEntry]:
    return er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)


def _entity_id(hass: HomeAssistant, domain: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, "gaggimate", f"{HOST}_{key}")
    assert entity_id is not None, f"{domain} {key} not registered"
    return entity_id


@pytest.mark.parametrize("frames", ["single_boiler_idle", "dual_boiler_idle", "waiting_controller"])
async def test_all_current_entities_created(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    frames: str,
) -> None:
    """Existing entities are created unconditionally with the documented unique_ids."""
    # the server starts with the default frame set; swap in the parametrized one before connecting
    data = load_json(frames)
    fake_gaggimate.state, fake_gaggimate.telemetry = data["state"], data["telemetry"]
    await setup_integration()

    entities = _entry_entities(hass, mock_config_entry)
    assert {e.unique_id for e in entities} == EXPECTED_UNIQUE_IDS
    assert len(entities) == len(EXPECTED_KEYS) == 34
    for entity in entities:
        key = entity.unique_id.removeprefix(f"{HOST}_")
        assert entity.domain == PLATFORM_OF.get(key, "sensor")
        assert entity.platform == "gaggimate"
        assert entity.disabled_by is None
    # entities hang off one device identified by the host
    device_ids = {e.device_id for e in entities}
    assert len(device_ids) == 1


async def test_idle_sensor_values(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    await setup_integration()

    def state(key: str) -> str:
        return hass.states.get(_entity_id(hass, "sensor", key)).state

    assert float(state("current_temperature")) == 93.4
    assert float(state("target_temperature")) == 93.0
    assert state("status") == "Idle"
    assert state("mode") == "Brew"
    assert hass.states.get(_entity_id(hass, "sensor", "mode")).attributes["mode_id"] == 1
    assert state("selected_profile") == "Default"
    assert state("scale_connection_status") == "Disconnected"
    assert state("current_weight") == "unknown"  # no BLE scale connected
    assert float(state("current_pressure")) == 0.02
    assert float(state("pump_flow")) == 0.0
    assert state("shot_volume_progress") == "unknown"
    assert state("hardware_model") == "GaggiMate Pro"  # from res:ota-settings
    assert state("software_display_version") == "v1.7.0"
    assert float(state("water_level")) == 100
    for key in ("water", "temperature", "switch", "flush", "scale_connected", "scale_battery"):
        assert state(f"warning_{key}") == "Inactive"
        assert hass.states.get(_entity_id(hass, "sensor", f"warning_{key}")).attributes["severity"] == "Warning"


@pytest.mark.frames("dual_boiler_brewing")
async def test_brewing_sensor_values(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    await setup_integration()

    status = hass.states.get(_entity_id(hass, "sensor", "status"))
    assert status.state == "Brewing"
    assert status.attributes["icon"] == "mdi:coffee"
    assert status.attributes["elapsed_seconds"] == 12.0
    assert hass.states.get(_entity_id(hass, "sensor", "process_phase")).state == "Extraction"
    assert float(hass.states.get(_entity_id(hass, "sensor", "current_pressure")).state) == 8.9
    assert float(hass.states.get(_entity_id(hass, "sensor", "pump_flow")).state) == 1.3
    # time-based process: no volumetric progress
    assert hass.states.get(_entity_id(hass, "sensor", "shot_volume_progress")).state == "unknown"


@pytest.mark.frames("warnings_full")
async def test_active_warning_sensor(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    await setup_integration()
    water = hass.states.get(_entity_id(hass, "sensor", "warning_water"))
    assert water.state == "Active"
    assert water.attributes["severity"] == "Error"
    assert water.attributes["severity_level"] == 2
    assert water.attributes["icon"] == "mdi:alert-circle"


@pytest.mark.frames("scale_connected")
async def test_scale_sensors(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    await setup_integration()
    assert hass.states.get(_entity_id(hass, "sensor", "scale_connection_status")).state == "Connected"
    assert float(hass.states.get(_entity_id(hass, "sensor", "current_weight")).state) == 18.2


async def test_mode_select(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    entity_id = _entity_id(hass, "select", "mode_select")
    state = hass.states.get(entity_id)
    assert state.state == "Brew"
    assert state.attributes["options"] == ["Standby", "Brew", "Steam", "Hot Water", "Grind"]

    fake_gaggimate.clear_requests()
    await hass.services.async_call(
        "select", "select_option", {"entity_id": entity_id, "option": "Grind"}, blocking=True
    )
    [request] = await fake_gaggimate.wait_for_request("req:change-mode")
    assert request["mode"] == 4 and isinstance(request["mode"], int) and not isinstance(request["mode"], bool)

    await fake_gaggimate.push({"tp": "evt:status", "m": 4})
    await wait_until(lambda: hass.states.get(entity_id).state == "Grind")


async def test_machine_active_switch(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    entity_id = _entity_id(hass, "switch", "power")
    assert hass.states.get(entity_id).state == "on"  # mode 1 = brew

    await hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
    [request] = await fake_gaggimate.wait_for_request("req:change-mode")
    assert request["mode"] == 0

    await fake_gaggimate.push({"tp": "evt:status", "m": 0})
    await wait_until(lambda: hass.states.get(entity_id).state == "off")

    fake_gaggimate.clear_requests()
    await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
    [request] = await fake_gaggimate.wait_for_request("req:change-mode")
    assert request["mode"] == 1  # brew


async def test_buttons_send_expected_messages(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.clear_requests()

    for key in ("brew_start", "brew_stop", "flush"):
        await hass.services.async_call(
            "button", "press", {"entity_id": _entity_id(hass, "button", key)}, blocking=True
        )

    await fake_gaggimate.wait_for_request("req:flush:start")
    assert [r["tp"] for r in fake_gaggimate.requests] == [
        "req:process:activate",
        "req:process:deactivate",
        "req:flush:start",
    ]
    assert fake_gaggimate.requests[0] == {"tp": "req:process:activate"}  # no ignoreWarnings today
    assert fake_gaggimate.requests[1] == {"tp": "req:process:deactivate"}
    assert "rid" in fake_gaggimate.requests[2]


async def test_target_temperature_number(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    entity_id = _entity_id(hass, "number", "target_temperature_setpoint")
    state = hass.states.get(entity_id)
    assert float(state.state) == 93.0
    assert state.attributes["min"] == 0 and state.attributes["max"] == 160 and state.attributes["step"] == 1

    fake_gaggimate.clear_requests()
    await hass.services.async_call("number", "set_value", {"entity_id": entity_id, "value": 91}, blocking=True)
    await fake_gaggimate.wait_for_request("req:lower-temp", 2)
    await asyncio.sleep(0.1)
    assert [r["tp"] for r in fake_gaggimate.requests] == ["req:lower-temp"] * 2


async def test_profile_select(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    entity_id = _entity_id(hass, "select", "profile_select")
    state = hass.states.get(entity_id)
    assert state.attributes["options"] == ["Default", "Flush Utility", "Lever Style"]
    assert state.state == "Default"

    fake_gaggimate.clear_requests()
    await hass.services.async_call(
        "select", "select_option", {"entity_id": entity_id, "option": "Lever Style"}, blocking=True
    )
    [request] = fake_gaggimate.requests_of("req:profiles:select")
    assert request["id"] == "lever" and request["rid"]

    await fake_gaggimate.push({"tp": "evt:status", "p": "Lever Style", "puid": "lever"})
    await wait_until(lambda: hass.states.get(entity_id).state == "Lever Style")
