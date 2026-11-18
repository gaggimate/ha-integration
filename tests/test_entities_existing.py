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

# Step 5 diagnostics. Present whenever the firmware sends the key (sys, lnk, lat, rtx, up, sbat, scaleSource).
DIAGNOSTIC_KEYS = {
    "system_state", "controller_error", "link_type", "link_rssi", "link_latency", "link_retransmits",
    "update_available", "scale_battery", "scale_source",
}
# Warning sensors (step 5) are gated on the key being present in `warn`.
DIAGNOSTIC_WARNING_KEYS = {"warning_backflush", "warning_descaling"}

# Step 6 power sensors: present whenever the telemetry frame carries hp / pw.
POWER_KEYS = {"heater_power", "pump_power"}
# Step 6 puck and flow sensors: gated on cp AND cd (target_flow on cd only). water_pumped also needs `wp`.
PUCK_KEYS = {"puck_resistance", "puck_flow", "target_flow"}
WATER_PUMPED_KEYS = {"water_pumped"}
# Step 6 steam sensors: gated on db (dual boiler).
STEAM_KEYS = {"steam_temperature", "target_steam_temperature"}

# Step 8 binary sensors: Tank Level Low needs htl and a non-null tlo; Steam Boiler Low needs hsl and a non-null sbl.
TANK_STEAM_LEVEL_KEYS = {"tank_level_low", "steam_boiler_low"}

# Step 9: Flush Stop and Clear Process always; Tare Scale only with hs true (no fixture has it);
# Steam Heater switch only with db true.
# flush_stop/process_clear (step 9) and the always-on brew confirmation entities (step 10)
SIMPLE_COMMAND_KEYS = {"flush_stop", "process_clear", "brew_confirm_pending", "brew_confirm", "brew_confirm_cancel"}
STEAM_HEATER_KEYS = {"steam_heater"}
# Step 11: Grind switch with gact present; Grind By Weight switch with bta == 1 (gact is in every fixture).
GRIND_KEYS = {"grind"}
GRIND_BY_WEIGHT_KEYS = {"grind_by_weight"}
# Step 12: Grind Target Duration number with gtd present; Grind Target Weight number with bta == 1 and gtv present.
GRIND_TARGET_DURATION_KEYS = {"grind_target_duration"}
GRIND_TARGET_WEIGHT_KEYS = {"grind_target_weight"}
# Step 13: four cleaning buttons, each gated on its warn key (backflush / descaling); dual_boiler_idle has both.
CLEANING_BUTTON_KEYS = {"backflush_start", "backflush_complete", "descaling_start", "descaling_complete"}

# Step 14: two always-on event entities.
EVENT_KEYS = {"shot_events", "machine_events"}

# Disabled by default in the entity registry (entity_registry_enabled_default=False).
DISABLED_BY_DEFAULT = {
    "link_rssi", "link_latency", "link_retransmits", "puck_resistance", "target_flow",
    "backflush_start", "descaling_start",
}

PLATFORM_OF = {
    "power": "switch",
    "mode_select": "select",
    "profile_select": "select",
    "brew_start": "button",
    "brew_stop": "button",
    "flush": "button",
    "tare_scale": "button",
    "flush_stop": "button",
    "process_clear": "button",
    "brew_confirm": "button",
    "brew_confirm_cancel": "button",
    "brew_confirm_pending": "binary_sensor",
    "steam_heater": "switch",
    "grind": "switch",
    "grind_by_weight": "switch",
    "target_temperature_setpoint": "number",
    "grind_target_duration": "number",
    "grind_target_weight": "number",
    "controller_error": "binary_sensor",
    "update_available": "binary_sensor",
    "tank_level_low": "binary_sensor",
    "steam_boiler_low": "binary_sensor",
    "backflush_start": "button",
    "backflush_complete": "button",
    "descaling_start": "button",
    "descaling_complete": "button",
    "shot_events": "event",
    "machine_events": "event",
}


def _entry_entities(hass: HomeAssistant, entry: MockConfigEntry) -> list[er.RegistryEntry]:
    return er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)


def _entity_id(hass: HomeAssistant, domain: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, "gaggimate", f"{HOST}_{key}")
    assert entity_id is not None, f"{domain} {key} not registered"
    return entity_id


@pytest.mark.parametrize(
    ("frames", "extra_keys"),
    [
        # no backflush/descaling entry in `warn` -> those warning sensors and cleaning buttons are not created;
        # cp/cd false and db false -> no puck, flow or steam sensors (heater/pump power always, from hp/pw)
        ("single_boiler_idle", DIAGNOSTIC_KEYS | POWER_KEYS | SIMPLE_COMMAND_KEYS | GRIND_KEYS | GRIND_TARGET_DURATION_KEYS | EVENT_KEYS),
        ("waiting_controller", DIAGNOSTIC_KEYS | POWER_KEYS | SIMPLE_COMMAND_KEYS | GRIND_KEYS | GRIND_TARGET_DURATION_KEYS | EVENT_KEYS),
        # cp and cd true -> puck/flow sensors; pkr/pf/tf present, wp absent (no process); db true -> steam;
        # htl and tlo true, hsl true and sbl false -> both Step 8 binary sensors
        (
            "dual_boiler_idle",
            DIAGNOSTIC_KEYS | DIAGNOSTIC_WARNING_KEYS | POWER_KEYS | PUCK_KEYS | STEAM_KEYS | TANK_STEAM_LEVEL_KEYS
            | SIMPLE_COMMAND_KEYS | STEAM_HEATER_KEYS | GRIND_KEYS | GRIND_BY_WEIGHT_KEYS
            | GRIND_TARGET_DURATION_KEYS | GRIND_TARGET_WEIGHT_KEYS | CLEANING_BUTTON_KEYS | EVENT_KEYS,
        ),
    ],
)
async def test_all_current_entities_created(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    frames: str,
    extra_keys: set[str],
) -> None:
    """Existing entities are created unconditionally with the documented unique_ids."""
    # the server starts with the default frame set; swap in the parametrized one before connecting
    data = load_json(frames)
    fake_gaggimate.state, fake_gaggimate.telemetry = data["state"], data["telemetry"]
    await setup_integration()

    expected_keys = EXPECTED_KEYS | extra_keys
    expected_unique_ids = {f"{HOST}_{key}" for key in expected_keys}
    entities = _entry_entities(hass, mock_config_entry)
    assert {e.unique_id for e in entities} == expected_unique_ids
    assert len(entities) == len(expected_keys)
    for entity in entities:
        key = entity.unique_id.removeprefix(f"{HOST}_")
        assert entity.domain == PLATFORM_OF.get(key, "sensor")
        assert entity.platform == "gaggimate"
        if key in DISABLED_BY_DEFAULT:
            assert entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        else:
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
