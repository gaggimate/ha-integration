"""Step 14: event entities (plan.md Step 14).

* ``<host>_shot_events`` ("Shot Events"): shot_saved, shot_finished.
* ``<host>_machine_events`` ("Machine Events"): scale_disconnected, autotune_result, autotune_failed.
* Fed by the coordinator event bus (non-status ``evt:*`` frames); bad payload fields become None.
* Unavailable with the device, no replay after reconnect, one listener per entity, removed on unload.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gaggimate.coordinator import GaggiMateCoordinator

from .conftest import SetupIntegration, settle, wait_until
from .fake_server import FakeGaggiMate, load_json

HOST = "127.0.0.1"
OWN_LOGGERS = (
    "custom_components.gaggimate",
    "homeassistant.components.event",
    "homeassistant.helpers.entity",
)
UNAVAILABLE = "unavailable"
SHOT_TYPES = ["shot_saved", "shot_finished"]
MACHINE_TYPES = ["scale_disconnected", "autotune_result", "autotune_failed"]
PAYLOAD_KEYS = {
    "shot_saved": {"shot_id"},
    "shot_finished": {"max_pressure_bar", "avg_flow_ml_s"},
    "scale_disconnected": {"name", "address"},
    "autotune_result": {"pid"},
    "autotune_failed": set(),
}
ALL_PAYLOAD_KEYS = {k for v in PAYLOAD_KEYS.values() for k in v}
STD_ATTRS = {"event_type", "event_types", "friendly_name", "icon"}


@pytest.fixture(autouse=True)
def _no_keepalive(fake_gaggimate: FakeGaggiMate) -> None:
    fake_gaggimate.keepalive = False


def _own_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        f"{r.name}: {r.getMessage()}"
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith(OWN_LOGGERS)
    ]


def _eid(hass: HomeAssistant, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("event", "gaggimate", f"{HOST}_{key}")
    assert entity_id is not None, f"event {key} not registered"
    return entity_id


def _state(hass: HomeAssistant, key: str) -> State:
    state = hass.states.get(_eid(hass, key))
    assert state is not None
    return state


def _payload(state: State) -> dict[str, Any]:
    return {k: v for k, v in state.attributes.items() if k not in STD_ATTRS}


async def _send(fake: FakeGaggiMate, frame: dict[str, Any]) -> None:
    await fake.push(frame)
    await settle(0.1)


async def _fire_and_get(
    hass: HomeAssistant, fake: FakeGaggiMate, key: str, frame: dict[str, Any], prev: str | None = None
) -> State:
    """Push a frame and wait for the entity state to change from ``prev`` (when given)."""
    await fake.push(frame)
    if prev is not None:
        await wait_until(lambda: _state(hass, key).state != prev)
    else:
        await wait_until(lambda: _state(hass, key).attributes.get("event_type") is not None)
    await settle()
    return _state(hass, key)


# -- creation ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("frames", ["single_boiler_idle", "waiting_controller", "dual_boiler_idle"])
async def test_both_entities_created_on_all_frame_sets(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    frames: str,
) -> None:
    data = load_json(frames)
    fake_gaggimate.state, fake_gaggimate.telemetry = data["state"], data["telemetry"]
    await setup_integration()
    registry = er.async_get(hass)
    entries = {
        e.unique_id: e
        for e in er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
        if e.domain == "event"
    }
    assert set(entries) == {f"{HOST}_shot_events", f"{HOST}_machine_events"}
    for entry in entries.values():
        assert entry.disabled_by is None
        assert entry.platform == "gaggimate"
    assert len(hass.states.async_entity_ids("event")) == 2


async def test_names_event_types_and_initial_state(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    await setup_integration()
    shot, machine = _state(hass, "shot_events"), _state(hass, "machine_events")
    assert shot.attributes["friendly_name"].endswith("Shot Events")
    assert machine.attributes["friendly_name"].endswith("Machine Events")
    assert list(shot.attributes["event_types"]) == SHOT_TYPES
    assert list(machine.attributes["event_types"]) == MACHINE_TYPES
    for state in (shot, machine):
        assert state.state not in (UNAVAILABLE,)
        assert state.attributes.get("event_type") is None
        assert _payload(state) == {}


async def test_same_device_as_other_entities(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
    assert len({e.device_id for e in entities}) == 1


# -- happy-path mappings ----------------------------------------------------------------------------


async def test_shot_saved(hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration) -> None:
    await setup_integration()
    state = await _fire_and_get(hass, fake_gaggimate, "shot_events", {"tp": "evt:history-shot-saved", "id": 42})
    assert state.attributes["event_type"] == "shot_saved"
    assert state.attributes["shot_id"] == 42
    assert isinstance(state.attributes["shot_id"], int)
    # the other entity is untouched
    assert _state(hass, "machine_events").attributes.get("event_type") is None


async def test_shot_finished(hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration) -> None:
    await setup_integration()
    state = await _fire_and_get(
        hass, fake_gaggimate, "shot_events", {"tp": "evt:shot-finished-stats", "maxPressure": 9.5, "avgFlow": 2}
    )
    assert state.attributes["event_type"] == "shot_finished"
    assert state.attributes["max_pressure_bar"] == 9.5
    assert state.attributes["avg_flow_ml_s"] == 2.0
    assert isinstance(state.attributes["avg_flow_ml_s"], float)
    assert _state(hass, "machine_events").attributes.get("event_type") is None


async def test_scale_disconnected(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    state = await _fire_and_get(
        hass,
        fake_gaggimate,
        "machine_events",
        {"tp": "evt:scale:disconnect", "address": "AA:BB:CC:DD:EE:FF", "name": "Acaia"},
    )
    assert state.attributes["event_type"] == "scale_disconnected"
    assert state.attributes["name"] == "Acaia"
    assert state.attributes["address"] == "AA:BB:CC:DD:EE:FF"
    assert _state(hass, "shot_events").attributes.get("event_type") is None


async def test_autotune_result(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    state = await _fire_and_get(
        hass, fake_gaggimate, "machine_events", {"tp": "evt:autotune-result", "pid": "1.0,2.0,3.0"}
    )
    assert state.attributes["event_type"] == "autotune_result"
    assert state.attributes["pid"] == "1.0,2.0,3.0"


async def test_autotune_failed_has_no_attributes(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    state = await _fire_and_get(hass, fake_gaggimate, "machine_events", {"tp": "evt:autotune-failed", "junk": 1})
    assert state.attributes["event_type"] == "autotune_failed"
    assert _payload(state) == {}


async def test_payload_attributes_replaced_by_next_event(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    s1 = await _fire_and_get(hass, fake_gaggimate, "machine_events", {"tp": "evt:autotune-result", "pid": "x"})
    assert s1.attributes["pid"] == "x"
    await asyncio.sleep(0.02)
    s2 = await _fire_and_get(hass, fake_gaggimate, "machine_events", {"tp": "evt:autotune-failed"}, prev=s1.state)
    assert s2.attributes["event_type"] == "autotune_failed"
    assert "pid" not in s2.attributes


async def test_consecutive_same_type_events_each_update_state(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    seen: list[str] = []
    s = await _fire_and_get(hass, fake_gaggimate, "shot_events", {"tp": "evt:history-shot-saved", "id": 1})
    seen.append(s.state)
    for shot_id in (2, 3, 4):
        await asyncio.sleep(0.02)
        s = await _fire_and_get(
            hass, fake_gaggimate, "shot_events", {"tp": "evt:history-shot-saved", "id": shot_id}, prev=s.state
        )
        assert s.attributes["shot_id"] == shot_id
        assert s.attributes["event_type"] == "shot_saved"
        seen.append(s.state)
    assert len(set(seen)) == 4


async def test_identical_payload_twice_still_fires_twice(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    s1 = await _fire_and_get(hass, fake_gaggimate, "machine_events", {"tp": "evt:autotune-failed"})
    await asyncio.sleep(0.02)
    s2 = await _fire_and_get(hass, fake_gaggimate, "machine_events", {"tp": "evt:autotune-failed"}, prev=s1.state)
    assert s2.state != s1.state


# -- bad payloads -----------------------------------------------------------------------------------

BAD = float("nan"), float("inf"), float("-inf")


@pytest.mark.parametrize(
    "frame",
    [
        {"tp": "evt:history-shot-saved"},
        {"tp": "evt:history-shot-saved", "id": None},
        {"tp": "evt:history-shot-saved", "id": True},
        {"tp": "evt:history-shot-saved", "id": "5"},
        {"tp": "evt:history-shot-saved", "id": 5.5},
        {"tp": "evt:history-shot-saved", "id": [1]},
        {"tp": "evt:history-shot-saved", "id": {"a": 1}},
    ],
)
async def test_shot_saved_bad_payload_gives_none(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
    frame: dict[str, Any],
) -> None:
    await setup_integration()
    state = await _fire_and_get(hass, fake_gaggimate, "shot_events", frame)
    assert state.attributes["event_type"] == "shot_saved"
    assert state.attributes["shot_id"] is None
    assert not _own_warnings(caplog)


@pytest.mark.parametrize(
    ("fields", "expect"),
    [
        ({}, (None, None)),
        ({"maxPressure": True, "avgFlow": False}, (None, None)),
        ({"maxPressure": "9", "avgFlow": "2"}, (None, None)),
        ({"maxPressure": None, "avgFlow": [1]}, (None, None)),
        ({"maxPressure": 9, "avgFlow": "x"}, (9.0, None)),
        ({"maxPressure": 9.1}, (9.1, None)),
        ({"avgFlow": 0}, (None, 0.0)),
    ],
)
async def test_shot_finished_bad_payload_gives_none(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
    fields: dict[str, Any],
    expect: tuple[float | None, float | None],
) -> None:
    await setup_integration()
    state = await _fire_and_get(hass, fake_gaggimate, "shot_events", {"tp": "evt:shot-finished-stats", **fields})
    assert state.attributes["event_type"] == "shot_finished"
    actual = (state.attributes["max_pressure_bar"], state.attributes["avg_flow_ml_s"])
    assert actual == expect
    assert not _own_warnings(caplog)


@pytest.mark.parametrize("which", ["maxPressure", "avgFlow"])
@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
async def test_shot_finished_non_finite_rejected(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
    which: str,
    raw: str,
) -> None:
    """Python's JSON parser accepts NaN/Infinity tokens, so send them as raw frames."""
    await setup_integration()
    other = "avgFlow" if which == "maxPressure" else "maxPressure"
    await fake_gaggimate.push_raw('{"tp": "evt:shot-finished-stats", "%s": %s, "%s": 3.5}' % (which, raw, other))
    await wait_until(lambda: _state(hass, "shot_events").attributes.get("event_type") is not None)
    await settle()
    attrs = _state(hass, "shot_events").attributes
    key = {"maxPressure": "max_pressure_bar", "avgFlow": "avg_flow_ml_s"}
    assert attrs[key[which]] is None
    assert attrs[key[other]] == 3.5
    assert not _own_warnings(caplog)


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"name": 1, "address": 2},
        {"name": None, "address": None},
        {"name": True, "address": ["a"]},
        {"name": {"x": 1}, "address": 1.5},
    ],
)
async def test_scale_disconnect_bad_payload_gives_none(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
    fields: dict[str, Any],
) -> None:
    await setup_integration()
    state = await _fire_and_get(hass, fake_gaggimate, "machine_events", {"tp": "evt:scale:disconnect", **fields})
    assert state.attributes["event_type"] == "scale_disconnected"
    assert state.attributes["name"] is None
    assert state.attributes["address"] is None
    assert not _own_warnings(caplog)


async def test_scale_disconnect_partial_payload(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    state = await _fire_and_get(hass, fake_gaggimate, "machine_events", {"tp": "evt:scale:disconnect", "name": "S"})
    assert state.attributes["name"] == "S"
    assert state.attributes["address"] is None


@pytest.mark.parametrize("pid", [None, 5, 1.5, True, ["a"], {"a": 1}])
async def test_autotune_result_bad_pid_gives_none(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
    pid: Any,
) -> None:
    await setup_integration()
    state = await _fire_and_get(hass, fake_gaggimate, "machine_events", {"tp": "evt:autotune-result", "pid": pid})
    assert state.attributes["event_type"] == "autotune_result"
    assert state.attributes["pid"] is None
    assert not _own_warnings(caplog)


async def test_autotune_result_missing_pid_gives_none(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, caplog: pytest.LogCaptureFixture
) -> None:
    await setup_integration()
    state = await _fire_and_get(hass, fake_gaggimate, "machine_events", {"tp": "evt:autotune-result"})
    assert state.attributes["pid"] is None
    assert not _own_warnings(caplog)


@pytest.mark.parametrize(
    ("tp", "key", "attr"),
    [
        ("evt:history-shot-saved", "shot_events", "shot_id"),
        ("evt:shot-finished-stats", "shot_events", "max_pressure_bar"),
        ("evt:scale:disconnect", "machine_events", "name"),
        ("evt:autotune-result", "machine_events", "pid"),
    ],
)
@pytest.mark.parametrize("message", [{}, "notadict", None, 5, [1, 2]])
async def test_direct_dispatch_of_empty_or_non_dict_does_not_raise(
    hass: HomeAssistant,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
    tp: str,
    key: str,
    attr: str,
    message: Any,
) -> None:
    """Empty dict / non-dict messages (as delivered by a misbehaving bus) give None attributes."""
    coordinator = await setup_integration()
    with caplog.at_level(logging.DEBUG):
        coordinator._dispatch_event(tp, message)
    await settle()
    state = _state(hass, key)
    assert state.attributes["event_type"] is not None
    assert state.attributes[attr] is None
    assert not _own_warnings(caplog)


async def test_direct_dispatch_non_dict_autotune_failed(
    hass: HomeAssistant, setup_integration: SetupIntegration, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = await setup_integration()
    coordinator._dispatch_event("evt:autotune-failed", "junk")  # type: ignore[arg-type]
    await settle()
    assert _state(hass, "machine_events").attributes["event_type"] == "autotune_failed"
    assert not _own_warnings(caplog)


async def test_bad_payload_logs_debug_only(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, caplog: pytest.LogCaptureFixture
) -> None:
    await setup_integration()
    with caplog.at_level(logging.DEBUG, logger="custom_components.gaggimate.event"):
        await _fire_and_get(hass, fake_gaggimate, "shot_events", {"tp": "evt:history-shot-saved", "id": "bad"})
    recs = [r for r in caplog.records if r.name == "custom_components.gaggimate.event"]
    assert recs
    assert all(r.levelno == logging.DEBUG for r in recs)


# -- unrelated / status frames ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "frame",
    [
        {"tp": "evt:history-delete-all"},
        {"tp": "evt:scale:connect:success", "name": "x"},
        {"tp": "evt:totally-unknown", "id": 1},
        {"tp": "evt:brew:confirm", "warn": []},
        {"tp": "evt:history-shot-saved-extra", "id": 1},
        {"tp": "EVT:HISTORY-SHOT-SAVED", "id": 1},
        {"tp": "history-shot-saved", "id": 1},
    ],
)
async def test_unrelated_events_trigger_nothing(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, frame: dict[str, Any]
) -> None:
    await setup_integration()
    before = (_state(hass, "shot_events"), _state(hass, "machine_events"))
    await _send(fake_gaggimate, frame)
    after = (_state(hass, "shot_events"), _state(hass, "machine_events"))
    for b, a in zip(before, after, strict=True):
        assert a.state == b.state
        assert a.attributes.get("event_type") is None
        assert a.last_updated == b.last_updated


async def test_status_frames_never_trigger(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    before = (_state(hass, "shot_events"), _state(hass, "machine_events"))
    await fake_gaggimate.push_state(sbl=True)
    await fake_gaggimate.push_telemetry(ct=91.0)
    await fake_gaggimate.push({"tp": "evt:status", "id": 1, "maxPressure": 5, "pid": "x"})
    await settle(0.15)
    after = (_state(hass, "shot_events"), _state(hass, "machine_events"))
    for b, a in zip(before, after, strict=True):
        assert a.attributes.get("event_type") is None
        assert a.state == b.state


async def test_frame_without_tp_or_invalid_json_ignored(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, caplog: pytest.LogCaptureFixture
) -> None:
    await setup_integration()
    await fake_gaggimate.push({"id": 7})
    await fake_gaggimate.push_raw("not json")
    await fake_gaggimate.push_raw("[1, 2]")
    await settle(0.15)
    assert _state(hass, "shot_events").attributes.get("event_type") is None
    assert _state(hass, "machine_events").attributes.get("event_type") is None
    # entity still works afterwards
    state = await _fire_and_get(hass, fake_gaggimate, "shot_events", {"tp": "evt:history-shot-saved", "id": 9})
    assert state.attributes["shot_id"] == 9


# -- availability / reconnect -----------------------------------------------------------------------


async def test_unavailable_when_disconnected_and_no_replay_after_reconnect(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    with patch("custom_components.gaggimate.coordinator.WS_RECONNECT_DELAYS", [1.0]):
        coordinator = await setup_integration()
        s = await _fire_and_get(hass, fake_gaggimate, "shot_events", {"tp": "evt:history-shot-saved", "id": 7})
        before_state = s.state
        await fake_gaggimate.drop_connections()
        await wait_until(lambda: not coordinator.available)
        await settle()
        assert _state(hass, "shot_events").state == UNAVAILABLE
        assert _state(hass, "machine_events").state == UNAVAILABLE
        await wait_until(lambda: coordinator.available, timeout=8)
        await settle(0.2)
        shot = _state(hass, "shot_events")
        machine = _state(hass, "machine_events")
        assert shot.state != UNAVAILABLE
        assert machine.state != UNAVAILABLE
        # nothing new fired by the reconnect: same timestamp, machine entity never fired
        assert shot.state == before_state
        assert machine.attributes.get("event_type") is None
        # events work again after reconnect
        await asyncio.sleep(0.02)
        s2 = await _fire_and_get(
            hass, fake_gaggimate, "shot_events", {"tp": "evt:history-shot-saved", "id": 8}, prev=before_state
        )
        assert s2.attributes["shot_id"] == 8


async def test_event_while_disconnected_not_replayed(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """An event raised on the (fake) server while no client is connected is lost, never replayed."""
    with patch("custom_components.gaggimate.coordinator.WS_RECONNECT_DELAYS", [1.0]):
        coordinator = await setup_integration()
        await fake_gaggimate.drop_connections()
        await wait_until(lambda: not coordinator.available)
        await fake_gaggimate.push({"tp": "evt:autotune-failed"})  # no connection: goes nowhere
        await wait_until(lambda: coordinator.available, timeout=8)
        await settle(0.2)
        assert _state(hass, "machine_events").attributes.get("event_type") is None


# -- listener lifecycle -----------------------------------------------------------------------------


async def test_one_listener_per_entity(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    coordinator = await setup_integration()
    handlers = [l for l in coordinator._event_listeners if getattr(l, "__qualname__", "").startswith("GaggiMateEvent.")]
    assert len(handlers) == 2
    # distinct bound methods of distinct entities
    assert len({id(l.__self__) for l in handlers}) == 2


async def test_unload_removes_listeners(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    baseline_event_listeners = len(coordinator._event_listeners)
    assert baseline_event_listeners >= 2
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert coordinator._event_listeners == []
    assert not [
        l for l in coordinator._event_listeners if getattr(l, "__qualname__", "").startswith("GaggiMateEvent.")
    ]


async def test_reload_single_listener_and_event_fires_once(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
) -> None:
    coordinator = await setup_integration()

    def event_listener_count(c: GaggiMateCoordinator) -> int:
        return len([l for l in c._event_listeners if getattr(l, "__qualname__", "").startswith("GaggiMateEvent.")])

    assert event_listener_count(coordinator) == 2
    total_before = len(coordinator._event_listeners)

    for _ in range(2):
        old = coordinator
        assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data["gaggimate"][mock_config_entry.entry_id]
        await wait_until(lambda: coordinator.available)
        await settle()
        assert old._event_listeners == []
        assert event_listener_count(coordinator) == 2
        assert len(coordinator._event_listeners) == total_before

    # a single frame produces exactly one state write per entity
    changes: list[Any] = []
    entity_id = _eid(hass, "shot_events")

    def _on_change(event: Any) -> None:
        if event.data["entity_id"] == entity_id:
            changes.append(event.data["new_state"])

    unsub = hass.bus.async_listen("state_changed", _on_change)
    try:
        await wait_until(lambda: len(fake_gaggimate.connections) >= 1)
        await _send(fake_gaggimate, {"tp": "evt:history-shot-saved", "id": 99})
    finally:
        unsub()
    assert len(changes) == 1
    assert changes[0].attributes["shot_id"] == 99
    assert changes[0].attributes["event_type"] == "shot_saved"


async def test_single_event_single_state_write_without_reload(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    changes: list[str] = []
    ids = {_eid(hass, "shot_events"), _eid(hass, "machine_events")}

    def _on_change(event: Any) -> None:
        if event.data["entity_id"] in ids:
            changes.append(event.data["entity_id"])

    unsub = hass.bus.async_listen("state_changed", _on_change)
    try:
        await _send(fake_gaggimate, {"tp": "evt:autotune-failed"})
    finally:
        unsub()
    assert changes == [_eid(hass, "machine_events")]


async def test_listener_exception_in_other_listener_does_not_block_entities(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = await setup_integration()

    def bad(tp: str, msg: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    coordinator.async_add_event_listener(bad)
    state = await _fire_and_get(hass, fake_gaggimate, "shot_events", {"tp": "evt:history-shot-saved", "id": 3})
    assert state.attributes["shot_id"] == 3
    assert any("boom" in str(r.exc_info[1]) for r in caplog.records if r.exc_info)
