"""Step 13: Backflush and Descaling cleaning buttons (Start and Mark Done).

Expectations follow the specification (plan.md, Step 13):

* four buttons, each created only when its warn key (``backflush`` / ``descaling``) is in ``warn``;
  add-only, and present whether or not a cleaning is due;
* the Start buttons are disabled by default in the entity registry, the Mark Done buttons are enabled;
* requests ``req:cleaning:<task>:<start|complete>`` carry a ``rid`` and the press waits for the
  ``res:cleaning:...`` answer;
* Start goes through the readiness guard (sys.s must be "ready" when sys is present and not null);
  ``success: false`` raises "Machine busy or profile unavailable" (Start) or "Could not mark cleaning
  as done" (Complete); a truthy ``error`` field raises with that text.
"""
from __future__ import annotations

import asyncio
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
OWN_LOGGERS = (
    "custom_components.gaggimate",
    "homeassistant.components.button",
    "homeassistant.helpers.entity",
)
EVT_BREW_CONFIRM = "evt:brew:confirm"
COORDINATOR_MODULE = "custom_components.gaggimate.coordinator"

# unique_id suffix -> (registry name, wire request)
CLEANING: dict[str, tuple[str, str]] = {
    "backflush_start": ("Start Backflush", "req:cleaning:backflush:start"),
    "backflush_complete": ("Mark Backflush Done", "req:cleaning:backflush:complete"),
    "descaling_start": ("Start Descaling", "req:cleaning:descaling:start"),
    "descaling_complete": ("Mark Descaling Done", "req:cleaning:descaling:complete"),
}
START_KEYS = ("backflush_start", "descaling_start")
COMPLETE_KEYS = ("backflush_complete", "descaling_complete")
ALL_KEYS = tuple(CLEANING)
EXISTING_BUTTON_KEYS = {
    "brew_start", "brew_stop", "flush", "flush_stop", "process_clear", "brew_confirm", "brew_confirm_cancel",
}


# -- helpers -----------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_keepalive(fake_gaggimate: FakeGaggiMate) -> None:
    fake_gaggimate.keepalive = False


@pytest.fixture
def enabled_start_buttons(mock_config_entry: MockConfigEntry, hass: HomeAssistant) -> None:
    """Register both Start buttons as enabled before setup, so they are created with a state.

    Changing disabled_by after setup makes HA reload the entry; pre-registering avoids that.
    The entry must be known to hass first; setup_integration then logs one harmless
    "already exists" ERROR from its own add_to_hass call (same as test_diagnostics_entities).
    """
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for key in START_KEYS:
        registry.async_get_or_create(
            "button", "gaggimate", f"{HOST}_{key}", config_entry=mock_config_entry, disabled_by=None
        )


def _own_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        f"{r.name}: {r.getMessage()}"
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith(OWN_LOGGERS)
    ]


def _warn_entry(key: str, level: int = 1, active: bool = False) -> dict[str, Any]:
    return {"k": key, "l": level, "a": active}


def _base_warn() -> list[dict[str, Any]]:
    """The single_boiler_idle warn list: no backflush or descaling entry."""
    return list(load_json("single_boiler_idle")["state"]["warn"])


def _use_frames(
    fake: FakeGaggiMate,
    name: str = "single_boiler_idle",
    state: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
) -> None:
    data = load_json(name)
    slow = {**data["state"], **(state or {})}
    for key in drop:
        slow.pop(key, None)
    fake.state = slow
    fake.telemetry = data["telemetry"]


def _eid(hass: HomeAssistant, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("button", "gaggimate", f"{HOST}_{key}")


def _need(hass: HomeAssistant, key: str) -> str:
    entity_id = _eid(hass, key)
    assert entity_id is not None, f"button {key} not registered"
    return entity_id


def _entry(hass: HomeAssistant, key: str) -> er.RegistryEntry:
    entry = er.async_get(hass).async_get(_need(hass, key))
    assert entry is not None
    return entry


async def _press(hass: HomeAssistant, key: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": _need(hass, key)}, blocking=True)


def _task_action(key: str) -> tuple[str, str]:
    """"backflush_start" -> ("backflush", "start")."""
    task, action = key.rsplit("_", 1)
    return task, action


async def _push_state_and_wait(fake: FakeGaggiMate, coordinator: Any, **changes: Any) -> None:
    """Push a slow frame and wait until the coordinator processed it (keepalive is off)."""
    updates = 0

    def _count() -> None:
        nonlocal updates
        updates += 1

    unsubscribe = coordinator.async_add_listener(_count)
    try:
        await fake.push_state(**changes)
        await wait_until(lambda: updates >= 1)
    finally:
        unsubscribe()
    await settle(0.02)


# -- gating ------------------------------------------------------------------------------------------


async def test_cleaning_buttons_absent_without_warn_keys(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate)  # single_boiler_idle: warn has no backflush or descaling entry
    await setup_integration()
    for key in ALL_KEYS:
        assert _eid(hass, key) is None


async def test_cleaning_buttons_registered_with_warn_keys(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle")
    await setup_integration()
    for key, (name, _) in CLEANING.items():
        entry = _entry(hass, key)
        assert entry.unique_id == f"{HOST}_{key}"
        assert entry.domain == "button"
        assert entry.platform == "gaggimate"
        assert entry.original_name == name
        expected = er.RegistryEntryDisabler.INTEGRATION if key in START_KEYS else None
        assert entry.disabled_by is expected, key
    for key in START_KEYS:
        # disabled entities are registered but have no state
        assert hass.states.get(_need(hass, key)) is None
    for key in COMPLETE_KEYS:
        assert hass.states.get(_need(hass, key)) is not None


@pytest.mark.parametrize(("task", "other"), [("backflush", "descaling"), ("descaling", "backflush")])
async def test_each_pair_gated_by_its_own_warn_key(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    task: str,
    other: str,
) -> None:
    _use_frames(fake_gaggimate, state={"warn": _base_warn() + [_warn_entry(task)]})
    await setup_integration()
    assert _eid(hass, f"{task}_start") is not None
    assert _eid(hass, f"{task}_complete") is not None
    assert _eid(hass, f"{other}_start") is None
    assert _eid(hass, f"{other}_complete") is None


@pytest.mark.parametrize("key", ["Backflush", "backflush_done", "descale", "BACKFLUSH"])
async def test_gate_is_exact_key_match(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, key: str
) -> None:
    _use_frames(fake_gaggimate, state={"warn": _base_warn() + [_warn_entry(key)]})
    await setup_integration()
    for button in ALL_KEYS:
        assert _eid(hass, button) is None


@pytest.mark.parametrize(("level", "active"), [(1, False), (2, True)])
async def test_present_whether_or_not_cleaning_is_due(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    level: int,
    active: bool,
) -> None:
    warn = _base_warn() + [_warn_entry("backflush", level, active), _warn_entry("descaling", level, active)]
    _use_frames(fake_gaggimate, state={"warn": warn})
    await setup_integration()
    for key in ALL_KEYS:
        assert _eid(hass, key) is not None


async def test_buttons_appear_when_warn_key_arrives_late(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()  # single_boiler_idle: no cleaning keys yet
    for key in ALL_KEYS:
        assert _eid(hass, key) is None

    await _push_state_and_wait(fake_gaggimate, coordinator, warn=_base_warn() + [_warn_entry("backflush", 2, True)])
    await wait_until(lambda: _eid(hass, "backflush_complete") is not None)
    await wait_until(lambda: _eid(hass, "backflush_start") is not None)

    assert _eid(hass, "descaling_start") is None
    assert _eid(hass, "descaling_complete") is None
    assert _entry(hass, "backflush_start").disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert _entry(hass, "backflush_complete").disabled_by is None


async def test_buttons_stay_registered_when_warn_key_disappears(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle")
    coordinator = await setup_integration()
    before = {key: _need(hass, key) for key in ALL_KEYS}

    # the cleaning entries leave the warn list: add-only, so the entities stay registered
    await _push_state_and_wait(fake_gaggimate, coordinator, warn=_base_warn())
    await settle(0.1)
    assert {key: _eid(hass, key) for key in ALL_KEYS} == before

    # the entries come back: no duplicate registration
    await _push_state_and_wait(fake_gaggimate, coordinator, warn=_base_warn() + [_warn_entry("backflush"), _warn_entry("descaling")])
    await settle(0.1)
    registry = er.async_get(hass)
    for key in ALL_KEYS:
        matches = [e for e in registry.entities.values() if e.unique_id == f"{HOST}_{key}"]
        assert len(matches) == 1, key
        assert _eid(hass, key) == before[key]


# -- wire messages -----------------------------------------------------------------------------------


@pytest.mark.parametrize("key", COMPLETE_KEYS)
async def test_complete_button_sends_rid_request(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, key: str
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle")
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _press(hass, key)
    [request] = fake_gaggimate.requests
    assert request["tp"] == CLEANING[key][1]
    assert set(request) == {"tp", "rid"}
    assert isinstance(request["rid"], str) and request["rid"]


@pytest.mark.parametrize("key", START_KEYS)
async def test_start_button_sends_rid_request(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_start_buttons: None,
    setup_integration: SetupIntegration,
    key: str,
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle")
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _press(hass, key)
    [request] = fake_gaggimate.requests
    assert request["tp"] == CLEANING[key][1]
    assert set(request) == {"tp", "rid"}
    assert isinstance(request["rid"], str) and request["rid"]


async def test_each_press_gets_its_own_rid(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle")
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _press(hass, "backflush_complete")
    await _press(hass, "backflush_complete")
    first, second = fake_gaggimate.requests
    assert first["rid"] != second["rid"]


# -- readiness guard ---------------------------------------------------------------------------------


@pytest.mark.parametrize("key", START_KEYS)
@pytest.mark.parametrize(
    "sys_state",
    [
        {"s": "heating", "m": "Heating"},
        {"s": "waiting", "m": "Waiting for controller"},
        {"s": "error", "m": "Controller error"},
    ],
)
async def test_start_refused_when_not_ready(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_start_buttons: None,
    setup_integration: SetupIntegration,
    key: str,
    sys_state: dict[str, str],
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle", state={"sys": sys_state})
    await setup_integration()
    fake_gaggimate.clear_requests()
    with pytest.raises(HomeAssistantError, match="not ready"):
        await _press(hass, key)
    await settle(0.1)
    assert fake_gaggimate.requests == []


@pytest.mark.parametrize("key", START_KEYS)
@pytest.mark.parametrize(("state", "drop"), [({}, ("sys",)), ({"sys": None}, ())])
async def test_start_not_blocked_without_sys_state(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_start_buttons: None,
    setup_integration: SetupIntegration,
    key: str,
    state: dict[str, Any],
    drop: tuple[str, ...],
) -> None:
    # older firmware sends no sys object (or null): the guard checks nothing
    _use_frames(fake_gaggimate, "dual_boiler_idle", state=state, drop=drop)
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _press(hass, key)
    [request] = fake_gaggimate.requests
    assert request["tp"] == CLEANING[key][1]


# -- failure paths -----------------------------------------------------------------------------------


@pytest.mark.parametrize("key", START_KEYS)
async def test_start_success_false_raises(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_start_buttons: None,
    setup_integration: SetupIntegration,
    key: str,
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle")
    fake_gaggimate.cleaning_results[":".join(_task_action(key))] = {"success": False}
    await setup_integration()
    fake_gaggimate.clear_requests()
    with pytest.raises(HomeAssistantError, match="Machine busy or profile unavailable"):
        await _press(hass, key)
    assert len(fake_gaggimate.requests_of(CLEANING[key][1])) == 1


@pytest.mark.parametrize("key", COMPLETE_KEYS)
async def test_complete_success_false_raises(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, key: str
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle")
    fake_gaggimate.cleaning_results[":".join(_task_action(key))] = {"success": False}
    await setup_integration()
    fake_gaggimate.clear_requests()
    with pytest.raises(HomeAssistantError, match="Could not mark cleaning as done"):
        await _press(hass, key)
    assert len(fake_gaggimate.requests_of(CLEANING[key][1])) == 1


@pytest.mark.parametrize("key", ALL_KEYS)
async def test_error_field_raises(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_start_buttons: None,
    setup_integration: SetupIntegration,
    key: str,
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle")
    fake_gaggimate.cleaning_results[":".join(_task_action(key))] = {"error": "Cleaning profile missing"}
    await setup_integration()
    with pytest.raises(HomeAssistantError, match="Cleaning profile missing"):
        await _press(hass, key)


@pytest.mark.parametrize("key", ALL_KEYS)
async def test_no_answer_times_out(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_start_buttons: None,
    setup_integration: SetupIntegration,
    key: str,
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle")
    fake_gaggimate.cleaning_results[":".join(_task_action(key))] = None  # firmware never answers
    coordinator = await setup_integration()
    with patch(f"{COORDINATOR_MODULE}.WS_REQUEST_TIMEOUT", 0.2):
        with pytest.raises(HomeAssistantError, match="Timed out waiting for response"):
            await _press(hass, key)
    assert coordinator._pending_requests == {}


@pytest.mark.parametrize("key", ALL_KEYS)
async def test_press_waits_for_response(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_start_buttons: None,
    setup_integration: SetupIntegration,
    key: str,
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle")
    await setup_integration()
    fake_gaggimate.clear_requests()
    fake_gaggimate.cleaning_delay = 0.4
    press = hass.async_create_task(_press(hass, key))
    await asyncio.sleep(0.1)
    assert not press.done(), "press returned before the response arrived"
    assert len(fake_gaggimate.requests_of(CLEANING[key][1])) == 1
    async with asyncio.timeout(5):
        await press


async def test_start_success_true_with_brew_confirm_raised_does_not_hang(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_start_buttons: None,
    setup_integration: SetupIntegration,
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle")
    await setup_integration()
    fake_gaggimate.clear_requests()
    fake_gaggimate.cleaning_delay = 0.3
    press = hass.async_create_task(_press(hass, "backflush_start"))
    await asyncio.sleep(0.1)
    # the firmware raises a brew confirmation while the start request is in flight
    await fake_gaggimate.push({"tp": EVT_BREW_CONFIRM})
    async with asyncio.timeout(5):
        await press  # success true: no exception
    assert len(fake_gaggimate.requests_of(CLEANING["backflush_start"][1])) == 1


# -- logging -----------------------------------------------------------------------------------------


async def test_happy_path_logs_no_warnings(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_start_buttons: None,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle")
    await setup_integration()
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    for key in ALL_KEYS:
        await _press(hass, key)
    await settle()
    # sanity: the debug record of a successful press was captured, so the empty warning list means something
    assert any("Pressed backflush_start" in r.getMessage() for r in caplog.records)
    assert _own_warnings(caplog) == []


# -- existing buttons --------------------------------------------------------------------------------


async def test_existing_buttons_unchanged(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    enabled_start_buttons: None,
    mock_config_entry: MockConfigEntry,
    setup_integration: SetupIntegration,
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle")
    await setup_integration()
    buttons = {
        e.unique_id.removeprefix(f"{HOST}_")
        for e in er.async_entries_for_config_entry(er.async_get(hass), mock_config_entry.entry_id)
        if e.domain == "button"
    }
    assert buttons == EXISTING_BUTTON_KEYS | set(ALL_KEYS)
    for key in EXISTING_BUTTON_KEYS:
        assert _entry(hass, key).disabled_by is None, key

    fake_gaggimate.clear_requests()
    await _press(hass, "process_clear")
    await _press(hass, "brew_stop")
    # fire-and-forget messages: wait until the fake firmware has received both
    await fake_gaggimate.wait_for_request("req:process:deactivate")
    assert fake_gaggimate.requests == [{"tp": "req:process:clear"}, {"tp": "req:process:deactivate"}]
