"""Step 4: capability gating helper (entity.py) and the coordinator event bus."""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gaggimate.coordinator import GaggiMateCoordinator
from custom_components.gaggimate.entity import (
    GaggiMateEntity,
    async_setup_gated,
    cap,
    has_key,
    has_warning_key,
)
from custom_components.gaggimate.sensor import GaggiMateEntity as SensorReexport

from .conftest import SetupIntegration, settle, wait_until
from .fake_server import FakeGaggiMate, load_json

ENTITY_LOGGER = "custom_components.gaggimate.entity"
COORD_LOGGER = "custom_components.gaggimate.coordinator"


# -- gate helpers -------------------------------------------------------------------------------


def test_sensor_reexports_entity_base() -> None:
    assert SensorReexport is GaggiMateEntity


def test_has_key() -> None:
    gate = has_key("sys")
    assert gate({"sys": {"s": "ready"}}) is True
    assert gate({"sys": None}) is True  # null value counts as present
    assert gate({"sys": 0}) is True
    assert gate({}) is False
    assert gate({"other": 1}) is False


def test_cap() -> None:
    gate = cap("db")
    assert gate({"db": True}) is True
    assert gate({"db": False}) is False
    assert gate({}) is False
    assert gate({"db": None}) is False
    assert gate({"db": 1}) is False  # exactly True only


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"warn": [{"k": "water", "l": 1}, {"k": "temperature"}]}, True),
        ({"warn": [{"k": "temperature"}]}, False),
        ({"warn": []}, False),
        ({}, False),
        ({"warn": None}, False),
        ({"warn": "water"}, False),
        ({"warn": {"k": "water"}}, False),
        ({"warn": ["water"]}, False),
        ({"warn": [None, 5, {"x": 1}, {"k": "water"}]}, True),
        ({"warn": [None, 5, {"x": 1}]}, False),
    ],
)
def test_has_warning_key(data: dict[str, Any], expected: bool) -> None:
    assert has_warning_key("water")(data) is expected


# -- async_setup_gated -----------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class _Desc:
    key: str
    supported_fn: Callable[[dict[str, Any]], bool] | None = None


class _FakeEntry:
    """Collects ``async_on_unload`` callbacks."""

    def __init__(self) -> None:
        self.unloads: list[Callable[[], None]] = []

    def async_on_unload(self, func: Callable[[], None]) -> None:
        self.unloads.append(func)

    def unload(self) -> None:
        for func in self.unloads:
            func()
        self.unloads.clear()


class _Ent:
    def __init__(self, coordinator: Any, entry: Any, description: _Desc) -> None:
        self.description = description


class _Adder:
    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    def __call__(self, entities: Any) -> None:
        self.calls.append(list(entities))

    @property
    def keys(self) -> list[str]:
        return [e.description.key for call in self.calls for e in call]


def _update(coordinator: GaggiMateCoordinator, data: dict[str, Any]) -> None:
    coordinator.async_set_updated_data(data)


async def test_gated_entity_added_when_key_appears_once_only(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    adder, entry = _Adder(), _FakeEntry()
    descs = [
        _Desc(key="always"),
        _Desc(key="late", supported_fn=has_key("late_key")),
        _Desc(key="flag", supported_fn=cap("zz_flag")),
    ]
    async_setup_gated(coordinator, entry, adder, descs, _Ent)  # type: ignore[arg-type]
    assert adder.keys == ["always"]  # None supported_fn is unconditional; others absent

    _update(coordinator, {**coordinator.data, "late_key": None})
    assert adder.keys == ["always", "late"]
    for _ in range(3):
        _update(coordinator, {**coordinator.data, "x": 1})
    assert adder.keys == ["always", "late"]  # added once only

    _update(coordinator, {**coordinator.data, "zz_flag": True})
    assert adder.keys == ["always", "late", "flag"]
    entry.unload()


async def test_gated_never_removed_after_data_reset_and_flapping(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    adder, entry = _Adder(), _FakeEntry()
    descs = [_Desc(key="late", supported_fn=has_key("late_key"))]
    async_setup_gated(coordinator, entry, adder, descs, _Ent)  # type: ignore[arg-type]
    _update(coordinator, {"late_key": 1})
    assert adder.keys == ["late"]
    # availability-timeout reset
    _update(coordinator, {})
    assert adder.keys == ["late"]
    # flapping
    for i in range(5):
        _update(coordinator, {"late_key": i} if i % 2 == 0 else {})
    assert adder.keys == ["late"]
    entry.unload()


async def test_gated_capability_flag_flip_waiting_to_dual_boiler(
    hass: HomeAssistant, setup_integration: SetupIntegration, fake_gaggimate: FakeGaggiMate
) -> None:
    coordinator = await setup_integration()
    w, d = load_json("waiting_controller"), load_json("dual_boiler_idle")
    waiting, dual = {**w["state"], **w["telemetry"]}, {**d["state"], **d["telemetry"]}
    _update(coordinator, {**waiting})
    assert coordinator.data["db"] is False

    adder, entry = _Adder(), _FakeEntry()
    descs = [
        _Desc(key="dual", supported_fn=cap("db")),
        _Desc(key="dim", supported_fn=cap("cd")),
        _Desc(key="sys", supported_fn=has_key("sys")),
    ]
    async_setup_gated(coordinator, entry, adder, descs, _Ent)  # type: ignore[arg-type]
    assert adder.keys == ["sys"]
    _update(coordinator, {**coordinator.data, **dual})
    assert sorted(adder.keys) == ["dim", "dual", "sys"]
    assert adder.keys.count("dual") == 1
    entry.unload()


async def test_gated_unsubscribes_on_unload_and_no_listener_leak(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    before = len(coordinator._listeners)
    adder, entry = _Adder(), _FakeEntry()
    descs = [_Desc(key="late", supported_fn=has_key("late_key"))]
    async_setup_gated(coordinator, entry, adder, descs, _Ent)  # type: ignore[arg-type]
    assert len(coordinator._listeners) == before + 1
    entry.unload()
    assert len(coordinator._listeners) == before
    _update(coordinator, {"late_key": 1})
    assert adder.keys == []  # no further additions after unload


async def test_gated_failing_constructor_logged_skipped_retried(
    hass: HomeAssistant, setup_integration: SetupIntegration, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = await setup_integration()
    adder, entry = _Adder(), _FakeEntry()
    attempts = {"bad": 0}

    def factory(coord: Any, ent: Any, description: _Desc) -> _Ent:
        if description.key == "bad":
            attempts["bad"] += 1
            if attempts["bad"] == 1:
                raise RuntimeError("ctor boom")
        return _Ent(coord, ent, description)

    descs = [_Desc(key="bad"), _Desc(key="good")]
    with caplog.at_level(logging.DEBUG, logger=ENTITY_LOGGER):
        async_setup_gated(coordinator, entry, adder, descs, factory)  # type: ignore[arg-type]
    assert adder.keys == ["good"]
    assert any("bad" in r.getMessage() and r.levelno >= logging.ERROR for r in caplog.records)
    assert any(r.exc_info and "ctor boom" in str(r.exc_info[1]) for r in caplog.records)

    _update(coordinator, {**coordinator.data, "x": 1})
    assert adder.keys == ["good", "bad"]  # retried next update, good not re-added
    _update(coordinator, {**coordinator.data, "x": 2})
    assert adder.keys == ["good", "bad"]
    assert attempts["bad"] == 2
    entry.unload()


async def test_gated_raising_gate_logged_once_per_key_others_unaffected(
    hass: HomeAssistant, setup_integration: SetupIntegration, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = await setup_integration()
    adder, entry = _Adder(), _FakeEntry()

    def boom(_: dict[str, Any]) -> bool:
        raise ValueError("gate boom")

    descs = [
        _Desc(key="g1", supported_fn=boom),
        _Desc(key="g2", supported_fn=boom),
        _Desc(key="ok"),
        _Desc(key="later", supported_fn=has_key("later_key")),
    ]
    with caplog.at_level(logging.DEBUG, logger=ENTITY_LOGGER):
        async_setup_gated(coordinator, entry, adder, descs, _Ent)  # type: ignore[arg-type]
        for i in range(4):
            _update(coordinator, {**coordinator.data, "n": i})
        _update(coordinator, {**coordinator.data, "later_key": 1})
    assert adder.keys == ["ok", "later"]

    def loud(key: str) -> list[logging.LogRecord]:
        return [
            r for r in caplog.records
            if r.name == ENTITY_LOGGER and key in r.getMessage() and r.levelno >= logging.WARNING
        ]

    assert len(loud("g1")) == 1
    assert len(loud("g2")) == 1
    entry.unload()


async def test_gated_data_none_treated_as_empty(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    coordinator = await setup_integration()
    adder, entry = _Adder(), _FakeEntry()
    coordinator.data = None
    async_setup_gated(  # type: ignore[arg-type]
        coordinator, entry, adder, [_Desc(key="a"), _Desc(key="b", supported_fn=has_key("k"))], _Ent
    )
    assert adder.keys == ["a"]
    entry.unload()


async def test_number_platform_unchanged_and_reload_no_duplicates(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    setup_integration: SetupIntegration,
) -> None:
    coordinator = await setup_integration()
    registry = er.async_get(hass)

    def numbers() -> list[str]:
        return sorted(
            e.unique_id
            for e in er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
            if e.domain == "number"
        )

    assert numbers() == ["127.0.0.1_target_temperature_setpoint"]
    assert len(hass.states.async_entity_ids("number")) == 1
    total = len(er.async_entries_for_config_entry(registry, mock_config_entry.entry_id))
    # default frames (single_boiler_idle): 34 existing + 9 step-5 diagnostics (no backflush/descaling in warn)
    # + 2 step-6 power sensors (heater_power, pump_power; no puck/flow/steam on a single boiler without cp/cd/db)
    # + 2 step-9 buttons (flush_stop, process_clear; no tare_scale as hs is false; no steam_heater without db)
    # + 3 step-10 always-on entities (brew_confirm_pending, brew_confirm, brew_confirm_cancel)
    # + 1 step-11 Grind switch (gact present; no Grind By Weight as bta is 0)
    assert total == 51

    for _ in range(2):
        assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data["gaggimate"][mock_config_entry.entry_id]
        await wait_until(lambda: coordinator.available)
        await settle()
        assert numbers() == ["127.0.0.1_target_temperature_setpoint"]
        assert len(hass.states.async_entity_ids("number")) == 1
        # disabled-by-default entities are registered but have no state
        assert len(hass.states.async_entity_ids("sensor")) == len(
            [
                e
                for e in er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)
                if e.domain == "sensor" and e.disabled_by is None
            ]
        )
        assert len(er.async_entries_for_config_entry(registry, mock_config_entry.entry_id)) == total
        # the new coordinator holds a bounded number of listeners (no accumulation across reloads)
        # (+3: one gate listener per gated platform setup; step 11 added a second one on switch)
        assert len(coordinator._listeners) <= total + 3


async def test_reload_does_not_leak_gate_listener(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    count_loaded = len(coordinator._listeners)
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    # nothing of the entry (entities, gate listener) may remain subscribed to the old coordinator
    assert len(coordinator._listeners) == 0, f"{len(coordinator._listeners)} listeners left of {count_loaded}"
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    new = hass.data["gaggimate"][mock_config_entry.entry_id]
    await wait_until(lambda: new.available)
    await settle()
    assert len(new._listeners) == count_loaded


# -- event bus -------------------------------------------------------------------------------------


def _frame(**fields: Any) -> str:
    return json.dumps(fields)


@pytest.fixture
def events() -> dict[str, dict[str, Any]]:
    return load_json("events")["events"]


async def test_events_reach_listeners_and_do_not_touch_data(
    hass: HomeAssistant, setup_integration: SetupIntegration, events: dict[str, dict[str, Any]]
) -> None:
    coordinator = await setup_integration()
    got: list[tuple[str, dict[str, Any]]] = []
    coordinator.async_add_event_listener(lambda tp, msg: got.append((tp, msg)))
    data_before = coordinator.data
    snapshot = dict(coordinator.data)
    updates = 0

    def _count() -> None:
        nonlocal updates
        updates += 1

    coordinator.async_add_listener(_count)

    msgs = [
        events["shot_saved"],
        events["scale_disconnect"],
        {"tp": "evt:totally-unknown", "foo": "bar"},
        events["brew_confirm_cancel"],
    ]
    for msg in msgs:
        await coordinator._handle_message(json.dumps(msg))

    assert got == [(m["tp"], m) for m in msgs]
    assert coordinator.data is data_before
    assert coordinator.data == snapshot
    assert "id" not in coordinator.data and "address" not in coordinator.data
    assert updates == 0  # dispatch alone does not notify entities


async def test_event_over_websocket_reaches_listener(
    hass: HomeAssistant, setup_integration: SetupIntegration, fake_gaggimate: FakeGaggiMate
) -> None:
    coordinator = await setup_integration()
    got: list[str] = []
    coordinator.async_add_event_listener(lambda tp, msg: got.append(tp))
    await fake_gaggimate.push({"tp": "evt:history-shot-saved", "id": 7})
    await wait_until(lambda: "evt:history-shot-saved" in got)
    assert "id" not in coordinator.data


async def test_status_not_dispatched_but_merged(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    got: list[str] = []
    coordinator.async_add_event_listener(lambda tp, msg: got.append(tp))
    await coordinator._handle_message(_frame(tp="evt:status", ct=77.5))
    assert got == []
    assert coordinator.data["ct"] == 77.5


async def test_non_event_and_malformed_types_not_dispatched(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    got: list[str] = []
    coordinator.async_add_event_listener(lambda tp, msg: got.append(tp))
    for msg in ({"tp": "res:whatever"}, {"tp": 5}, {"foo": 1}, {"tp": None}, {"tp": "evt"}, {"tp": "EVT:x"}):
        await coordinator._handle_message(json.dumps(msg))
    assert got == []


async def test_raising_listener_does_not_stop_others(
    hass: HomeAssistant, setup_integration: SetupIntegration, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = await setup_integration()
    got: list[str] = []

    def bad(tp: str, msg: dict[str, Any]) -> None:
        raise RuntimeError("listener boom")

    coordinator.async_add_event_listener(lambda tp, msg: got.append("a"))
    coordinator.async_add_event_listener(bad)
    coordinator.async_add_event_listener(lambda tp, msg: got.append("b"))
    with caplog.at_level(logging.DEBUG, logger=COORD_LOGGER):
        await coordinator._handle_message(_frame(tp="evt:x"))
    assert got == ["a", "b"]
    assert any(r.exc_info and "listener boom" in str(r.exc_info[1]) for r in caplog.records)
    # coordinator still processes later messages
    await coordinator._handle_message(_frame(tp="evt:y"))
    assert got == ["a", "b", "a", "b"]


async def test_unsubscribe_semantics(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    coordinator = await setup_integration()
    got: list[str] = []
    unsub_a = coordinator.async_add_event_listener(lambda tp, msg: got.append("a"))
    unsub_b = coordinator.async_add_event_listener(lambda tp, msg: got.append("b"))
    unsub_a()
    await coordinator._handle_message(_frame(tp="evt:x"))
    assert got == ["b"]
    unsub_a()  # twice: no error
    unsub_a()
    await coordinator._handle_message(_frame(tp="evt:x"))
    assert got == ["b", "b"]
    unsub_b()
    await coordinator._handle_message(_frame(tp="evt:x"))
    assert got == ["b", "b"]


async def test_same_listener_registered_twice_unsubscribes_one(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    got: list[str] = []

    def listener(tp: str, msg: dict[str, Any]) -> None:
        got.append(tp)

    unsub1 = coordinator.async_add_event_listener(listener)
    coordinator.async_add_event_listener(listener)
    unsub1()
    await coordinator._handle_message(_frame(tp="evt:x"))
    assert got == ["evt:x"]


async def test_unsubscribe_during_dispatch(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    coordinator = await setup_integration()
    got: list[str] = []
    unsubs: dict[str, Callable[[], None]] = {}

    def first(tp: str, msg: dict[str, Any]) -> None:
        got.append("first")
        unsubs["first"]()  # remove self
        unsubs["third"]()  # remove a later listener

    unsubs["first"] = coordinator.async_add_event_listener(first)
    coordinator.async_add_event_listener(lambda tp, msg: got.append("second"))
    unsubs["third"] = coordinator.async_add_event_listener(lambda tp, msg: got.append("third"))
    await coordinator._handle_message(_frame(tp="evt:x"))
    # dispatch iterates a copy: no skipped listener, no exception
    assert got == ["first", "second", "third"]
    await coordinator._handle_message(_frame(tp="evt:x"))
    assert got[3:] == ["second"]


async def test_listener_added_during_dispatch_not_called_in_same_round(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    got: list[str] = []

    def adder(tp: str, msg: dict[str, Any]) -> None:
        got.append("adder")
        coordinator.async_add_event_listener(lambda t, m: got.append("new"))

    unsub = coordinator.async_add_event_listener(adder)
    await coordinator._handle_message(_frame(tp="evt:x"))
    assert got == ["adder"]
    unsub()
    await coordinator._handle_message(_frame(tp="evt:x"))
    assert got == ["adder", "new"]


async def test_shutdown_clears_listeners(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    coordinator = await setup_integration()
    got: list[str] = []
    unsub = coordinator.async_add_event_listener(lambda tp, msg: got.append(tp))
    await coordinator.async_shutdown()
    assert coordinator._event_listeners == []
    coordinator._dispatch_event("evt:x", {"tp": "evt:x"})
    assert got == []
    unsub()  # unsubscribing after shutdown is harmless


async def test_rid_bearing_response_unaffected(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    import asyncio

    coordinator = await setup_integration()
    got: list[str] = []
    coordinator.async_add_event_listener(lambda tp, msg: got.append(tp))
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    coordinator._pending_requests["rid-1"] = fut
    await coordinator._handle_message(_frame(tp="res:profiles:select", rid="rid-1", success=True))
    assert fut.done() and fut.result()["tp"] == "res:profiles:select"
    assert "rid-1" not in coordinator._pending_requests
    assert got == []

    # a rid-bearing event is still resolved AND dispatched
    fut2: asyncio.Future = asyncio.get_running_loop().create_future()
    coordinator._pending_requests["rid-2"] = fut2
    await coordinator._handle_message(_frame(tp="evt:odd", rid="rid-2"))
    assert fut2.done()
    assert got == ["evt:odd"]


async def test_profiles_and_ota_responses_still_processed(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await coordinator._handle_message(
        _frame(tp="res:profiles:list", rid="x", profiles=[{"label": "New", "id": "nid"}])
    )
    assert coordinator.profiles == {"New": "nid"}
    await coordinator._handle_message(_frame(tp="res:ota-settings", foo=1))
    assert coordinator.ota_settings.get("foo") == 1
