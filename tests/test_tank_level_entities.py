"""Step 8: Tank Level Low and Steam Boiler Low binary sensors.

Expectations follow the specification (plan.md, Step 8, and the firmware semantics recorded there),
not the implementation:

* Tank Level Low (problem): created only when ``htl`` is True and ``tlo`` is not null. On when
  ``tlo`` is False, off when ``tlo`` is True, unknown when ``tlo`` is null. Add-only.
* Steam Boiler Low (problem): created only when ``hsl`` is True and ``sbl`` is not null. On when
  ``sbl`` is True, off when ``sbl`` is False, unknown when null. Add-only.
* The existing ``Tank level``, ``ToF Distance`` and ``Water Tank Low`` sensors are unaffected.

Frames: the synthetic fixtures ``single_boiler_tof``, ``tank_switch`` and ``steam_level`` (tests/fixtures/frames),
the unchanged ``tof_tank`` fixture (htl true, tlo null) and ``dual_boiler_idle``, or inline edits of them.
"""
from __future__ import annotations

import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gaggimate.const import DOMAIN
from custom_components.gaggimate.coordinator import GaggiMateCoordinator

from .conftest import SetupIntegration, wait_until
from .fake_server import FakeGaggiMate, load_json

HOST = "127.0.0.1"
TANK = "tank_level_low"
STEAM = "steam_boiler_low"
# Loggers whose WARNING-or-worse records would mean a real problem (HA state/enum complaints or integration errors).
OWN_LOGGERS = (
    "custom_components.gaggimate",
    "homeassistant.components.binary_sensor",
    "homeassistant.helpers.entity",
)


# -- fixtures and helpers ----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_keepalive(fake_gaggimate: FakeGaggiMate) -> None:
    """Every pushed frame then causes exactly one coordinator update (the tests are far below the 15 s timeout)."""
    fake_gaggimate.keepalive = False


def _use_frames(
    fake: FakeGaggiMate,
    name: str,
    state: dict[str, Any] | None = None,
    telemetry: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
) -> None:
    """Replace the frames the fake server sends on connect, with optional overrides and dropped keys."""
    data = load_json(name)
    slow = {**data["state"], **(state or {})}
    for key in drop:
        slow.pop(key, None)
    fake.state = slow
    fake.telemetry = {**data["telemetry"], **(telemetry or {})}


def _eid(hass: HomeAssistant, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("binary_sensor", "gaggimate", f"{HOST}_{key}")


def _registered(hass: HomeAssistant, key: str) -> bool:
    return _eid(hass, key) is not None


def _state(hass: HomeAssistant, key: str) -> str:
    entity_id = _eid(hass, key)
    assert entity_id is not None, f"binary_sensor {key} not registered"
    state = hass.states.get(entity_id)
    assert state is not None, f"binary_sensor {key} registered but has no state"
    return state.state


def _registry_entry(hass: HomeAssistant, key: str) -> er.RegistryEntry:
    entity_id = _eid(hass, key)
    assert entity_id is not None, f"binary_sensor {key} not registered"
    entry = er.async_get(hass).async_get(entity_id)
    assert entry is not None
    return entry


def _entry_unique_ids(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    return [e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)]


def _own_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        f"{r.name}: {r.getMessage()}"
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith(OWN_LOGGERS)
    ]


async def _push(
    hass: HomeAssistant,
    fake: FakeGaggiMate,
    coordinator: GaggiMateCoordinator,
    *,
    telemetry: bool = False,
    **changes: Any,
) -> None:
    """Push a partial frame and block until the coordinator processed it and HA finished the entity work.

    Counting coordinator updates (keepalive is off) is deterministic even when the value is unchanged
    or null, which waiting on the merged value alone is not.
    """
    updates = 0

    def _count() -> None:
        nonlocal updates
        updates += 1

    unsubscribe = coordinator.async_add_listener(_count)
    try:
        if telemetry:
            await fake.push_telemetry(**changes)
        else:
            await fake.push_state(**changes)
        await wait_until(lambda: updates >= 1)
    finally:
        unsubscribe()
    await hass.async_block_till_done()


# -- Tank Level Low: absence -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frames", "state", "drop"),
    [
        # single-boiler machine without a tank switch (htl false, tlo null)
        ("single_boiler_idle", {}, ()),
        # single-boiler ToF machine: htl false, so no Tank Level Low even though tofc and wl are set
        ("single_boiler_tof", {}, ()),
        # existing fixture (htl true, tlo null): no reading yet, so no entity
        ("tof_tank", {}, ()),
        # tank switch with the reading still null
        ("tank_switch", {"tlo": None}, ()),
        # htl null is not a capability (gate needs exactly True)
        ("tank_switch", {"htl": None}, ()),
        # htl false while a reading exists
        ("tank_switch", {"htl": False}, ()),
        # htl true but the tlo key is absent altogether
        ("tank_switch", {}, ("tlo",)),
    ],
)
async def test_tank_level_low_absent(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    frames: str,
    state: dict[str, Any],
    drop: tuple[str, ...],
) -> None:
    _use_frames(fake_gaggimate, frames, state=state, drop=drop)
    await setup_integration()
    assert not _registered(hass, TANK)
    assert hass.states.get("binary_sensor.gaggimate_test_tank_level_low") is None


async def test_single_boiler_tof_keeps_existing_tank_sensors(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """On a ToF single boiler the Tank level, ToF Distance and Water Tank Low sensors are unchanged."""
    _use_frames(fake_gaggimate, "single_boiler_tof")
    await setup_integration()

    assert not _registered(hass, TANK)
    assert float(hass.states.get("sensor.gaggimate_test_tank_level").state) == 64
    assert float(hass.states.get("sensor.gaggimate_test_tof_distance").state) == 87
    assert hass.states.get("sensor.gaggimate_test_water_tank_low").state == "Inactive"


# -- Tank Level Low: presence, device class, inversion, null --------------------------------------------


async def test_tank_switch_creates_enabled_problem_sensor(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, "tank_switch")
    await setup_integration()

    assert _eid(hass, TANK) == "binary_sensor.gaggimate_test_tank_level_low"
    entry = _registry_entry(hass, TANK)
    assert entry.unique_id == f"{HOST}_tank_level_low"
    assert entry.platform == DOMAIN
    assert entry.original_device_class == "problem"
    assert entry.disabled_by is None  # enabled by default
    assert _state(hass, TANK) == "off"  # tlo True means tank OK


async def test_tank_level_low_is_inverted_tlo(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, "tank_switch")
    coordinator = await setup_integration()

    await _push(hass, fake_gaggimate, coordinator, tlo=False)
    assert _state(hass, TANK) == "on"
    await _push(hass, fake_gaggimate, coordinator, tlo=True)
    assert _state(hass, TANK) == "off"


async def test_tank_level_low_null_is_unknown_and_entity_stays(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, "tank_switch")
    coordinator = await setup_integration()
    entity_id = _eid(hass, TANK)

    await _push(hass, fake_gaggimate, coordinator, tlo=None)
    assert _state(hass, TANK) == "unknown"
    assert _eid(hass, TANK) == entity_id  # not removed when the reading goes null

    await _push(hass, fake_gaggimate, coordinator, tlo=False)
    assert _state(hass, TANK) == "on"


async def test_tank_level_low_late_gating_when_tlo_arrives(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """htl true with tlo null at start: no entity; it appears when the reading arrives."""
    _use_frames(fake_gaggimate, "tank_switch", state={"tlo": None})
    coordinator = await setup_integration()
    assert not _registered(hass, TANK)

    await _push(hass, fake_gaggimate, coordinator, tlo=False)
    assert _registered(hass, TANK)
    assert _state(hass, TANK) == "on"


async def test_tank_level_low_late_gating_on_existing_tof_tank_fixture(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """The unchanged tof_tank fixture (htl true, tlo null) gains the entity once tlo becomes true."""
    _use_frames(fake_gaggimate, "tof_tank")
    coordinator = await setup_integration()
    assert not _registered(hass, TANK)

    await _push(hass, fake_gaggimate, coordinator, tlo=True)
    assert _registered(hass, TANK)
    assert _state(hass, TANK) == "off"


async def test_tank_level_low_late_gating_when_htl_flips_true(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """htl alone is not enough: the entity waits for htl AND a non-null tlo."""
    _use_frames(fake_gaggimate, "single_boiler_tof")
    coordinator = await setup_integration()

    await _push(hass, fake_gaggimate, coordinator, htl=True)
    assert not _registered(hass, TANK)  # tlo still null

    await _push(hass, fake_gaggimate, coordinator, tlo=True)
    assert _registered(hass, TANK)
    assert _state(hass, TANK) == "off"


async def test_tank_level_low_is_add_only(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """Once created the entity is not removed when htl flips false again (add-only, avoids churn)."""
    _use_frames(fake_gaggimate, "tank_switch")
    coordinator = await setup_integration()
    entity_id = _eid(hass, TANK)

    await _push(hass, fake_gaggimate, coordinator, htl=False)
    assert _eid(hass, TANK) == entity_id
    assert _state(hass, TANK) == "off"  # the state reflects tlo, which is unchanged


async def test_tank_level_low_on_dual_boiler_with_htl_and_tlo(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """A dual boiler with a ToF tank sensor (htl true, tlo true) gets the entity."""
    _use_frames(fake_gaggimate, "dual_boiler_idle")
    await setup_integration()
    assert _state(hass, TANK) == "off"


# -- Steam Boiler Low ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frames", "state", "drop"),
    [
        ("single_boiler_idle", {}, ()),  # hsl false, sbl null
        ("steam_level", {"hsl": False, "sbl": False}, ()),  # hsl false while a reading exists
        ("steam_level", {"sbl": None}, ()),  # reading still null
        ("steam_level", {"hsl": None}, ()),  # hsl null is not a capability
        ("steam_level", {}, ("sbl",)),  # sbl key absent
    ],
)
async def test_steam_boiler_low_absent(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    frames: str,
    state: dict[str, Any],
    drop: tuple[str, ...],
) -> None:
    _use_frames(fake_gaggimate, frames, state=state, drop=drop)
    await setup_integration()
    assert not _registered(hass, STEAM)


async def test_steam_level_creates_enabled_problem_sensor(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, "steam_level")
    await setup_integration()

    assert _eid(hass, STEAM) == "binary_sensor.gaggimate_test_steam_boiler_low"
    entry = _registry_entry(hass, STEAM)
    assert entry.unique_id == f"{HOST}_steam_boiler_low"
    assert entry.platform == DOMAIN
    assert entry.original_device_class == "problem"
    assert entry.disabled_by is None
    assert _state(hass, STEAM) == "off"  # sbl False means level OK
    # the steam level gate is independent of the tank gate: htl false here, so no Tank Level Low
    assert not _registered(hass, TANK)


async def test_steam_boiler_low_is_inverted_sbl_and_null_unknown(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, "steam_level")
    coordinator = await setup_integration()
    entity_id = _eid(hass, STEAM)

    await _push(hass, fake_gaggimate, coordinator, sbl=True)
    assert _state(hass, STEAM) == "on"

    await _push(hass, fake_gaggimate, coordinator, sbl=None)
    assert _state(hass, STEAM) == "unknown"
    assert _eid(hass, STEAM) == entity_id

    await _push(hass, fake_gaggimate, coordinator, sbl=False)
    assert _state(hass, STEAM) == "off"


async def test_steam_boiler_low_late_gating_when_sbl_arrives(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, "steam_level", state={"sbl": None})
    coordinator = await setup_integration()
    assert not _registered(hass, STEAM)

    await _push(hass, fake_gaggimate, coordinator, sbl=False)
    assert _registered(hass, STEAM)
    assert _state(hass, STEAM) == "off"


async def test_steam_boiler_low_late_gating_when_hsl_flips_true(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, "steam_level", state={"hsl": False, "sbl": False})
    coordinator = await setup_integration()
    assert not _registered(hass, STEAM)

    await _push(hass, fake_gaggimate, coordinator, hsl=True)
    assert _registered(hass, STEAM)
    assert _state(hass, STEAM) == "off"


async def test_steam_boiler_low_is_add_only(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, "steam_level")
    coordinator = await setup_integration()
    entity_id = _eid(hass, STEAM)

    await _push(hass, fake_gaggimate, coordinator, hsl=False)
    assert _eid(hass, STEAM) == entity_id


async def test_steam_boiler_low_gate_is_hsl_not_db(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """The spec gates on hsl and a non-null sbl only; a single boiler with the sensor reports it too."""
    _use_frames(fake_gaggimate, "single_boiler_idle", state={"hsl": True, "sbl": False})
    await setup_integration()
    assert _state(hass, STEAM) == "off"


# -- existing sensors unaffected ----------------------------------------------------------------------


async def test_tank_switch_does_not_change_existing_tank_sensors(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """tlo drives only Tank Level Low; the Tank level, ToF Distance and Water Tank Low sensors stay as they were."""
    _use_frames(fake_gaggimate, "tank_switch")
    coordinator = await setup_integration()

    def existing() -> tuple[float, str, float]:
        return (
            float(hass.states.get("sensor.gaggimate_test_tank_level").state),
            hass.states.get("sensor.gaggimate_test_water_tank_low").state,
            float(hass.states.get("sensor.gaggimate_test_tof_distance").state),
        )

    before = existing()
    assert before == (100.0, "Inactive", 0.0)

    await _push(hass, fake_gaggimate, coordinator, tlo=False)
    assert _state(hass, TANK) == "on"
    assert existing() == before


async def test_steam_level_keeps_steam_temperature_and_tank_level(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, "steam_level")
    await setup_integration()
    assert hass.states.get("sensor.gaggimate_test_steam_temperature") is not None
    assert float(hass.states.get("sensor.gaggimate_test_tank_level").state) == 100


# -- reload, duplicates, warnings ---------------------------------------------------------------------


async def test_no_duplicates_after_reload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle")
    await setup_integration()
    before = sorted(_entry_unique_ids(hass, mock_config_entry))
    assert before.count(f"{HOST}_{TANK}") == 1
    assert before.count(f"{HOST}_{STEAM}") == 1

    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    coordinator: GaggiMateCoordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    await wait_until(lambda: coordinator.available and coordinator.data is not None)
    await hass.async_block_till_done()

    after = _entry_unique_ids(hass, mock_config_entry)
    assert len(after) == len(set(after)), "duplicate unique_ids after reload"
    assert sorted(after) == before
    assert _state(hass, TANK) == "off"
    assert _state(hass, STEAM) == "off"


async def test_late_created_entity_not_duplicated_after_reload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
) -> None:
    """Created late (tlo arrives after start), then reload: still exactly one entity, state restored."""
    _use_frames(fake_gaggimate, "tank_switch", state={"tlo": None})
    coordinator = await setup_integration()
    await _push(hass, fake_gaggimate, coordinator, tlo=False)
    assert _state(hass, TANK) == "on"

    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    await wait_until(lambda: coordinator.available and coordinator.data is not None)
    await hass.async_block_till_done()

    ids = _entry_unique_ids(hass, mock_config_entry)
    assert ids.count(f"{HOST}_{TANK}") == 1
    assert len(ids) == len(set(ids))
    assert _state(hass, TANK) == "on"


async def test_no_warning_logs_across_lifecycle(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Null to bool to null transitions, late gating and htl/hsl flips log nothing at WARNING or above."""
    _use_frames(fake_gaggimate, "single_boiler_tof", state={"tlo": None, "hsl": True, "sbl": None})
    coordinator = await setup_integration()

    await _push(hass, fake_gaggimate, coordinator, htl=True)
    await _push(hass, fake_gaggimate, coordinator, tlo=False)
    await _push(hass, fake_gaggimate, coordinator, tlo=None)
    await _push(hass, fake_gaggimate, coordinator, tlo=True)
    await _push(hass, fake_gaggimate, coordinator, sbl=True)
    await _push(hass, fake_gaggimate, coordinator, sbl=None)
    await _push(hass, fake_gaggimate, coordinator, sbl=False)
    await _push(hass, fake_gaggimate, coordinator, htl=False)

    assert _state(hass, TANK) == "off"
    assert _state(hass, STEAM) == "off"
    assert _own_warnings(caplog) == []
