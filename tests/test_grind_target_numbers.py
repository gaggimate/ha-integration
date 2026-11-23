"""Step 12: Grind Target Duration / Grind Target Weight numbers.

Expectations follow the specification (plan.md, Step 12), not the implementation:
* number.<host>_grind_target_duration: seconds (gtd ms / 1000), step 1, 5-300; created when gtd is present;
  available only while gt == 0.
* number.<host>_grind_target_weight: grams (gtv), step 0.5, 5-250; created while bta == 1 and gtv is present
  (add-only); available only while gt == 1. gt absent/unknown -> both unavailable.
* The firmware only has req:raise-grind-target / req:lower-grind-target (one step each, clamped); the
  coordinator sends |delta| bare messages and the value only shows in a slow frame ~0.2-0.5 s later.
* One lock serialises calls; the last-started call wins; stalls, gt flips, disconnects and timeouts raise.
The fake server applies the raise/lower messages to gtd/gtv (see tests/fake_server.py). Timing constants of the
coordinator are patched small to keep the tests fast.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed

from .conftest import SetupIntegration, settle, wait_until
from .fake_server import FakeGaggiMate

HOST = "127.0.0.1"
OWN_LOGGERS = ("custom_components.gaggimate", "homeassistant.components.number")
RAISE = "req:raise-grind-target"
LOWER = "req:lower-grind-target"
CHANGE = "req:change-grind-target"
COORD = "custom_components.gaggimate.coordinator"

pytestmark = pytest.mark.frames("dual_boiler_idle")  # gt 0, gtd 25000, gtv 40.0, bta 1


# -- fixtures and helpers ----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fast_timing(fake_gaggimate: FakeGaggiMate) -> Iterator[None]:
    """Small timing constants; the fake reports a change 0.15 s after it happened."""
    fake_gaggimate.grind_frame_delay = 0.15
    with (
        patch(f"{COORD}.GRIND_TARGET_MESSAGE_DELAY", 0.01),
        patch(f"{COORD}.GRIND_TARGET_SETTLE", 0.8),
        patch(f"{COORD}.GRIND_TARGET_STALL", 1.0),
    ):
        yield


def _eid(hass: HomeAssistant, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("number", "gaggimate", f"{HOST}_{key}")


def _state(hass: HomeAssistant, key: str) -> Any:
    entity_id = _eid(hass, key)
    return hass.states.get(entity_id) if entity_id else None


def _own_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        f"{r.name}: {r.getMessage()}"
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith(OWN_LOGGERS)
    ]


def _prime(fake: FakeGaggiMate, *, drop: tuple[str, ...] = (), **state: Any) -> None:
    merged = {**fake.state, **state}
    fake.state = {k: v for k, v in merged.items() if k not in drop}


def _sent(fake: FakeGaggiMate) -> list[dict[str, Any]]:
    return [r for r in fake.requests if r.get("tp") in (RAISE, LOWER)]


def _counts(fake: FakeGaggiMate) -> tuple[int, int]:
    return len(fake.requests_of(RAISE)), len(fake.requests_of(LOWER))


async def _push(fake: FakeGaggiMate, coordinator: Any, **changes: Any) -> None:
    await fake.push_state(**changes)
    await wait_until(lambda: all(k in coordinator.data and coordinator.data[k] == v for k, v in changes.items()))
    await settle(0.02)


async def _set(hass: HomeAssistant, key: str, value: float) -> None:
    await hass.services.async_call(
        "number", "set_value", {"entity_id": _eid(hass, key), "value": value}, blocking=True
    )


async def _reported(coordinator: Any, key: str, value: float) -> None:
    await wait_until(lambda: coordinator.data.get(key) == value, timeout=3)


# -- creation and gating -----------------------------------------------------------------------------


async def test_both_numbers_created_with_metadata(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    await setup_integration()
    registry = er.async_get(hass)
    for key, name, unit, lo, hi, step in (
        ("grind_target_duration", "Grind Target Duration", "s", 5, 300, 1),
        ("grind_target_weight", "Grind Target Weight", "g", 5, 250, 0.5),
    ):
        entity_id = _eid(hass, key)
        assert entity_id is not None, key
        entry = registry.async_get(entity_id)
        assert entry.unique_id == f"{HOST}_{key}"
        assert entry.domain == "number"
        assert entry.disabled_by is None
        state = hass.states.get(entity_id)
        assert state.attributes["friendly_name"].endswith(name)
        assert state.attributes["unit_of_measurement"] == unit
        assert state.attributes["min"] == lo
        assert state.attributes["max"] == hi
        assert state.attributes["step"] == step


async def test_weight_absent_when_bta_zero(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prime(fake_gaggimate, bta=0)
    await setup_integration()
    assert _eid(hass, "grind_target_duration") is not None
    assert _eid(hass, "grind_target_weight") is None


@pytest.mark.parametrize("bta", [None, "1", 2, True])
async def test_weight_requires_bta_int_one(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, bta: Any
) -> None:
    _prime(fake_gaggimate, bta=bta)
    await setup_integration()
    if bta is True:  # JSON true is equal to 1 in Python; the firmware sends an integer, so accept either outcome
        return
    assert _eid(hass, "grind_target_weight") is None


async def test_weight_absent_without_gtv_even_with_bta(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prime(fake_gaggimate, drop=("gtv",))
    await setup_integration()
    assert _eid(hass, "grind_target_weight") is None
    assert _eid(hass, "grind_target_duration") is not None


async def test_duration_absent_without_gtd(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prime(fake_gaggimate, drop=("gtd",))
    await setup_integration()
    assert _eid(hass, "grind_target_duration") is None
    assert _eid(hass, "grind_target_weight") is not None


async def test_duration_added_when_gtd_arrives_late_and_stays(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prime(fake_gaggimate, drop=("gtd",))
    coordinator = await setup_integration()
    assert _eid(hass, "grind_target_duration") is None
    await _push(fake_gaggimate, coordinator, gtd=30000)
    await wait_until(lambda: _state(hass, "grind_target_duration") is not None)
    assert _state(hass, "grind_target_duration").state == "30.0"


async def test_weight_added_when_bta_and_gtv_arrive_late_and_stays(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prime(fake_gaggimate, drop=("gtv",), bta=0)
    coordinator = await setup_integration()
    assert _eid(hass, "grind_target_weight") is None
    await _push(fake_gaggimate, coordinator, bta=1)
    assert _eid(hass, "grind_target_weight") is None  # still no gtv
    await _push(fake_gaggimate, coordinator, gtv=36.5, gt=1)
    await wait_until(lambda: _state(hass, "grind_target_weight") is not None)
    assert _state(hass, "grind_target_weight").state == "36.5"
    # add-only: bta dropping back does not remove it
    await _push(fake_gaggimate, coordinator, bta=0)
    assert _state(hass, "grind_target_weight") is not None
    assert _state(hass, "grind_target_weight").state == "36.5"


# -- values and availability -------------------------------------------------------------------------


async def test_values_and_units(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    assert _state(hass, "grind_target_duration").state == "25.0"  # 25000 ms
    await _push(fake_gaggimate, coordinator, gt=1)
    assert _state(hass, "grind_target_weight").state == "40.0"


async def test_values_follow_frames(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await _push(fake_gaggimate, coordinator, gtd=12500, gtv=18.5)
    assert float(_state(hass, "grind_target_duration").state) == 12.5
    await _push(fake_gaggimate, coordinator, gt=1)
    assert float(_state(hass, "grind_target_weight").state) == 18.5


@pytest.mark.parametrize("bad", [None, "abc", True])
async def test_non_numeric_values_are_unknown(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, bad: Any
) -> None:
    coordinator = await setup_integration()
    await _push(fake_gaggimate, coordinator, gtd=bad, gtv=bad)
    assert _state(hass, "grind_target_duration").state in ("unknown", "unavailable")
    assert _state(hass, "grind_target_weight").state in ("unknown", "unavailable")


async def test_availability_follows_gt(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()  # gt 0
    assert _state(hass, "grind_target_duration").state != "unavailable"
    assert _state(hass, "grind_target_weight").state == "unavailable"
    await _push(fake_gaggimate, coordinator, gt=1)
    assert _state(hass, "grind_target_duration").state == "unavailable"
    assert _state(hass, "grind_target_weight").state == "40.0"
    await _push(fake_gaggimate, coordinator, gt=0)
    assert _state(hass, "grind_target_duration").state == "25.0"
    assert _state(hass, "grind_target_weight").state == "unavailable"


async def test_both_unavailable_when_gt_absent(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prime(fake_gaggimate, drop=("gt",))
    await setup_integration()
    assert _state(hass, "grind_target_duration").state == "unavailable"
    assert _state(hass, "grind_target_weight").state == "unavailable"


@pytest.mark.parametrize("gt", [None, 2, "0"])
async def test_both_unavailable_when_gt_unknown(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, gt: Any
) -> None:
    coordinator = await setup_integration()
    await _push(fake_gaggimate, coordinator, gt=gt)
    assert _state(hass, "grind_target_duration").state == "unavailable"
    assert _state(hass, "grind_target_weight").state == "unavailable"


# -- setting values: deltas --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "gt", "start", "tkey", "value", "expected", "raises", "lowers", "scale"),
    [
        ("grind_target_duration", 0, 25000, "gtd", 30, 30000, 5, 0, 1000),
        ("grind_target_duration", 0, 25000, "gtd", 20, 20000, 0, 5, 1000),
        ("grind_target_weight", 1, 40.0, "gtv", 43, 43.0, 6, 0, 1),
        ("grind_target_weight", 1, 40.0, "gtv", 38.5, 38.5, 0, 3, 1),
    ],
)
async def test_set_value_delta_up_and_down(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
    key: str,
    gt: int,
    start: float,
    tkey: str,
    value: float,
    expected: float,
    raises: int,
    lowers: int,
    scale: int,
) -> None:
    _prime(fake_gaggimate, gt=gt)
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    caplog.clear()
    await _set(hass, key, value)
    assert _counts(fake_gaggimate) == (raises, lowers)
    # bare messages: no rid, no payload
    assert all(set(r) == {"tp"} for r in _sent(fake_gaggimate))
    assert fake_gaggimate.state[tkey] == expected
    await _reported(coordinator, tkey, expected)
    assert float(_state(hass, key).state) == expected / scale
    assert fake_gaggimate.requests_of(CHANGE) == []
    assert _own_warnings(caplog) == []


async def test_messages_are_spaced_by_the_delay(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    with patch(f"{COORD}.GRIND_TARGET_MESSAGE_DELAY", 0.05):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await coordinator.set_grind_target("duration", 30)  # 5 messages
        elapsed = loop.time() - t0
    assert elapsed >= 0.2  # at least 4 inter-message gaps of 50 ms
    assert _counts(fake_gaggimate) == (5, 0)


async def test_set_current_value_sends_nothing(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _set(hass, "grind_target_duration", 25)
    assert _sent(fake_gaggimate) == []


async def test_set_current_weight_sends_nothing(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prime(fake_gaggimate, gt=1)
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    await coordinator.set_grind_target("weight", 40.0)
    await coordinator.set_grind_target("weight", 40.2)  # rounds to 0 steps
    await settle()
    assert _sent(fake_gaggimate) == []


async def test_large_delta_converges_and_counts_match(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prime(fake_gaggimate, gtd=10000)
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    await coordinator.set_grind_target("duration", 60)
    assert _counts(fake_gaggimate) == (50, 0)
    assert fake_gaggimate.state["gtd"] == 60000
    await _reported(coordinator, "gtd", 60000)


@pytest.mark.parametrize(
    ("value", "expected_g"),
    [(42.3, 42.5), (42.2, 42.0), (40.24, 40.0), (40.26, 40.5), (39.8, 40.0), (39.7, 39.5)],
)
async def test_off_grid_weight_rounds_to_nearest_step(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, value: float, expected_g: float
) -> None:
    _prime(fake_gaggimate, gt=1)
    coordinator = await setup_integration()
    await coordinator.set_grind_target("weight", value)
    assert fake_gaggimate.state["gtv"] == expected_g


@pytest.mark.parametrize(("value", "expected_s"), [(20.4, 20), (20.6, 21), (25.4, 25)])
async def test_off_grid_duration_rounds_to_nearest_second(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, value: float, expected_s: int
) -> None:
    coordinator = await setup_integration()
    await coordinator.set_grind_target("duration", value)
    assert fake_gaggimate.state["gtd"] == expected_s * 1000


# -- boundaries --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "gt", "tkey", "start", "request_value", "expected", "msgs", "tp"),
    [
        ("duration", 0, "gtd", 298000, 400, 300000, 2, RAISE),
        ("duration", 0, "gtd", 300000, 400, 300000, 0, RAISE),
        ("duration", 0, "gtd", 7000, 1, 5000, 2, LOWER),
        ("duration", 0, "gtd", 5000, -50, 5000, 0, LOWER),
        ("duration", 0, "gtd", 25000, 300, 300000, 275, RAISE),
        ("weight", 1, "gtv", 248.0, 999, 250.0, 4, RAISE),
        ("weight", 1, "gtv", 250.0, 999, 250.0, 0, RAISE),
        ("weight", 1, "gtv", 6.0, 0, 5.0, 2, LOWER),
        ("weight", 1, "gtv", 5.0, -10, 5.0, 0, LOWER),
    ],
)
async def test_clamping_beyond_range_via_coordinator(
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    kind: str,
    gt: int,
    tkey: str,
    start: float,
    request_value: float,
    expected: float,
    msgs: int,
    tp: str,
) -> None:
    _prime(fake_gaggimate, gt=gt, **{tkey: start})
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    with patch(f"{COORD}.GRIND_TARGET_MESSAGE_DELAY", 0.0):
        await coordinator.set_grind_target(kind, request_value)
    assert len(fake_gaggimate.requests_of(tp)) == msgs
    assert len(_sent(fake_gaggimate)) == msgs
    assert fake_gaggimate.state[tkey] == expected


@pytest.mark.parametrize(
    ("key", "gt", "value"),
    [
        ("grind_target_duration", 0, 301),
        ("grind_target_duration", 0, 4),
        ("grind_target_weight", 1, 250.5),
        ("grind_target_weight", 1, 4.5),
    ],
)
async def test_service_rejects_out_of_range(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    key: str,
    gt: int,
    value: float,
) -> None:
    _prime(fake_gaggimate, gt=gt)
    await setup_integration()
    fake_gaggimate.clear_requests()
    with pytest.raises((HomeAssistantError, vol.Invalid)):
        await _set(hass, key, value)
    assert _sent(fake_gaggimate) == []


@pytest.mark.parametrize(
    ("key", "gt", "tkey", "value", "expected"),
    [
        ("grind_target_duration", 0, "gtd", 5, 5000),
        ("grind_target_duration", 0, "gtd", 300, 300000),
        ("grind_target_weight", 1, "gtv", 5, 5.0),
        ("grind_target_weight", 1, "gtv", 250, 250.0),
    ],
)
async def test_service_accepts_limits(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    key: str,
    gt: int,
    tkey: str,
    value: float,
    expected: float,
) -> None:
    _prime(fake_gaggimate, gt=gt)
    await setup_integration()
    with patch(f"{COORD}.GRIND_TARGET_MESSAGE_DELAY", 0.0):
        await _set(hass, key, value)
    assert fake_gaggimate.state[tkey] == expected


# -- errors ------------------------------------------------------------------------------------------


async def test_unknown_kind_raises_value_error(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    with pytest.raises(ValueError):
        await coordinator.set_grind_target("volume", 20)
    assert _sent(fake_gaggimate) == []
    assert not coordinator._grind_target_lock.locked()


@pytest.mark.parametrize(
    ("kind", "gt"),
    [("weight", 0), ("duration", 1)],
)
async def test_gt_mismatch_at_start_raises_and_sends_nothing(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, kind: str, gt: int
) -> None:
    _prime(fake_gaggimate, gt=gt)
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    with pytest.raises(UpdateFailed):
        await coordinator.set_grind_target(kind, 30)
    assert _sent(fake_gaggimate) == []
    assert not coordinator._grind_target_lock.locked()


@pytest.mark.parametrize("kind", ["duration", "weight"])
@pytest.mark.parametrize("gt", [None, 2])
async def test_gt_unknown_raises(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, kind: str, gt: Any
) -> None:
    coordinator = await setup_integration()
    await _push(fake_gaggimate, coordinator, gt=gt)
    fake_gaggimate.clear_requests()
    with pytest.raises(UpdateFailed):
        await coordinator.set_grind_target(kind, 30)
    assert _sent(fake_gaggimate) == []


async def test_gt_absent_raises(fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration) -> None:
    _prime(fake_gaggimate, drop=("gt",))
    coordinator = await setup_integration()
    with pytest.raises(UpdateFailed):
        await coordinator.set_grind_target("duration", 30)
    assert _sent(fake_gaggimate) == []


@pytest.mark.parametrize("bad", [None, "abc", True])
async def test_missing_or_non_numeric_current_value_raises(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, bad: Any
) -> None:
    coordinator = await setup_integration()
    await _push(fake_gaggimate, coordinator, gtd=bad)
    fake_gaggimate.clear_requests()
    with pytest.raises(UpdateFailed):
        await coordinator.set_grind_target("duration", 30)
    assert _sent(fake_gaggimate) == []
    assert not coordinator._grind_target_lock.locked()


async def test_missing_current_value_key_absent_raises(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prime(fake_gaggimate, drop=("gtd",))
    coordinator = await setup_integration()
    with pytest.raises(UpdateFailed):
        await coordinator.set_grind_target("duration", 30)


async def test_service_error_reaches_caller(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prime(fake_gaggimate, gtd=None)
    coordinator = await setup_integration()
    await wait_until(lambda: "gtd" in coordinator.data)
    with pytest.raises(UpdateFailed):
        await _set(hass, "grind_target_duration", 30)


async def test_gt_flip_mid_sequence_raises(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    with patch(f"{COORD}.GRIND_TARGET_MESSAGE_DELAY", 0.03):
        task = asyncio.create_task(coordinator.set_grind_target("duration", 200))  # 175 raises
        await fake_gaggimate.wait_for_request(RAISE, count=3)
        await _push(fake_gaggimate, coordinator, gt=1)
        with pytest.raises(UpdateFailed):
            await asyncio.wait_for(task, 5)
    sent_at_raise = len(_sent(fake_gaggimate))
    await settle(0.2)
    assert len(_sent(fake_gaggimate)) == sent_at_raise  # nothing more sent after the flip was seen
    assert sent_at_raise < 175
    assert not coordinator._grind_target_lock.locked()


async def test_gt_flip_during_settle_wait_raises_or_returns_without_hanging(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    task = asyncio.create_task(coordinator.set_grind_target("duration", 28))
    await fake_gaggimate.wait_for_request(RAISE, count=3)
    await _push(fake_gaggimate, coordinator, gt=1)
    try:
        await asyncio.wait_for(task, 5)
    except UpdateFailed:
        pass
    assert not coordinator._grind_target_lock.locked()


async def test_disconnect_mid_sequence_raises_and_does_not_loop(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    with (
        patch(f"{COORD}.WS_RECONNECT_DELAYS", [30.0]),
        patch(f"{COORD}.GRIND_TARGET_MESSAGE_DELAY", 0.03),
    ):
        task = asyncio.create_task(coordinator.set_grind_target("duration", 200))
        await fake_gaggimate.wait_for_request(RAISE, count=3)
        await fake_gaggimate.drop_connections()
        with pytest.raises(UpdateFailed):
            await asyncio.wait_for(task, 5)
    assert len(_sent(fake_gaggimate)) < 175
    assert not coordinator._grind_target_lock.locked()


async def test_disconnected_before_start_raises(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    with patch.object(coordinator, "_schedule_reconnect"):
        real_ws = coordinator._ws
        coordinator._ws = None
        try:
            with pytest.raises(UpdateFailed, match="not connected"):
                await coordinator.set_grind_target("duration", 30)
        finally:
            coordinator._ws = real_ws
    assert _sent(fake_gaggimate) == []
    assert not coordinator._grind_target_lock.locked()


@pytest.mark.parametrize("mode", ["drop", "stall"])
async def test_stall_raises(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, mode: str
) -> None:
    coordinator = await setup_integration()
    fake_gaggimate.grind_mode = mode  # the reported value never changes
    fake_gaggimate.clear_requests()
    with patch(f"{COORD}.GRIND_TARGET_MESSAGE_DELAY", 0.03), patch(f"{COORD}.GRIND_TARGET_STALL", 0.5):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        call = coordinator.set_grind_target("duration", 200)  # 175 steps = 5 s
        with pytest.raises(UpdateFailed, match="stopped moving"):
            await asyncio.wait_for(call, 10)
        assert loop.time() - t0 < 3
    assert 0 < len(_sent(fake_gaggimate)) < 175
    assert not coordinator._grind_target_lock.locked()


async def test_stall_resets_when_value_moves(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """A slow but moving target (frame jitter) must not count as a stall."""
    coordinator = await setup_integration()
    fake_gaggimate.grind_frame_delay = 0.2
    fake_gaggimate.grind_frame_jitter = 0.2
    with patch(f"{COORD}.GRIND_TARGET_MESSAGE_DELAY", 0.03), patch(f"{COORD}.GRIND_TARGET_STALL", 0.8):
        await coordinator.set_grind_target("duration", 60)  # 35 steps ~1 s, several frames
    assert fake_gaggimate.state["gtd"] == 60000
    assert _counts(fake_gaggimate)[1] == 0


async def test_timeout_raises(fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration) -> None:
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    with (
        patch(f"{COORD}.GRIND_TARGET_TIMEOUT", 0.3),
        patch(f"{COORD}.GRIND_TARGET_MESSAGE_DELAY", 0.05),
    ):
        call = coordinator.set_grind_target("duration", 200)
        with pytest.raises(UpdateFailed, match="Timed out"):
            await asyncio.wait_for(call, 5)
    assert 0 < len(_sent(fake_gaggimate)) < 175
    assert not coordinator._grind_target_lock.locked()


async def test_lock_released_after_exception_and_next_call_works(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    fake_gaggimate.grind_mode = "drop"
    with patch(f"{COORD}.GRIND_TARGET_STALL", 0.3), patch(f"{COORD}.GRIND_TARGET_MESSAGE_DELAY", 0.03):
        with pytest.raises(UpdateFailed):
            await coordinator.set_grind_target("duration", 200)
    assert not coordinator._grind_target_lock.locked()
    fake_gaggimate.grind_mode = "normal"
    await asyncio.wait_for(coordinator.set_grind_target("duration", 27), 5)
    assert fake_gaggimate.state["gtd"] == 27000


async def test_lock_released_after_cancellation_and_next_call_works(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    with patch(f"{COORD}.GRIND_TARGET_MESSAGE_DELAY", 0.03):
        task = asyncio.create_task(coordinator.set_grind_target("duration", 200))
        await fake_gaggimate.wait_for_request(RAISE, count=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not coordinator._grind_target_lock.locked()
        # let the already sent steps show up before the next call reads its start value
        await wait_until(lambda: coordinator.data["gtd"] == fake_gaggimate.state["gtd"], timeout=3)
        await asyncio.wait_for(coordinator.set_grind_target("duration", 30), 5)
    assert fake_gaggimate.state["gtd"] == 30000


async def test_waiter_cancelled_while_waiting_for_lock_does_not_break_lock(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    first = asyncio.create_task(coordinator.set_grind_target("duration", 35))
    await fake_gaggimate.wait_for_request(RAISE, count=1)
    waiter = asyncio.create_task(coordinator.set_grind_target("duration", 10))
    await settle(0.02)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.wait_for(first, 5)
    assert not coordinator._grind_target_lock.locked()
    assert fake_gaggimate.state["gtd"] == 35000
    assert _counts(fake_gaggimate)[1] == 0  # the cancelled waiter never sent anything


# -- concurrency -------------------------------------------------------------------------------------


@pytest.mark.parametrize("jitter", [0.0, 0.3])
async def test_two_concurrent_calls_end_at_last_started_target(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, jitter: float
) -> None:
    _prime(fake_gaggimate, gtd=10000)
    coordinator = await setup_integration()
    fake_gaggimate.grind_frame_jitter = jitter
    fake_gaggimate.clear_requests()
    a = asyncio.create_task(coordinator.set_grind_target("duration", 20))
    await asyncio.sleep(0)  # a is first in line for the lock
    b = asyncio.create_task(coordinator.set_grind_target("duration", 25))
    await asyncio.wait_for(asyncio.gather(a, b), 15)
    # 10 -> 20 (10 raises) then 20 -> 25 (5 raises); never a wrong end or a lower
    assert _counts(fake_gaggimate) == (15, 0)
    assert fake_gaggimate.state["gtd"] == 25000
    await _reported(coordinator, "gtd", 25000)
    assert not coordinator._grind_target_lock.locked()


async def test_concurrent_calls_20_and_25_from_default_start(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()  # starts at 25 s
    fake_gaggimate.clear_requests()
    a = asyncio.create_task(coordinator.set_grind_target("duration", 20))
    await asyncio.sleep(0)
    b = asyncio.create_task(coordinator.set_grind_target("duration", 25))
    await asyncio.wait_for(asyncio.gather(a, b), 15)
    assert fake_gaggimate.state["gtd"] == 25000
    assert _counts(fake_gaggimate) == (5, 5)  # 25 -> 20 -> 25
    await _reported(coordinator, "gtd", 25000)


async def test_three_concurrent_calls_last_started_wins(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prime(fake_gaggimate, gtd=10000)
    coordinator = await setup_integration()
    tasks = []
    for target in (30, 15, 22):
        tasks.append(asyncio.create_task(coordinator.set_grind_target("duration", target)))
        await asyncio.sleep(0)
    await asyncio.wait_for(asyncio.gather(*tasks), 20)
    assert fake_gaggimate.state["gtd"] == 22000


async def test_sequential_call_shortly_after_first_returns(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await coordinator.set_grind_target("duration", 20)
    await asyncio.sleep(0.7)
    await coordinator.set_grind_target("duration", 28)
    assert fake_gaggimate.state["gtd"] == 28000
    assert _counts(fake_gaggimate) == (8, 5)


async def test_back_to_back_sequential_calls_start_from_fresh_value(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """The second call follows the first immediately: the settle wait makes its start value current."""
    coordinator = await setup_integration()
    await coordinator.set_grind_target("duration", 20)
    await coordinator.set_grind_target("duration", 24)
    assert fake_gaggimate.state["gtd"] == 24000
    assert _counts(fake_gaggimate) == (4, 5)
    await settle(0.4)
    assert fake_gaggimate.state["gtd"] == 24000  # nothing was sent afterwards


async def test_slow_frames_still_back_to_back_correct(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    fake_gaggimate.grind_frame_delay = 0.5
    with patch(f"{COORD}.GRIND_TARGET_SETTLE", 1.5):
        await coordinator.set_grind_target("duration", 20)
        await coordinator.set_grind_target("duration", 24)
    assert fake_gaggimate.state["gtd"] == 24000


async def test_opposing_calls_end_at_last_started_target(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prime(fake_gaggimate, gtd=10000)
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    a = asyncio.create_task(coordinator.set_grind_target("duration", 20))
    await asyncio.sleep(0)
    b = asyncio.create_task(coordinator.set_grind_target("duration", 5))
    await asyncio.wait_for(asyncio.gather(a, b), 15)
    assert fake_gaggimate.state["gtd"] == 5000
    assert _counts(fake_gaggimate) == (10, 15)
    await _reported(coordinator, "gtd", 5000)


async def test_weight_concurrent_calls_last_started_wins(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _prime(fake_gaggimate, gt=1)
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    a = asyncio.create_task(coordinator.set_grind_target("weight", 45))
    await asyncio.sleep(0)
    b = asyncio.create_task(coordinator.set_grind_target("weight", 42.5))
    await asyncio.wait_for(asyncio.gather(a, b), 15)
    assert fake_gaggimate.state["gtv"] == 42.5
    assert _counts(fake_gaggimate) == (10, 5)


@pytest.mark.parametrize("duration_first", [True, False])
async def test_both_numbers_at_once_mismatching_one_raises_without_deadlock(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, duration_first: bool
) -> None:
    coordinator = await setup_integration()  # gt 0: only the duration can change
    fake_gaggimate.clear_requests()
    calls = [coordinator.set_grind_target("duration", 30), coordinator.set_grind_target("weight", 45)]
    if not duration_first:
        calls.reverse()
    tasks = [asyncio.create_task(c) for c in calls]
    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
    ok = [r for r in results if r is None]
    failed = [r for r in results if isinstance(r, UpdateFailed)]
    assert len(ok) == 1 and len(failed) == 1, results
    assert fake_gaggimate.state["gtd"] == 30000
    assert fake_gaggimate.state["gtv"] == 40.0
    assert all(r["tp"] == RAISE for r in _sent(fake_gaggimate))
    assert len(_sent(fake_gaggimate)) == 5
    assert not coordinator._grind_target_lock.locked()


async def test_both_numbers_via_service_at_once(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    # the weight number is unavailable at gt 0, so HA itself may refuse; either way nothing hangs
    results = await asyncio.wait_for(
        asyncio.gather(
            _set(hass, "grind_target_duration", 30),
            _set(hass, "grind_target_weight", 45),
            return_exceptions=True,
        ),
        10,
    )
    assert results[0] is None
    assert fake_gaggimate.state["gtd"] == 30000
    assert fake_gaggimate.state["gtv"] == 40.0


async def test_no_warnings_on_concurrent_happy_path(
    fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = await setup_integration()
    caplog.clear()
    a = asyncio.create_task(coordinator.set_grind_target("duration", 20))
    await asyncio.sleep(0)
    b = asyncio.create_task(coordinator.set_grind_target("duration", 30))
    await asyncio.wait_for(asyncio.gather(a, b), 15)
    await settle(0.3)
    assert _own_warnings(caplog) == []


# -- regression --------------------------------------------------------------------------------------


async def test_target_temperature_number_still_works(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    entity_id = er.async_get(hass).async_get_entity_id("number", "gaggimate", f"{HOST}_target_temperature_setpoint")
    assert entity_id is not None
    fake_gaggimate.clear_requests()
    await hass.services.async_call("number", "set_value", {"entity_id": entity_id, "value": 91}, blocking=True)
    await settle()
    assert _sent(fake_gaggimate) == []  # does not touch the grind target
