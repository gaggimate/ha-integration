"""Step 6: heater and pump power, puck and flow, water pumped and steam temperature sensors.

Expectations follow the specification (plan.md, Step 6, 2a and 3d), not the implementation:
* heater_power: from hp, but 0 in standby (m == 0); pump_power: from pw.
* puck_resistance, puck_flow, water_pumped: need cp AND cd (the firmware computes them only then);
  puck_resistance is valid only for 0 < pkr < 100 and disabled by default.
* target_flow: needs cd; 0 means unknown; disabled by default.
* steam_temperature, target_steam_temperature: need db (dual boiler); tst 0 means unknown.
Frames are the synthetic fixtures in tests/fixtures/frames or inline edits of them.
"""
from __future__ import annotations

from collections.abc import Collection
import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import SetupIntegration, settle, wait_until
from .fake_server import FakeGaggiMate, load_json

HOST = "127.0.0.1"
OWN_LOGGERS = ("custom_components.gaggimate", "homeassistant.components.sensor")

NEW_KEYS = (
    "heater_power",
    "pump_power",
    "puck_resistance",
    "puck_flow",
    "target_flow",
    "water_pumped",
    "steam_temperature",
    "target_steam_temperature",
)
PUCK_FLOW_GATED = ("puck_resistance", "puck_flow", "water_pumped")  # cp AND cd
PROCESS_BREWING = {"a": 1, "s": "brew", "l": "Extraction", "e": 12000, "u": 0, "tt": "time", "pt": 30000, "pp": 12000}


# -- helpers -----------------------------------------------------------------------------------------


def _eid(hass: HomeAssistant, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("sensor", "gaggimate", f"{HOST}_{key}")


def _state(hass: HomeAssistant, key: str) -> Any:
    """State object of a created and enabled sensor; None when it is not created or disabled."""
    entity_id = _eid(hass, key)
    return hass.states.get(entity_id) if entity_id else None


def _value(hass: HomeAssistant, key: str) -> str:
    state = _state(hass, key)
    assert state is not None, f"sensor {key} has no state"
    return state.state


def _registry(hass: HomeAssistant, key: str) -> er.RegistryEntry | None:
    entity_id = _eid(hass, key)
    return er.async_get(hass).async_get(entity_id) if entity_id else None


def _own_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Warning-or-worse records from the integration or HA's sensor platform."""
    return [
        f"{r.name}: {r.getMessage()}"
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith(OWN_LOGGERS)
    ]


def _prepare(
    fake: FakeGaggiMate,
    frame: str,
    *,
    state: dict[str, Any] | None = None,
    telemetry: dict[str, Any] | None = None,
    drop_state: Collection[str] = (),
    drop_telemetry: Collection[str] = (),
) -> None:
    """Replace the frames the fake server sends on connect with ``frame`` plus inline changes."""
    data = load_json(frame)
    merged_state = {**data["state"], **(state or {})}
    merged_telemetry = {**data["telemetry"], **(telemetry or {})}
    fake.state = {k: v for k, v in merged_state.items() if k not in drop_state}
    fake.telemetry = {k: v for k, v in merged_telemetry.items() if k not in drop_telemetry}


async def _push_and_wait(
    fake: FakeGaggiMate,
    coordinator: Any,
    *,
    telemetry: bool = False,
    **changes: Any,
) -> None:
    """Push a partial frame and wait until the coordinator has merged it (the key must be present)."""
    if telemetry:
        await fake.push_telemetry(**changes)
    else:
        await fake.push_state(**changes)
    await wait_until(lambda: all(k in coordinator.data and coordinator.data[k] == v for k, v in changes.items()))
    await settle(0.02)


@pytest.fixture
def enabled_puck_sensors(mock_config_entry: MockConfigEntry, hass: HomeAssistant) -> None:
    """Pre-register the disabled-by-default puck and target flow sensors as enabled.

    Existing registry entries keep their disabled_by value, so these entities are created enabled
    and get a state (same approach as the link diagnostics in test_diagnostics_entities.py).
    """
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for key in ("puck_resistance", "target_flow"):
        registry.async_get_or_create(
            "sensor", "gaggimate", f"{HOST}_{key}", config_entry=mock_config_entry, disabled_by=None
        )


# -- registry ----------------------------------------------------------------------------------------


@pytest.mark.frames("dual_boiler_brewing")
async def test_only_puck_resistance_and_target_flow_disabled_by_default(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    for key in NEW_KEYS:
        entry = _registry(hass, key)
        assert entry is not None, f"sensor {key} not registered"
        assert entry.domain == "sensor"
        assert entry.unique_id == f"{HOST}_{key}"
        if key in ("puck_resistance", "target_flow"):
            assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key
            assert hass.states.get(entry.entity_id) is None, key
        else:
            assert entry.disabled_by is None, key
            assert hass.states.get(entry.entity_id) is not None, key


# -- Heater Power (hp, 0 in standby) -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hp", "expected"),
    [(0, "0"), (40, "40"), (40.4, "40"), (40.6, "41"), (100, "100")],
)
async def test_heater_power_follows_hp(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    hp: float,
    expected: str,
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, hp=hp)
    assert _value(hass, "heater_power") == expected


@pytest.mark.parametrize("hp", [50, 0, None])
async def test_heater_power_zero_in_standby_whatever_hp_says(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    hp: float | None,
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, m=0)  # MachineMode.STANDBY
    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, hp=hp)
    assert _value(hass, "heater_power") == "0"

    # leaving standby: the value follows hp again
    await _push_and_wait(fake_gaggimate, coordinator, m=1)
    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, hp=35)
    assert _value(hass, "heater_power") == "35"


async def test_heater_power_absent_until_hp_arrives(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """Late gating: no hp key in the frames -> no entity; the first frame with hp creates it."""
    _prepare(fake_gaggimate, "single_boiler_idle", drop_telemetry={"hp"})
    coordinator = await setup_integration()
    assert _state(hass, "heater_power") is None
    assert _registry(hass, "heater_power") is None

    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, hp=10)
    await wait_until(lambda: _state(hass, "heater_power") is not None)
    assert _value(hass, "heater_power") == "10"


async def test_heater_power_unit_and_state_class(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    await setup_integration()
    attrs = _state(hass, "heater_power").attributes
    assert attrs["unit_of_measurement"] == "%"
    assert attrs["state_class"] == "measurement"


# -- Pump Power (pw) ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("pw", "expected"), [(0, "0"), (73.6, "74"), (100, "100")])
async def test_pump_power_follows_pw(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    pw: float,
    expected: str,
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, pw=pw)
    assert _value(hass, "pump_power") == expected
    attrs = _state(hass, "pump_power").attributes
    assert attrs["unit_of_measurement"] == "%"
    assert attrs["state_class"] == "measurement"


async def test_pump_power_absent_until_pw_arrives(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(fake_gaggimate, "single_boiler_idle", drop_telemetry={"pw"})
    coordinator = await setup_integration()
    assert _state(hass, "pump_power") is None
    assert _registry(hass, "pump_power") is None

    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, pw=20)
    await wait_until(lambda: _state(hass, "pump_power") is not None)
    assert _value(hass, "pump_power") == "20"


# -- Puck Resistance (pkr, valid only in (0, 100), 2 dp, disabled by default) ----------------------------


@pytest.mark.frames("dual_boiler_idle")
@pytest.mark.parametrize(
    ("pkr", "expected"),
    [
        (0.01, "0.01"),
        (0.5, "0.5"),
        (1.8, "1.8"),
        (99.99, "99.99"),
        (0, "unknown"),  # the firmware sends 0 when there is no reading
        (100, "unknown"),
        (100.0, "unknown"),
        (150, "unknown"),
        (-3, "unknown"),
        (None, "unknown"),
    ],
)
async def test_puck_resistance_valid_only_between_0_and_100(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_puck_sensors: None,
    setup_integration: SetupIntegration,
    pkr: float | None,
    expected: str,
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, pkr=pkr)
    assert _value(hass, "puck_resistance") == expected
    if expected != "unknown":
        assert _state(hass, "puck_resistance").attributes["unit_of_measurement"] == "s*sqrt(bar)/mL"


@pytest.mark.frames("dual_boiler_idle")
async def test_puck_resistance_returns_to_unknown_when_out_of_range(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_puck_sensors: None,
    setup_integration: SetupIntegration,
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, pkr=1.8)
    assert _value(hass, "puck_resistance") == "1.8"
    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, pkr=150)
    assert _value(hass, "puck_resistance") == "unknown"
    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, pkr=2.5)
    assert _value(hass, "puck_resistance") == "2.5"


@pytest.mark.frames("dual_boiler_idle")
@pytest.mark.parametrize(("pkr", "expected"), [(1.236, "1.24"), (1.233, "1.23")])
async def test_puck_resistance_two_decimals(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_puck_sensors: None,
    setup_integration: SetupIntegration,
    pkr: float,
    expected: str,
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, pkr=pkr)
    assert _value(hass, "puck_resistance") == expected


@pytest.mark.parametrize(("cp", "cd"), [(True, True), (False, True), (True, False), (False, False)])
@pytest.mark.parametrize("key", PUCK_FLOW_GATED)
async def test_puck_flow_and_water_sensors_need_cp_and_cd(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_puck_sensors: None,
    setup_integration: SetupIntegration,
    key: str,
    cp: bool,
    cd: bool,
) -> None:
    """Puck resistance, puck flow and water pumped are created only when cp AND cd are both true.

    The enabled_puck_sensors fixture pre-registers puck_resistance, so the check is on the state object.
    """
    _prepare(fake_gaggimate, "dual_boiler_brewing", state={"cp": cp, "cd": cd})
    await setup_integration()
    present = cp and cd
    assert (_state(hass, key) is not None) is present, f"{key} with cp={cp} cd={cd}"


@pytest.mark.parametrize(
    ("cp", "cd", "present"),
    [(True, True, True), (False, True, True), (True, False, False), (False, False, False)],
)
async def test_target_flow_needs_cd_only(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_puck_sensors: None,
    setup_integration: SetupIntegration,
    cp: bool,
    cd: bool,
    present: bool,
) -> None:
    """Target flow is gated on cd alone: it exists with cp false when cd is true."""
    _prepare(fake_gaggimate, "dual_boiler_brewing", state={"cp": cp, "cd": cd})
    await setup_integration()
    assert (_state(hass, "target_flow") is not None) is present, f"cp={cp} cd={cd}"


async def test_puck_metrics_added_when_cp_arrives_late(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_puck_sensors: None,
    setup_integration: SetupIntegration,
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_brewing", state={"cp": False, "cd": True})
    coordinator = await setup_integration()
    for key in PUCK_FLOW_GATED:
        assert _state(hass, key) is None, key
    assert _value(hass, "target_flow") == "1.5"  # cd alone is enough

    await _push_and_wait(fake_gaggimate, coordinator, cp=True)
    await wait_until(lambda: all(_state(hass, key) is not None for key in PUCK_FLOW_GATED))
    assert _value(hass, "puck_resistance") == "1.8"
    assert _value(hass, "puck_flow") == "1.2"
    assert _value(hass, "water_pumped") == "14.2"


async def test_puck_metrics_added_when_cd_arrives_late(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_puck_sensors: None,
    setup_integration: SetupIntegration,
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_brewing", state={"cp": True, "cd": False})
    coordinator = await setup_integration()
    for key in (*PUCK_FLOW_GATED, "target_flow"):
        assert _state(hass, key) is None, key

    await _push_and_wait(fake_gaggimate, coordinator, cd=True)
    await wait_until(lambda: all(_state(hass, key) is not None for key in (*PUCK_FLOW_GATED, "target_flow")))
    assert _value(hass, "target_flow") == "1.5"
    assert _value(hass, "puck_flow") == "1.2"
    assert _value(hass, "puck_resistance") == "1.8"


# -- Puck Flow (pf, mL/s, cp AND cd) ---------------------------------------------------------------------


@pytest.mark.frames("dual_boiler_idle")
async def test_puck_flow_reads_zero_when_idle_then_follows_pf(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    assert float(_value(hass, "puck_flow")) == 0.0
    assert _state(hass, "puck_flow").attributes["unit_of_measurement"] == "mL/s"

    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, pf=2.36)
    assert _value(hass, "puck_flow") == "2.4"


# -- Target Flow (tf, mL/s, cd; 0 -> unknown; disabled by default) ---------------------------------------


@pytest.mark.frames("dual_boiler_idle")
async def test_target_flow_zero_is_unknown_and_follows_tf(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_puck_sensors: None,
    setup_integration: SetupIntegration,
) -> None:
    coordinator = await setup_integration()
    assert _value(hass, "target_flow") == "unknown"

    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, tf=1.5)
    assert _value(hass, "target_flow") == "1.5"
    assert _state(hass, "target_flow").attributes["unit_of_measurement"] == "mL/s"

    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, tf=0)
    assert _value(hass, "target_flow") == "unknown"


# -- Water Pumped (wp, mL, needs key wp) ------------------------------------------------------------------


@pytest.mark.frames("dual_boiler_idle")
async def test_water_pumped_appears_after_first_process_frame(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    assert _state(hass, "water_pumped") is None  # no wp key without a process
    assert _registry(hass, "water_pumped") is None

    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, process=PROCESS_BREWING, wp=0)
    await wait_until(lambda: _state(hass, "water_pumped") is not None)
    assert float(_value(hass, "water_pumped")) == 0.0

    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, wp=14.2)
    assert _value(hass, "water_pumped") == "14.2"


@pytest.mark.frames("dual_boiler_brewing")
async def test_water_pumped_keeps_last_value_when_wp_missing(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """Documented behaviour: wp is only sent while a process exists; the last value is kept after it ends."""
    coordinator = await setup_integration()
    assert _value(hass, "water_pumped") == "14.2"

    # process ended: no wp key, process null, pf/tf/pkr reset to 0 as the firmware does without a process
    await fake_gaggimate.push({"tp": "evt:status", "process": None, "pf": 0, "pkr": 0, "tf": 0})
    await wait_until(lambda: coordinator.data.get("pf") == 0)
    await settle(0.02)
    assert "wp" in coordinator.data
    assert _value(hass, "water_pumped") == "14.2"

    # the next process starts the counter again
    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, process=PROCESS_BREWING, wp=0)
    assert float(_value(hass, "water_pumped")) == 0.0


@pytest.mark.frames("dual_boiler_brewing")
async def test_water_pumped_unit_and_state_class(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    await setup_integration()
    attrs = _state(hass, "water_pumped").attributes
    assert attrs["unit_of_measurement"] == "mL"
    assert attrs["state_class"] == "measurement"


# -- Steam Temperature and Target Steam Temperature (db) --------------------------------------------------


async def test_steam_sensors_absent_on_single_boiler(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    await setup_integration()  # single_boiler_idle: db false
    for key in ("steam_temperature", "target_steam_temperature"):
        assert _state(hass, key) is None, key
        assert _registry(hass, key) is None, key


@pytest.mark.frames("dual_boiler_idle")
async def test_steam_sensors_present_on_dual_boiler(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    await setup_integration()
    steam = _state(hass, "steam_temperature")
    target = _state(hass, "target_steam_temperature")
    assert float(steam.state) == 125.3  # cst
    assert float(target.state) == 145.0  # tst
    for state in (steam, target):
        assert state.attributes["unit_of_measurement"] == "°C"
        assert state.attributes["device_class"] == "temperature"
        assert state.attributes["state_class"] == "measurement"


@pytest.mark.frames("dual_boiler_idle")
@pytest.mark.parametrize(("cst", "expected"), [(125.3, "125.3"), (93.44, "93.4"), (None, "unknown")])
async def test_steam_temperature_values(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    cst: float | None,
    expected: str,
) -> None:
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, cst=cst)
    assert _value(hass, "steam_temperature") == expected


@pytest.mark.frames("dual_boiler_idle")
@pytest.mark.parametrize(
    ("tst", "expected"),
    [(145, "145.0"), (93.44, "93.4"), (0, "unknown"), (None, "unknown")],
)
async def test_target_steam_temperature_values(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    tst: float | None,
    expected: str,
) -> None:
    """tst 0 means the steam heater is off: that is not a reading."""
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, tst=tst)
    assert _value(hass, "target_steam_temperature") == expected


async def test_steam_sensors_added_when_db_arrives_late(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()  # single boiler at first
    assert _state(hass, "steam_temperature") is None
    assert _state(hass, "target_steam_temperature") is None

    await _push_and_wait(fake_gaggimate, coordinator, telemetry=True, db=True, cst=125.3, tst=145)
    await wait_until(
        lambda: _state(hass, "steam_temperature") is not None and _state(hass, "target_steam_temperature") is not None
    )
    assert _value(hass, "steam_temperature") == "125.3"
    assert _value(hass, "target_steam_temperature") == "145.0"


# -- Whole set on one frame ------------------------------------------------------------------------------


@pytest.mark.frames("dual_boiler_brewing")
async def test_brewing_values_for_new_sensors_and_no_warnings(
    hass: HomeAssistant,
    setup_integration: SetupIntegration,
    enabled_puck_sensors: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_integration()
    expected = {
        "heater_power": "40",
        "pump_power": "100",
        "puck_resistance": "1.8",
        "puck_flow": "1.2",
        "target_flow": "1.5",
        "water_pumped": "14.2",
        "steam_temperature": "125.3",
        "target_steam_temperature": "145.0",
    }
    for key, value in expected.items():
        assert _value(hass, key) == value, key
    assert _own_warnings(caplog) == []
