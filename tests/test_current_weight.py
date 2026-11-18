"""Step 7: Current Weight follows the active scale source (decisions.md #8).

Expectations follow the specification (plan.md, Step 7), not the implementation:
* value = cw (1 dp) when scaleSource is hardware, bluetooth or estimation;
* value = unknown when scaleSource is inactive, null or an unknown value (for example "nfc");
* when the scaleSource key is absent (older firmware) the old rule applies: cw only when bc is true;
* attribute ``source`` is always present: the validated scaleSource, or None when absent, null or unknown;
* bc true with scaleSource inactive gives unknown (cw is a stale latch then);
* the entity is not gated: it exists on every fixture frame set and keeps unique_id <host>_current_weight.
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
KEY = "current_weight"
OWN_LOGGERS = ("custom_components.gaggimate", "homeassistant.components.sensor")
FRAME_SETS = (
    "single_boiler_idle",
    "dual_boiler_idle",
    "dual_boiler_brewing",
    "scale_connected",
    "tof_tank",
    "waiting_controller",
    "warnings_full",
)


# -- helpers -----------------------------------------------------------------------------------------


def _eid(hass: HomeAssistant, key: str = KEY) -> str | None:
    return er.async_get(hass).async_get_entity_id("sensor", "gaggimate", f"{HOST}_{key}")


def _state(hass: HomeAssistant) -> Any:
    """State object of the created and enabled current weight sensor; None when not created."""
    entity_id = _eid(hass)
    return hass.states.get(entity_id) if entity_id else None


def _value(hass: HomeAssistant) -> str:
    state = _state(hass)
    assert state is not None, "current_weight has no state"
    return state.state


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
def capture_gaggimate_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Capture DEBUG and up from the integration so any level of log output is visible to the tests."""
    caplog.set_level(logging.DEBUG, logger="custom_components.gaggimate")
    caplog.set_level(logging.DEBUG, logger="homeassistant.components.sensor")
    return caplog


# -- registry ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("frame", FRAME_SETS)
async def test_current_weight_exists_on_every_frame_set(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    mock_config_entry: MockConfigEntry,
    setup_integration: SetupIntegration,
    frame: str,
) -> None:
    """Not gated: created and enabled whatever the frame set says about the scale."""
    _prepare(fake_gaggimate, frame)
    await setup_integration()
    entry = er.async_get(hass).async_get(_eid(hass))
    assert entry is not None, frame
    assert entry.unique_id == f"{HOST}_current_weight"
    assert entry.domain == "sensor"
    assert entry.disabled_by is None, frame
    state = _state(hass)
    assert state is not None, frame
    assert state.attributes["unit_of_measurement"] == "g", frame
    assert state.attributes["state_class"] == "measurement", frame
    entry = er.async_get(hass).async_get(_eid(hass))
    assert entry.original_device_class == "weight", frame


@pytest.mark.frames("dual_boiler_brewing")
async def test_unit_and_unique_id_unchanged(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    await setup_integration()
    entry = er.async_get(hass).async_get(_eid(hass))
    assert entry is not None
    assert entry.unique_id == f"{HOST}_{KEY}"
    assert entry.original_device_class == "weight"
    assert _state(hass).attributes["unit_of_measurement"] == "g"


# -- value by scale source ---------------------------------------------------------------------------


@pytest.mark.frames("dual_boiler_brewing")
@pytest.mark.parametrize(
    ("source", "expected"),
    [("hardware", "18.2"), ("bluetooth", "18.2"), ("estimation", "18.2"), ("inactive", "unknown")],
)
async def test_value_follows_scale_source_with_bc_false(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    source: str,
    expected: str,
) -> None:
    """cw 18.2 with bc false: the scaleSource alone decides; ``source`` attribute always matches it."""
    _prepare(fake_gaggimate, "dual_boiler_brewing", state={"scaleSource": source, "bc": False})
    await setup_integration()
    assert _value(hass) == expected
    assert _state(hass).attributes["source"] == source


@pytest.mark.frames("dual_boiler_brewing")
async def test_bc_true_with_inactive_source_is_unknown(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """A stale cw latch must not show while no source is active, even if bc is still true."""
    _prepare(fake_gaggimate, "dual_boiler_brewing", state={"scaleSource": "inactive", "bc": True, "cw": 18.2})
    await setup_integration()
    assert _value(hass) == "unknown"
    assert _state(hass).attributes["source"] == "inactive"


@pytest.mark.frames("dual_boiler_brewing")
async def test_existing_brewing_fixture_is_unknown(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    """dual_boiler_brewing as shipped: scaleSource inactive, cw 18.2, bc false -> unknown."""
    await setup_integration()
    assert _value(hass) == "unknown"
    assert _state(hass).attributes["source"] == "inactive"


@pytest.mark.frames("scale_connected")
async def test_bluetooth_scale_fixture_reads_cw(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    await setup_integration()
    assert _value(hass) == "18.2"
    assert _state(hass).attributes["source"] == "bluetooth"


# -- older firmware without scaleSource --------------------------------------------------------------


@pytest.mark.frames("dual_boiler_brewing")
@pytest.mark.parametrize(("bc", "expected"), [(True, "18.2"), (False, "unknown")])
async def test_absent_scale_source_falls_back_to_bc(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    bc: bool,
    expected: str,
) -> None:
    """No scaleSource key at all: cw only when bc is true (the rule before Step 7)."""
    _prepare(fake_gaggimate, "dual_boiler_brewing", state={"bc": bc}, drop_state={"scaleSource"})
    await setup_integration()
    assert _value(hass) == expected
    attrs = _state(hass).attributes
    assert "source" in attrs
    assert attrs["source"] is None


# -- null and unknown source values ------------------------------------------------------------------


@pytest.mark.frames("dual_boiler_brewing")
@pytest.mark.parametrize("source", [None, "nfc"])
async def test_null_or_unknown_source_is_unknown_without_warnings(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    capture_gaggimate_logs: pytest.LogCaptureFixture,
    source: str | None,
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_brewing", state={"scaleSource": source, "bc": True})
    await setup_integration()
    assert _value(hass) == "unknown"
    assert _state(hass).attributes["source"] is None
    assert _own_warnings(capture_gaggimate_logs) == []


# -- cw null -----------------------------------------------------------------------------------------


@pytest.mark.frames("dual_boiler_brewing")
@pytest.mark.parametrize("source", ["hardware", "bluetooth", "estimation"])
async def test_null_cw_is_unknown_for_weight_sources(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, source: str
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_brewing", state={"scaleSource": source}, telemetry={"cw": None})
    await setup_integration()
    assert _value(hass) == "unknown"
    assert _state(hass).attributes["source"] == source


# -- frame merge -------------------------------------------------------------------------------------


@pytest.mark.frames("scale_connected")
async def test_bluetooth_then_inactive_frame_merge_goes_unknown(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """Slow state frames are merged onto the last status: the source change must reach the sensor."""
    coordinator = await setup_integration()
    assert _value(hass) == "18.2"

    await _push_and_wait(fake_gaggimate, coordinator, scaleSource="inactive")
    await wait_until(lambda: _value(hass) == "unknown")
    assert _state(hass).attributes["source"] == "inactive"

    await _push_and_wait(fake_gaggimate, coordinator, scaleSource="bluetooth")
    await wait_until(lambda: _value(hass) == "18.2")
    assert _state(hass).attributes["source"] == "bluetooth"


@pytest.mark.frames("dual_boiler_brewing")
async def test_later_null_source_frame_clears_weight(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """A later frame sending scaleSource null (merged as a cleared key) must give unknown, not the old weight."""
    coordinator = await setup_integration()
    await _push_and_wait(fake_gaggimate, coordinator, scaleSource="hardware")
    await wait_until(lambda: _value(hass) == "18.2")

    await _push_and_wait(fake_gaggimate, coordinator, scaleSource=None)
    await wait_until(lambda: _value(hass) == "unknown")
    assert _state(hass).attributes["source"] is None


# -- rounding ----------------------------------------------------------------------------------------


@pytest.mark.frames("dual_boiler_brewing")
@pytest.mark.parametrize(("cw", "expected"), [(12.345, "12.3"), (12.36, "12.4"), (0, "0.0"), (18.2, "18.2")])
async def test_weight_rounded_to_one_decimal(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    cw: float,
    expected: str,
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_brewing", state={"scaleSource": "hardware"}, telemetry={"cw": cw})
    await setup_integration()
    assert _value(hass) == expected
