"""Step 11: Grind and Grind By Weight switches.

Expectations follow the specification (plan.md, Step 11), not the implementation:
* Grind: shown from the integer flag gact (1/0, unknown when null); created when gact is present.
  turn_on sends exactly req:grind:activate; turn_off sends exactly req:grind:deactivate.
* Grind By Weight: shown from gt; created only while bta == 1 (add-only, also on late arrival).
  Sends req:change-grind-target with target as a real JSON integer.
* Commands are idempotent against the reported flag, raise when the state is unknown or the
  controller is disconnected; grind start also raises when the machine is not ready or another
  process (a brew) is running; grind stop never stops a running brew.
Frames are the synthetic fixtures in tests/fixtures/frames or inline edits of them.
"""
from __future__ import annotations

from collections.abc import Collection
import json
import logging
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import SetupIntegration, settle, wait_until
from .fake_server import FakeGaggiMate, load_json

HOST = "127.0.0.1"
OWN_LOGGERS = ("custom_components.gaggimate", "homeassistant.components.switch")

ACTIVATE = "req:grind:activate"
DEACTIVATE = "req:grind:deactivate"
TARGET = "req:change-grind-target"
GRIND_TPS = (ACTIVATE, DEACTIVATE, TARGET)

BREW = {"a": 1, "s": "brew", "l": "Extraction", "e": 12000, "u": 0, "tt": "time", "pt": 30000, "pp": 12000}
GRINDING = {"a": 1, "s": "grind", "l": "Grind", "e": 1000, "u": 0, "tt": "time", "pt": 25000, "pp": 1000}


# -- helpers -----------------------------------------------------------------------------------------


def _eid(hass: HomeAssistant, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("switch", "gaggimate", f"{HOST}_{key}")


def _state(hass: HomeAssistant, key: str) -> Any:
    entity_id = _eid(hass, key)
    return hass.states.get(entity_id) if entity_id else None


def _own_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        f"{r.name}: {r.getMessage()}"
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith(OWN_LOGGERS)
    ]


def _prepare(
    fake: FakeGaggiMate,
    frame: str = "dual_boiler_idle",
    *,
    state: dict[str, Any] | None = None,
    telemetry: dict[str, Any] | None = None,
    drop_state: Collection[str] = (),
    drop_telemetry: Collection[str] = (),
) -> None:
    data = load_json(frame)
    merged_state = {**data["state"], **(state or {})}
    merged_telemetry = {**data["telemetry"], **(telemetry or {})}
    fake.state = {k: v for k, v in merged_state.items() if k not in drop_state}
    fake.telemetry = {k: v for k, v in merged_telemetry.items() if k not in drop_telemetry}


async def _push(fake: FakeGaggiMate, coordinator: Any, *, telemetry: bool = False, **changes: Any) -> None:
    """Push a partial frame and wait until the coordinator has merged it."""
    if telemetry:
        await fake.push_telemetry(**changes)
    else:
        await fake.push_state(**changes)
    await wait_until(lambda: all(k in coordinator.data and coordinator.data[k] == v for k, v in changes.items()))
    await settle(0.02)


async def _call(hass: HomeAssistant, service: str, key: str) -> None:
    await hass.services.async_call("switch", service, {"entity_id": _eid(hass, key)}, blocking=True)


def _sent(fake: FakeGaggiMate) -> list[dict[str, Any]]:
    return [r for r in fake.requests if r.get("tp") in GRIND_TPS]


async def _expect_refused(hass: HomeAssistant, fake: FakeGaggiMate, service: str, key: str) -> None:
    fake.clear_requests()
    with pytest.raises(HomeAssistantError):
        await _call(hass, service, key)
    await settle()
    assert _sent(fake) == []


# -- creation and gating -----------------------------------------------------------------------------


@pytest.mark.frames("dual_boiler_idle")
async def test_both_switches_created_with_names_and_unique_ids(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    registry = er.async_get(hass)
    for key, name in (("grind", "Grind"), ("grind_by_weight", "Grind By Weight")):
        entity_id = _eid(hass, key)
        assert entity_id is not None, key
        entry = registry.async_get(entity_id)
        assert entry.unique_id == f"{HOST}_{key}"
        assert entry.domain == "switch"
        assert entry.disabled_by is None
        assert hass.states.get(entity_id).attributes["friendly_name"].endswith(name)


async def test_grind_by_weight_absent_when_bta_is_zero(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    # default single_boiler_idle: gact present, bta 0
    await setup_integration()
    assert _state(hass, "grind") is not None
    assert _eid(hass, "grind_by_weight") is None


async def test_both_absent_without_gact_and_bta(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(fake_gaggimate, "single_boiler_idle", drop_state=("gact", "bta", "gt"))
    await setup_integration()
    assert _eid(hass, "grind") is None
    assert _eid(hass, "grind_by_weight") is None
    assert _eid(hass, "power") is not None  # existing switches unchanged


@pytest.mark.parametrize("bta", [0, None, "1", 2])
async def test_grind_by_weight_requires_int_one(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, bta: Any
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle", state={"bta": bta})
    await setup_integration()
    # Only the integer 1 counts.
    assert _eid(hass, "grind_by_weight") is None


async def test_grind_added_when_gact_arrives_late(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle", drop_state=("gact",))
    coordinator = await setup_integration()
    assert _eid(hass, "grind") is None
    await _push(fake_gaggimate, coordinator, gact=0)
    await wait_until(lambda: _state(hass, "grind") is not None)
    assert _state(hass, "grind").state == "off"
    assert _eid(hass, "grind_by_weight") is not None


async def test_grind_by_weight_added_when_bta_arrives_late_and_stays(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle", state={"bta": 0})
    coordinator = await setup_integration()
    assert _eid(hass, "grind_by_weight") is None
    await _push(fake_gaggimate, coordinator, bta=1)
    await wait_until(lambda: _state(hass, "grind_by_weight") is not None)
    # add-only: bta dropping back does not remove it
    await _push(fake_gaggimate, coordinator, bta=0)
    assert _state(hass, "grind_by_weight") is not None
    assert _state(hass, "grind_by_weight").state != "unavailable"


# -- state -------------------------------------------------------------------------------------------


async def test_states_follow_frames(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle")
    coordinator = await setup_integration()
    assert _state(hass, "grind").state == "off"
    assert _state(hass, "grind_by_weight").state == "off"
    await _push(fake_gaggimate, coordinator, gact=1)
    assert _state(hass, "grind").state == "on"
    assert _state(hass, "grind_by_weight").state == "off"
    await _push(fake_gaggimate, coordinator, gt=1)
    assert _state(hass, "grind_by_weight").state == "on"
    await _push(fake_gaggimate, coordinator, gact=0, gt=0)
    assert _state(hass, "grind").state == "off"
    assert _state(hass, "grind_by_weight").state == "off"


async def test_initial_on_states(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle", state={"gact": 1, "gt": 1}, telemetry={"process": GRINDING})
    await setup_integration()
    assert _state(hass, "grind").state == "on"
    assert _state(hass, "grind_by_weight").state == "on"


async def test_null_gact_is_unknown_state(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()  # single boiler: grind only
    await _push(fake_gaggimate, coordinator, gact=None)
    assert _state(hass, "grind").state == "unknown"


async def test_null_gt_is_unknown_state(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle")
    coordinator = await setup_integration()
    await _push(fake_gaggimate, coordinator, gt=None)
    assert _state(hass, "grind_by_weight").state == "unknown"


# -- grind turn_on -----------------------------------------------------------------------------------


async def test_grind_turn_on_sends_exact_activate_and_no_warnings(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle")
    await setup_integration()
    fake_gaggimate.clear_requests()
    caplog.clear()
    await _call(hass, "turn_on", "grind")
    await fake_gaggimate.wait_for_request(ACTIVATE)
    await settle()
    assert _sent(fake_gaggimate) == [{"tp": ACTIVATE}]
    # the switch does not flip optimistically; it follows the frames
    assert _state(hass, "grind").state == "off"
    assert _own_warnings(caplog) == []


async def test_grind_turn_on_idempotent_when_already_grinding(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle", state={"gact": 1}, telemetry={"process": GRINDING})
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _call(hass, "turn_on", "grind")
    await settle()
    assert _sent(fake_gaggimate) == []


async def test_grind_turn_on_raises_when_state_unknown(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await _push(fake_gaggimate, coordinator, gact=None)
    await _expect_refused(hass, fake_gaggimate, "turn_on", "grind")


@pytest.mark.parametrize("sys_state", ["error", "init", "booting"])
async def test_grind_turn_on_raises_when_not_ready(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, sys_state: str
) -> None:
    coordinator = await setup_integration()
    await _push(fake_gaggimate, coordinator, sys={"s": sys_state, "m": "", "c": 0})
    await _expect_refused(hass, fake_gaggimate, "turn_on", "grind")


async def test_grind_turn_on_allowed_when_sys_absent(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(fake_gaggimate, "single_boiler_idle", drop_state=("sys",))
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _call(hass, "turn_on", "grind")
    await fake_gaggimate.wait_for_request(ACTIVATE)
    assert _sent(fake_gaggimate) == [{"tp": ACTIVATE}]


@pytest.mark.parametrize("sys_value", [None, {"s": None, "m": "", "c": 0}])
async def test_grind_turn_on_allowed_when_sys_null_or_state_null(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, sys_value: Any
) -> None:
    _prepare(fake_gaggimate, "single_boiler_idle", state={"sys": sys_value})
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _call(hass, "turn_on", "grind")
    await fake_gaggimate.wait_for_request(ACTIVATE)
    assert _sent(fake_gaggimate) == [{"tp": ACTIVATE}]


async def test_grind_turn_on_raises_during_brew(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle", telemetry={"process": BREW})
    await setup_integration()
    await _expect_refused(hass, fake_gaggimate, "turn_on", "grind")


async def test_grind_turn_on_raises_when_running_process_has_no_state(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle", telemetry={"process": {"a": 1}})
    await setup_integration()
    await _expect_refused(hass, fake_gaggimate, "turn_on", "grind")


async def test_grind_turn_on_allowed_when_process_is_the_grind_itself(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    # gact still 0 (frame lag) while the process already reports the grind
    _prepare(fake_gaggimate, "dual_boiler_idle", telemetry={"process": GRINDING})
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _call(hass, "turn_on", "grind")
    await fake_gaggimate.wait_for_request(ACTIVATE)
    assert _sent(fake_gaggimate) == [{"tp": ACTIVATE}]


@pytest.mark.parametrize("process", [None, {"a": 0, "s": "brew"}, {"a": 0}])
async def test_grind_turn_on_allowed_with_idle_or_finished_process(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, process: Any
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle", telemetry={"process": process})
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _call(hass, "turn_on", "grind")
    await fake_gaggimate.wait_for_request(ACTIVATE)
    assert _sent(fake_gaggimate) == [{"tp": ACTIVATE}]


# -- grind turn_off ----------------------------------------------------------------------------------


async def test_grind_turn_off_sends_exact_deactivate(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle", state={"gact": 1}, telemetry={"process": GRINDING})
    await setup_integration()
    fake_gaggimate.clear_requests()
    caplog.clear()
    await _call(hass, "turn_off", "grind")
    await fake_gaggimate.wait_for_request(DEACTIVATE)
    await settle()
    assert _sent(fake_gaggimate) == [{"tp": DEACTIVATE}]
    assert _own_warnings(caplog) == []


async def test_grind_turn_off_sends_when_gact_1_and_process_missing(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle", state={"gact": 1}, telemetry={"process": None})
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _call(hass, "turn_off", "grind")
    await fake_gaggimate.wait_for_request(DEACTIVATE)
    assert _sent(fake_gaggimate) == [{"tp": DEACTIVATE}]


async def test_grind_turn_off_allowed_when_not_ready(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(
        fake_gaggimate,
        "dual_boiler_idle",
        state={"gact": 1, "sys": {"s": "error", "m": "x", "c": 1}},
        telemetry={"process": GRINDING},
    )
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _call(hass, "turn_off", "grind")
    await fake_gaggimate.wait_for_request(DEACTIVATE)
    assert _sent(fake_gaggimate) == [{"tp": DEACTIVATE}]


async def test_grind_turn_off_noop_when_gact_0(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle")
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _call(hass, "turn_off", "grind")  # must not raise
    await settle()
    assert _sent(fake_gaggimate) == []


async def test_grind_turn_off_noop_during_brew_never_stops_the_brew(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle", telemetry={"process": BREW})  # gact 0
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _call(hass, "turn_off", "grind")
    await settle()
    assert _sent(fake_gaggimate) == []


async def test_grind_turn_off_noop_when_gact_1_but_other_process_running(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    # stale gact 1 while a brew runs: the firmware's deactivate would stop the brew
    _prepare(fake_gaggimate, "dual_boiler_idle", state={"gact": 1}, telemetry={"process": BREW})
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _call(hass, "turn_off", "grind")
    await settle()
    assert _sent(fake_gaggimate) == []


async def test_grind_turn_off_noop_when_process_state_missing_and_gact_1(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle", state={"gact": 1}, telemetry={"process": {"a": 1}})
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _call(hass, "turn_off", "grind")
    await settle()
    assert _sent(fake_gaggimate) == []


async def test_grind_turn_off_raises_when_state_unknown(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await _push(fake_gaggimate, coordinator, gact=None)
    await _expect_refused(hass, fake_gaggimate, "turn_off", "grind")


# -- grind by weight ---------------------------------------------------------------------------------


async def test_grind_by_weight_turn_on_sends_integer_target_1(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle")  # gt 0
    await setup_integration()
    fake_gaggimate.clear_requests()
    caplog.clear()
    await _call(hass, "turn_on", "grind_by_weight")
    sent = await fake_gaggimate.wait_for_request(TARGET)
    await settle()
    assert _sent(fake_gaggimate) == [{"tp": TARGET, "target": 1}]
    assert type(sent[0]["target"]) is int  # not a bool: the firmware reads JSON integers only
    assert _own_warnings(caplog) == []


async def test_grind_by_weight_turn_off_sends_integer_target_0(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle", state={"gt": 1})
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _call(hass, "turn_off", "grind_by_weight")
    sent = await fake_gaggimate.wait_for_request(TARGET)
    assert _sent(fake_gaggimate) == [{"tp": TARGET, "target": 0}]
    assert type(sent[0]["target"]) is int


async def test_grind_by_weight_wire_format_is_json_integer(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """The raw JSON must read "target": 1, never true."""
    raw: list[str] = []
    _prepare(fake_gaggimate, "dual_boiler_idle")
    coordinator = await setup_integration()
    original = coordinator._ws.send_json

    async def _spy(data: Any, *args: Any, **kwargs: Any) -> None:
        raw.append(json.dumps(data))
        await original(data, *args, **kwargs)

    coordinator._ws.send_json = _spy
    try:
        await _call(hass, "turn_on", "grind_by_weight")
    finally:
        coordinator._ws.send_json = original
    assert len(raw) == 1
    assert '"target": 1' in raw[0] and "true" not in raw[0]


async def test_grind_by_weight_idempotent(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle", state={"gt": 1})
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    await _call(hass, "turn_on", "grind_by_weight")  # already on
    await _push(fake_gaggimate, coordinator, gt=0)
    await _call(hass, "turn_off", "grind_by_weight")  # already off
    await settle()
    assert _sent(fake_gaggimate) == []


@pytest.mark.parametrize("service", ["turn_on", "turn_off"])
async def test_grind_by_weight_raises_when_state_unknown(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, service: str
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle")
    coordinator = await setup_integration()
    await _push(fake_gaggimate, coordinator, gt=None)
    await _expect_refused(hass, fake_gaggimate, service, "grind_by_weight")


@pytest.mark.parametrize("service", ["turn_on", "turn_off"])
async def test_grind_by_weight_raises_when_gt_absent(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, service: str
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle", drop_state=("gt",))
    await setup_integration()
    assert _state(hass, "grind_by_weight").state == "unknown"
    await _expect_refused(hass, fake_gaggimate, service, "grind_by_weight")


# -- disconnected ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("service", "key", "state"),
    [
        ("turn_on", "grind", {"gact": 0}),
        ("turn_off", "grind", {"gact": 1}),
        ("turn_on", "grind_by_weight", {"gt": 0}),
        ("turn_off", "grind_by_weight", {"gt": 1}),
    ],
)
async def test_disconnected_raises_and_sends_nothing(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    service: str,
    key: str,
    state: dict[str, Any],
) -> None:
    _prepare(fake_gaggimate, "dual_boiler_idle", state=state)
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    with patch.object(coordinator, "_schedule_reconnect"):
        real_ws = coordinator._ws
        coordinator._ws = None
        try:
            with pytest.raises(HomeAssistantError, match="not connected"):
                await _call(hass, service, key)
        finally:
            coordinator._ws = real_ws
    await settle()
    assert _sent(fake_gaggimate) == []


# -- regression: existing switches -------------------------------------------------------------------


@pytest.mark.frames("dual_boiler_idle")
async def test_existing_switches_unchanged(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    assert _state(hass, "power") is not None
    assert _state(hass, "steam_heater") is not None
    keys = {
        e.unique_id.removeprefix(f"{HOST}_")
        for e in er.async_entries_for_config_entry(er.async_get(hass), mock_config_entry.entry_id)
        if e.domain == "switch"
    }
    assert keys == {"power", "steam_heater", "grind", "grind_by_weight"}
