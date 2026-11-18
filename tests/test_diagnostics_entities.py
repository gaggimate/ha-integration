"""Step 5: system, link and update diagnostics, warning sensors and the binary_sensor platform.

Expectations follow the specification (plan.md, Step 5 and 3d), not the implementation.
Frames are the synthetic fixtures in tests/fixtures/frames or inline edits of them.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gaggimate import coordinator as coordinator_module
from custom_components.gaggimate.coordinator import GaggiMateCoordinator

from .conftest import SetupIntegration, settle, wait_until
from .fake_server import FakeGaggiMate, load_json

HOST = "127.0.0.1"
SYSTEM_STATES = ["starting", "waiting", "ready", "updating", "autotuning", "mismatch", "error"]
LINK_TYPES = ["ble", "uart", "none"]
SCALE_SOURCES = ["hardware", "bluetooth", "estimation", "inactive"]
# loggers whose warnings would be HA "invalid state / enum" complaints or integration errors
OWN_LOGGERS = ("custom_components.gaggimate", "homeassistant.components.sensor", "homeassistant.components.binary_sensor")


# -- helpers -----------------------------------------------------------------------------------------


def _eid(hass: HomeAssistant, domain: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, "gaggimate", f"{HOST}_{key}")


def _state(hass: HomeAssistant, domain: str, key: str) -> Any:
    entity_id = _eid(hass, domain, key)
    assert entity_id is not None, f"{domain} {key} not registered"
    return hass.states.get(entity_id)


def _registry_entry(hass: HomeAssistant, domain: str, key: str) -> er.RegistryEntry:
    entity_id = _eid(hass, domain, key)
    assert entity_id is not None, f"{domain} {key} not registered"
    return er.async_get(hass).async_get(entity_id)


def _own_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Warning-or-worse records from the integration or HA's sensor platforms."""
    return [
        f"{r.name}: {r.getMessage()}"
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith(OWN_LOGGERS)
    ]


async def _push_and_wait(
    fake: FakeGaggiMate,
    coordinator: GaggiMateCoordinator,
    *,
    telemetry: bool = False,
    **changes: Any,
) -> None:
    """Push a partial frame and wait until the coordinator has merged it.

    Waiting on the merged data (not on the entity state) avoids passing on a value the entity already had.
    """
    if telemetry:
        await fake.push_telemetry(**changes)
    else:
        await fake.push_state(**changes)
    await wait_until(lambda: all(coordinator.data.get(k) == v for k, v in changes.items()))
    await settle(0.02)


async def _push_full(fake: FakeGaggiMate, state: dict[str, Any]) -> None:
    """Replace the slow-state frame (what a reconnect would send too) and push it."""
    fake.state = state
    await fake.push(state)


def _warn_entry(key: str, level: int, active: bool) -> dict[str, Any]:
    return {"k": key, "l": level, "a": active}


def _ota_requests(fake: FakeGaggiMate) -> int:
    return len(fake.requests_of("req:ota-settings"))


# -- System State (sensor ENUM) ----------------------------------------------------------------------


@pytest.mark.parametrize("value", SYSTEM_STATES)
async def test_system_state_each_value(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
    value: str,
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, sys={"s": value, "m": f"message for {value}", "c": 0})

    state = _state(hass, "sensor", "system_state")
    assert state.state == value
    assert state.attributes["message"] == f"message for {value}"
    assert state.attributes["error_code"] == 0
    assert state.attributes["options"] == SYSTEM_STATES
    assert _own_warnings(caplog) == []


async def test_system_state_error_attributes(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, sys={"s": "error", "m": "Heater fault", "c": 4})
    attrs = _state(hass, "sensor", "system_state").attributes
    assert _state(hass, "sensor", "system_state").state == "error"
    assert attrs["message"] == "Heater fault"
    assert attrs["error_code"] == 4


async def test_system_state_invalid_value_is_unknown_without_warnings(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.gaggimate.sensor")
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, sys={"s": "rebooting", "m": "", "c": 0})

    assert _state(hass, "sensor", "system_state").state == "unknown"
    assert _own_warnings(caplog) == []
    assert any(
        r.levelno == logging.DEBUG and "Unknown system state value" in r.getMessage() for r in caplog.records
    )


async def test_system_state_null_sys_is_unknown(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, sys=None)
    state = _state(hass, "sensor", "system_state")
    assert state.state == "unknown"
    assert "message" not in state.attributes


async def test_sys_absent_from_start_creates_no_sys_entities(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """No `sys` key at all: System State and Controller Error are not created (gated on the key)."""
    fake_gaggimate.state = {k: v for k, v in fake_gaggimate.state.items() if k != "sys"}
    await setup_integration()
    assert _eid(hass, "sensor", "system_state") is None
    assert _eid(hass, "binary_sensor", "controller_error") is None


# -- Controller Error (binary_sensor PROBLEM, diagnostic) --------------------------------------------


@pytest.mark.parametrize(
    ("sys_state", "expected"),
    [
        ({"s": "ready", "m": "", "c": 0}, "off"),
        ({"s": "starting", "m": "", "c": 0}, "off"),
        ({"s": "autotuning", "m": "", "c": 0}, "off"),
        ({"s": "ready", "m": "Sensor fault", "c": 3}, "on"),  # non-zero code alone turns it on
        ({"s": "error", "m": "", "c": 0}, "on"),
        ({"s": "mismatch", "m": "", "c": 0}, "on"),
    ],
)
async def test_controller_error_state(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    sys_state: dict[str, Any],
    expected: str,
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, sys=sys_state)
    entity = _state(hass, "binary_sensor", "controller_error")
    assert entity.state == expected
    assert entity.attributes.get("device_class") == "problem"


async def test_controller_error_unknown_when_sys_null(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, sys=None)
    assert _state(hass, "binary_sensor", "controller_error").state == "unknown"


# -- Controller Link / RSSI / Latency / Retransmits ---------------------------------------------------


@pytest.fixture
def enabled_diagnostics(mock_config_entry: MockConfigEntry, hass: HomeAssistant) -> None:
    """Pre-register the disabled-by-default link entities as enabled, before the platform sets them up.

    Existing registry entries keep their disabled_by value, so these entities are created enabled and
    get a state. This avoids a config entry reload, which HA schedules after a registry change.
    """
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for key in ("link_rssi", "link_latency", "link_retransmits"):
        registry.async_get_or_create(
            "sensor", "gaggimate", f"{HOST}_{key}", config_entry=mock_config_entry, disabled_by=None
        )


@pytest.mark.parametrize("link", LINK_TYPES)
async def test_link_type_values(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, link: str
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, lnk=link)
    state = _state(hass, "sensor", "link_type")
    assert state.state == link
    assert state.attributes["options"] == LINK_TYPES


async def test_link_type_invalid_is_unknown(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, lnk="wifi")
    assert _state(hass, "sensor", "link_type").state == "unknown"
    assert _own_warnings(caplog) == []


@pytest.mark.parametrize(
    ("lnk", "rssi", "expected"),
    [
        ("ble", -61, "-61"),
        ("ble", -72.4, "-72"),  # rounded to an integer
        ("ble", 0, "unknown"),  # firmware reports 0 when there is no RSSI reading
        ("uart", -61, "unknown"),
        ("none", -61, "unknown"),
        ("uart", 0, "unknown"),
    ],
)
async def test_link_rssi_values(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_diagnostics: None,
    setup_integration: SetupIntegration,
    lnk: str,
    rssi: float,
    expected: str,
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, lnk=lnk, rssi=rssi)
    state = _state(hass, "sensor", "link_rssi")
    assert state.state == expected
    if expected != "unknown":
        assert state.attributes["unit_of_measurement"] == "dBm"


@pytest.mark.parametrize(
    ("lat", "expected"),
    [(-1, "unknown"), (0, "0"), (42, "42"), (17.6, "18")],
)
async def test_link_latency_values(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_diagnostics: None,
    setup_integration: SetupIntegration,
    lat: float,
    expected: str,
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, lat=lat)
    state = _state(hass, "sensor", "link_latency")
    assert state.state == expected
    if expected != "unknown":
        assert state.attributes["unit_of_measurement"] == "ms"


@pytest.mark.parametrize(("rtx", "expected"), [(0, "0"), (7, "7"), (12.0, "12"), (None, "unknown")])
async def test_link_retransmits_values(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_diagnostics: None,
    setup_integration: SetupIntegration,
    rtx: Any,
    expected: str,
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, rtx=rtx)
    assert _state(hass, "sensor", "link_retransmits").state == expected


async def test_disabled_by_default_link_entities_are_registered_disabled(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    for key in ("link_rssi", "link_latency", "link_retransmits"):
        entry = _registry_entry(hass, "sensor", key)
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        # a disabled entity has no state object
        assert hass.states.get(entry.entity_id) is None

    for domain, key in (
        ("sensor", "link_type"),
        ("sensor", "system_state"),
        ("sensor", "scale_battery"),
        ("sensor", "scale_source"),
        ("binary_sensor", "update_available"),
        ("binary_sensor", "controller_error"),
    ):
        entry = _registry_entry(hass, domain, key)
        assert entry.disabled_by is None, key
        assert hass.states.get(entry.entity_id) is not None, key


async def test_diagnostic_entity_categories(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    await setup_integration()
    for domain, key in (
        ("binary_sensor", "controller_error"),
        ("sensor", "link_type"),
        ("sensor", "link_rssi"),
        ("sensor", "link_latency"),
        ("sensor", "link_retransmits"),
        ("binary_sensor", "update_available"),
        ("sensor", "scale_battery"),
        ("sensor", "scale_source"),
    ):
        assert _registry_entry(hass, domain, key).entity_category == "diagnostic", key


# -- Update Available (binary_sensor UPDATE) ---------------------------------------------------------


@pytest.mark.parametrize(("up", "expected"), [(True, "on"), (False, "off"), (None, "unknown")])
async def test_update_available_values(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    up: bool | None,
    expected: str,
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, up=up)
    state = _state(hass, "binary_sensor", "update_available")
    assert state.state == expected
    assert state.attributes.get("device_class") == "update"


# -- Scale Battery and Active Scale Source -----------------------------------------------------------


async def test_scale_battery_integer_and_null(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    assert _state(hass, "sensor", "scale_battery").state == "unknown"  # sbat is null in the default frame

    await _push_and_wait(fake_gaggimate, coordinator, sbat=87)
    state = _state(hass, "sensor", "scale_battery")
    assert state.state == "87"
    assert state.attributes["unit_of_measurement"] == "%"
    assert state.attributes["device_class"] == "battery"

    await _push_and_wait(fake_gaggimate, coordinator, sbat=None)
    assert _state(hass, "sensor", "scale_battery").state == "unknown"


@pytest.mark.parametrize(("source", "expected"), [(s, s) for s in SCALE_SOURCES] + [("nfc", "unknown")])
async def test_scale_source_values(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
    source: str,
    expected: str,
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, scaleSource=source)
    state = _state(hass, "sensor", "scale_source")
    assert state.state == expected
    assert state.attributes["options"] == SCALE_SOURCES
    assert _own_warnings(caplog) == []


# -- Backflush Due / Descaling Due (warning-style sensors) ------------------------------------------


@pytest.mark.frames("dual_boiler_idle")
async def test_backflush_and_descaling_inactive_then_active(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    for key in ("backflush", "descaling"):
        state = _state(hass, "sensor", f"warning_{key}")
        assert state.state == "Inactive"
        assert state.attributes["severity"] == "Warning"
        assert state.attributes["severity_level"] == 1

    warn = [{**w, "l": 2, "a": True} if w["k"] == "backflush" else dict(w) for w in fake_gaggimate.state["warn"]]
    await _push_and_wait(fake_gaggimate, coordinator, warn=warn)
    state = _state(hass, "sensor", "warning_backflush")
    assert state.state == "Active"
    assert state.attributes["severity"] == "Error"
    assert state.attributes["severity_level"] == 2
    assert _state(hass, "sensor", "warning_descaling").state == "Inactive"


async def test_backflush_and_descaling_absent_then_appear_late(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """Warning sensors are gated on the key being in `warn`; a later full frame adds them."""
    # waiting_controller: the controller has not connected yet; warn has no backflush/descaling entry
    fake_gaggimate.state = load_json("waiting_controller")["state"]
    await setup_integration()
    assert _eid(hass, "sensor", "warning_backflush") is None
    assert _eid(hass, "sensor", "warning_descaling") is None

    # the fuller slow-state frame that follows once the controller is up
    full = load_json("single_boiler_idle")["state"]
    full_warn = full["warn"] + [_warn_entry("backflush", 2, True), _warn_entry("descaling", 1, False)]
    await _push_full(fake_gaggimate, {**full, "warn": full_warn})

    await wait_until(lambda: _eid(hass, "sensor", "warning_backflush") is not None)
    await wait_until(lambda: _eid(hass, "sensor", "warning_descaling") is not None)
    await settle(0.05)
    assert _state(hass, "sensor", "warning_backflush").state == "Active"
    assert _state(hass, "sensor", "warning_backflush").attributes["severity"] == "Error"
    assert _state(hass, "sensor", "warning_descaling").state == "Inactive"


async def test_warning_sensor_unknown_after_key_leaves_warn(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """Add-only gating: once created, the entity stays; its value is unknown while the key is absent."""
    fake_gaggimate.state = load_json("dual_boiler_idle")["state"]
    coordinator = await setup_integration()
    assert _state(hass, "sensor", "warning_backflush").state == "Inactive"

    without = [w for w in fake_gaggimate.state["warn"] if w["k"] != "backflush"]
    await _push_and_wait(fake_gaggimate, coordinator, warn=without)
    assert _eid(hass, "sensor", "warning_backflush") is not None
    assert _state(hass, "sensor", "warning_backflush").state == "unknown"


# -- OTA refresh on `up` changes (coordinator) -------------------------------------------------------


@pytest.fixture
def short_ota_delay() -> Any:
    with patch.object(coordinator_module, "OTA_REFRESH_DELAY", 0.3):
        yield


async def test_ota_no_refresh_on_first_frame(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, short_ota_delay: None
) -> None:
    """The connect frames carry `up` but there is no previous value: only the connect request is sent."""
    await setup_integration()
    assert _ota_requests(fake_gaggimate) == 1
    await asyncio.sleep(0.8)
    assert _ota_requests(fake_gaggimate) == 1


async def test_ota_no_refresh_when_up_absent(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, short_ota_delay: None
) -> None:
    fake_gaggimate.state = {k: v for k, v in fake_gaggimate.state.items() if k != "up"}
    coordinator = await setup_integration()
    await fake_gaggimate.push({"tp": "evt:status", "ct": 90.0})
    await _push_and_wait(fake_gaggimate, coordinator, sbat=50)
    await settle(0.8)
    assert _ota_requests(fake_gaggimate) == 1  # only the connect request


async def test_ota_up_toggle_triggers_exactly_one_refresh(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, short_ota_delay: None
) -> None:
    coordinator = await setup_integration()
    assert _ota_requests(fake_gaggimate) == 1
    fake_gaggimate.clear_requests()

    # three flips inside the 0.3 s debounce window
    await fake_gaggimate.push_state(up=True)
    await settle(0.02)
    await fake_gaggimate.push_state(up=False)
    await settle(0.02)
    await fake_gaggimate.push_state(up=True)

    await fake_gaggimate.wait_for_request("req:ota-settings")
    await settle(0.8)
    assert _ota_requests(fake_gaggimate) == 1
    assert coordinator.data["up"] is True


async def test_ota_single_toggle_then_later_toggle_refreshes_again(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, short_ota_delay: None
) -> None:
    await setup_integration()
    fake_gaggimate.clear_requests()

    await fake_gaggimate.push_state(up=True)
    await fake_gaggimate.wait_for_request("req:ota-settings")
    await settle(0.5)
    assert _ota_requests(fake_gaggimate) == 1

    await fake_gaggimate.push_state(up=False)
    await fake_gaggimate.wait_for_request("req:ota-settings", count=2)
    await settle(0.5)
    assert _ota_requests(fake_gaggimate) == 2


async def test_ota_refresh_reaches_update_sensors(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, short_ota_delay: None
) -> None:
    """After an `up` change the refreshed OTA answer reaches the update sensors (no stale values)."""
    coordinator = await setup_integration()
    assert _state(hass, "sensor", "display_software_update").state == "False"

    fake_gaggimate.ota_settings = {**fake_gaggimate.ota_settings, "displayUpdateAvailable": True}
    await _push_and_wait(fake_gaggimate, coordinator, up=True)
    await wait_until(lambda: _state(hass, "sensor", "display_software_update").state == "True")


async def test_shutdown_during_debounce_leaves_no_task(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
) -> None:
    with patch.object(coordinator_module, "OTA_REFRESH_DELAY", 5):
        coordinator: GaggiMateCoordinator = await setup_integration()
        fake_gaggimate.clear_requests()

        await fake_gaggimate.push_state(up=True)
        await wait_until(lambda: coordinator._ota_refresh_task is not None and not coordinator._ota_refresh_task.done())
        task = coordinator._ota_refresh_task
        assert task is not None

        await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert task.done()
    assert coordinator._ota_refresh_task is None
    assert _ota_requests(fake_gaggimate) == 0
    await settle(0.2)
    assert _ota_requests(fake_gaggimate) == 0
