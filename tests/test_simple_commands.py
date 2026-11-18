"""Step 9: Tare Scale, Flush Stop, Clear Process buttons and the Steam Heater switch.

Expectations follow the specification (plan.md, Step 9) and the audited behaviour:

* Tare Scale exists only when ``hs`` is true (``bc`` alone does not create it); the press sends
  exactly ``{"tp": "req:scale:tare"}`` without ``rid``.
* Flush Stop always exists, sends ``req:flush:stop`` with ``rid`` and awaits the response.
* Clear Process always exists and sends ``req:process:clear`` without ``rid``.
* Steam Heater switch exists only with ``db`` true; the firmware only has a toggle, so a toggle is sent
  only when the effective state differs from the requested one; in standby only ``turn_on`` raises.
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
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.gaggimate.coordinator import GaggiMateCoordinator

from .conftest import SetupIntegration, settle, wait_until
from .fake_server import FakeGaggiMate, load_json

HOST = "127.0.0.1"
OWN_LOGGERS = (
    "custom_components.gaggimate",
    "homeassistant.components.button",
    "homeassistant.components.switch",
    "homeassistant.helpers.entity",
)
TOGGLE = "req:steam:toggle"
SETTLE_PATH = "custom_components.gaggimate.coordinator.STEAM_TOGGLE_SETTLE"


@pytest.fixture(autouse=True)
def _no_keepalive(fake_gaggimate: FakeGaggiMate) -> None:
    fake_gaggimate.keepalive = False


def _own_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        f"{r.name}: {r.getMessage()}"
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith(OWN_LOGGERS)
    ]


def _use_frames(
    fake: FakeGaggiMate,
    name: str = "single_boiler_idle",
    state: dict[str, Any] | None = None,
    telemetry: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
) -> None:
    data = load_json(name)
    slow = {**data["state"], **(state or {})}
    for key in drop:
        slow.pop(key, None)
    fake.state = slow
    fake.telemetry = {**data["telemetry"], **(telemetry or {})}


def _eid(hass: HomeAssistant, domain: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, "gaggimate", f"{HOST}_{key}")


def _need(hass: HomeAssistant, domain: str, key: str) -> str:
    entity_id = _eid(hass, domain, key)
    assert entity_id is not None, f"{domain} {key} not registered"
    return entity_id


async def _press(hass: HomeAssistant, key: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": _need(hass, "button", key)}, blocking=True)


async def _switch(hass: HomeAssistant, service: str) -> None:
    await hass.services.async_call(
        "switch", service, {"entity_id": _need(hass, "switch", "steam_heater")}, blocking=True
    )


async def _push(
    fake: FakeGaggiMate, coordinator: GaggiMateCoordinator, *, telemetry: bool = False, **changes: Any
) -> None:
    """Push a partial frame and wait until the coordinator processed it (keepalive is off)."""
    updates = 0

    def _count() -> None:
        nonlocal updates
        updates += 1

    unsubscribe = coordinator.async_add_listener(_count)
    try:
        await (fake.push_telemetry(**changes) if telemetry else fake.push_state(**changes))
        await wait_until(lambda: updates >= 1)
    finally:
        unsubscribe()
    await settle(0.02)


def _toggles(fake: FakeGaggiMate) -> list[dict[str, Any]]:
    return fake.requests_of(TOGGLE)


# -- Tare Scale ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        {"hs": False, "bc": False},
        {"hs": False, "bc": True},  # BLE scale only: the firmware tare reaches hardware scales only
        {"bc": True},
    ],
)
async def test_tare_absent_without_hs(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, state: dict[str, Any]
) -> None:
    _use_frames(fake_gaggimate, state=state)
    await setup_integration()
    assert _eid(hass, "button", "tare_scale") is None


async def test_tare_absent_when_hs_key_missing(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, drop=("hs",))
    await setup_integration()
    assert _eid(hass, "button", "tare_scale") is None


async def test_tare_present_with_hs_and_sends_bare_message(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, state={"hs": True})
    await setup_integration()
    entity_id = _need(hass, "button", "tare_scale")
    assert er.async_get(hass).async_get(entity_id).unique_id == f"{HOST}_tare_scale"
    assert er.async_get(hass).async_get(entity_id).disabled_by is None
    fake_gaggimate.clear_requests()
    await _press(hass, "tare_scale")
    await fake_gaggimate.wait_for_request("req:scale:tare")
    await settle()
    assert fake_gaggimate.requests == [{"tp": "req:scale:tare"}]  # exactly once, no rid


async def test_tare_late_gating_is_add_only(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    assert _eid(hass, "button", "tare_scale") is None
    await _push(fake_gaggimate, coordinator, hs=True)
    await hass.async_block_till_done()
    entity_id = _need(hass, "button", "tare_scale")
    # hs dropping again (link lost) does not remove the entity or create a duplicate
    await _push(fake_gaggimate, coordinator, hs=False)
    await hass.async_block_till_done()
    assert _need(hass, "button", "tare_scale") == entity_id
    await _push(fake_gaggimate, coordinator, hs=True)
    await hass.async_block_till_done()
    assert len([e for e in er.async_get(hass).entities.values() if e.unique_id == f"{HOST}_tare_scale"]) == 1


async def test_tare_bc_flip_alone_does_not_create(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await _push(fake_gaggimate, coordinator, bc=True)
    await hass.async_block_till_done()
    assert _eid(hass, "button", "tare_scale") is None


# -- Flush Stop ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("frames", ["single_boiler_idle", "dual_boiler_idle", "waiting_controller"])
async def test_flush_stop_always_present(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, frames: str
) -> None:
    _use_frames(fake_gaggimate, frames)
    await setup_integration()
    entity_id = _need(hass, "button", "flush_stop")
    assert er.async_get(hass).async_get(entity_id).unique_id == f"{HOST}_flush_stop"


async def test_flush_stop_sends_with_rid_and_awaits_response(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _press(hass, "flush_stop")
    [request] = fake_gaggimate.requests
    assert request["tp"] == "req:flush:stop"
    assert request["rid"]
    assert set(request) == {"tp", "rid"}


def _answer_flush_stop(fake: FakeGaggiMate, **fields: Any) -> None:
    original = fake._handle_request

    async def handler(ws: Any, req: dict[str, Any]) -> None:
        if req.get("tp") == "req:flush:stop":
            fake.requests.append(req)
            await fake._respond(ws, req, "res:flush:stop", **fields)
        else:
            await original(ws, req)

    fake._handle_request = handler  # type: ignore[method-assign]


async def test_flush_stop_success_false_raises(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    _answer_flush_stop(fake_gaggimate, success=False)
    with pytest.raises(HomeAssistantError, match="Flush stop failed"):
        await _press(hass, "flush_stop")


async def test_flush_stop_error_field_raises(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    _answer_flush_stop(fake_gaggimate, error="Not flushing")
    with pytest.raises(HomeAssistantError, match="Not flushing"):
        await _press(hass, "flush_stop")


async def test_flush_stop_waits_for_response(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """The press does not complete before the res frame arrives."""
    await setup_integration()
    release = asyncio.Event()
    original = fake_gaggimate._handle_request

    async def handler(ws: Any, req: dict[str, Any]) -> None:
        if req.get("tp") == "req:flush:stop":
            fake_gaggimate.requests.append(req)
            await release.wait()
            await fake_gaggimate._respond(ws, req, "res:flush:stop", success=True)
        else:
            await original(ws, req)

    fake_gaggimate._handle_request = handler  # type: ignore[method-assign]
    task = asyncio.create_task(_press(hass, "flush_stop"))
    await fake_gaggimate.wait_for_request("req:flush:stop")
    await settle(0.1)
    assert not task.done()
    release.set()
    await asyncio.wait_for(task, 5)


# -- Clear Process / Stop Brew ---------------------------------------------------------------------------


@pytest.mark.parametrize("frames", ["single_boiler_idle", "dual_boiler_idle", "waiting_controller"])
async def test_process_clear_always_present(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, frames: str
) -> None:
    _use_frames(fake_gaggimate, frames)
    await setup_integration()
    entity_id = _need(hass, "button", "process_clear")
    assert er.async_get(hass).async_get(entity_id).unique_id == f"{HOST}_process_clear"


async def test_process_clear_sends_bare_message_and_stop_brew_unchanged(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _press(hass, "process_clear")
    await fake_gaggimate.wait_for_request("req:process:clear")
    await settle()
    assert fake_gaggimate.requests == [{"tp": "req:process:clear"}]
    fake_gaggimate.clear_requests()
    await _press(hass, "brew_stop")
    await fake_gaggimate.wait_for_request("req:process:deactivate")
    await settle()
    assert fake_gaggimate.requests == [{"tp": "req:process:deactivate"}]


# -- Steam Heater switch ---------------------------------------------------------------------------------


async def test_steam_heater_absent_on_single_boiler(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    assert _eid(hass, "switch", "steam_heater") is None


async def test_steam_heater_present_with_db_and_unique_id(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle")
    await setup_integration()
    entity_id = _need(hass, "switch", "steam_heater")
    assert er.async_get(hass).async_get(entity_id).unique_id == f"{HOST}_steam_heater"
    assert hass.states.get(entity_id).state == "off"


async def test_steam_heater_late_flip_adds_it(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    assert _eid(hass, "switch", "steam_heater") is None
    await _push(fake_gaggimate, coordinator, telemetry=True, db=True)
    await hass.async_block_till_done()
    entity_id = _need(hass, "switch", "steam_heater")
    assert hass.states.get(entity_id).state == "off"
    await _push(fake_gaggimate, coordinator, telemetry=True, db=False)  # add-only
    await hass.async_block_till_done()
    assert _need(hass, "switch", "steam_heater") == entity_id


async def test_steam_heater_is_on_follows_sa(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle")
    coordinator = await setup_integration()
    entity_id = _need(hass, "switch", "steam_heater")
    assert hass.states.get(entity_id).state == "off"
    await _push(fake_gaggimate, coordinator, sa=True)
    assert hass.states.get(entity_id).state == "on"
    await _push(fake_gaggimate, coordinator, sa=False)
    assert hass.states.get(entity_id).state == "off"
    await _push(fake_gaggimate, coordinator, sa=None)
    assert hass.states.get(entity_id).state == "unknown"


async def test_steam_heater_unknown_when_sa_absent(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle", drop=("sa",))
    await setup_integration()
    assert hass.states.get(_need(hass, "switch", "steam_heater")).state == "unknown"


@pytest.fixture
def dual(fake_gaggimate: FakeGaggiMate) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle")


async def test_turn_on_when_off_sends_one_toggle(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, dual: None
) -> None:
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _switch(hass, "turn_on")
    await fake_gaggimate.wait_for_request(TOGGLE)
    await settle()
    assert fake_gaggimate.requests == [{"tp": TOGGLE}]  # no rid


async def test_turn_off_when_on_sends_one_toggle(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, dual: None
) -> None:
    coordinator = await setup_integration()
    await _push(fake_gaggimate, coordinator, sa=True)
    fake_gaggimate.clear_requests()
    await _switch(hass, "turn_off")
    await fake_gaggimate.wait_for_request(TOGGLE)
    await settle()
    assert fake_gaggimate.requests == [{"tp": TOGGLE}]


async def test_turn_on_when_already_on_sends_nothing(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, dual: None
) -> None:
    coordinator = await setup_integration()
    await _push(fake_gaggimate, coordinator, sa=True)
    fake_gaggimate.clear_requests()
    await _switch(hass, "turn_on")
    await settle(0.1)
    assert fake_gaggimate.requests == []


async def test_turn_off_when_already_off_sends_nothing(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, dual: None
) -> None:
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _switch(hass, "turn_off")
    await settle(0.1)
    assert fake_gaggimate.requests == []


async def test_rapid_turn_on_sends_one_toggle(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, dual: None
) -> None:
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _switch(hass, "turn_on")
    await _switch(hass, "turn_on")  # sa still false in the last frame, but the toggle is pending
    await settle(0.1)
    assert len(_toggles(fake_gaggimate)) == 1


async def test_concurrent_turn_on_sends_one_toggle(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, dual: None
) -> None:
    await setup_integration()
    fake_gaggimate.clear_requests()
    await asyncio.gather(_switch(hass, "turn_on"), _switch(hass, "turn_on"), _switch(hass, "turn_on"))
    await settle(0.1)
    assert len(_toggles(fake_gaggimate)) == 1


async def test_on_then_off_before_frame_sends_two_toggles(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, dual: None
) -> None:
    await setup_integration()
    fake_gaggimate.clear_requests()
    await _switch(hass, "turn_on")
    await _switch(hass, "turn_off")
    await fake_gaggimate.wait_for_request(TOGGLE, 2)
    await settle(0.1)
    assert len(_toggles(fake_gaggimate)) == 2


async def test_pending_cleared_once_sa_confirms(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, dual: None
) -> None:
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    await _switch(hass, "turn_on")
    await fake_gaggimate.wait_for_request(TOGGLE)
    await _push(fake_gaggimate, coordinator, sa=True)
    # confirmed on: turn_on is a no-op, turn_off sends a toggle straight away (well inside the window)
    await _switch(hass, "turn_on")
    await settle(0.05)
    assert len(_toggles(fake_gaggimate)) == 1
    await _switch(hass, "turn_off")
    await fake_gaggimate.wait_for_request(TOGGLE, 2)
    # machine reports off again; a new turn_on must work immediately (pending was cleared)
    await _push(fake_gaggimate, coordinator, sa=False)
    await _switch(hass, "turn_on")
    await fake_gaggimate.wait_for_request(TOGGLE, 3)


async def test_window_expiry_returns_to_sa(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, dual: None
) -> None:
    """The machine ignored the toggle (sa stays false): after the window a new turn_on toggles again."""
    with patch(SETTLE_PATH, 0.15):
        await setup_integration()
        fake_gaggimate.clear_requests()
        await _switch(hass, "turn_on")
        await _switch(hass, "turn_on")
        await settle(0.05)
        assert len(_toggles(fake_gaggimate)) == 1
        await asyncio.sleep(0.2)
        await _switch(hass, "turn_on")
        await fake_gaggimate.wait_for_request(TOGGLE, 2)
        await settle(0.05)
        assert len(_toggles(fake_gaggimate)) == 2
        # the switch state itself always reflects sa, not the pending request
        assert hass.states.get(_need(hass, "switch", "steam_heater")).state == "off"


async def test_turn_on_in_standby_raises_and_sends_nothing(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle", state={"m": 0})
    await setup_integration()
    fake_gaggimate.clear_requests()
    with pytest.raises(HomeAssistantError, match="Steam heater cannot be changed in standby"):
        await _switch(hass, "turn_on")
    await settle(0.1)
    assert fake_gaggimate.requests == []


async def test_turn_off_in_standby_when_off_is_silent_noop(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle", state={"m": 0})
    await setup_integration()
    fake_gaggimate.clear_requests()
    caplog.clear()
    await _switch(hass, "turn_off")
    await settle(0.1)
    assert fake_gaggimate.requests == []
    assert _own_warnings(caplog) == []


async def test_turn_off_in_standby_when_on_raises(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """sa true while in standby: turning off would need a toggle, which the firmware does not allow."""
    _use_frames(fake_gaggimate, "dual_boiler_idle", state={"m": 0, "sa": True})
    await setup_integration()
    fake_gaggimate.clear_requests()
    with pytest.raises(HomeAssistantError, match="standby"):
        await _switch(hass, "turn_off")
    assert fake_gaggimate.requests == []


@pytest.mark.parametrize("service", ["turn_on", "turn_off"])
async def test_sa_unknown_raises(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, service: str
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle", state={"sa": None})
    await setup_integration()
    fake_gaggimate.clear_requests()
    with pytest.raises(HomeAssistantError, match="Steam heater state unknown"):
        await _switch(hass, service)
    assert fake_gaggimate.requests == []


async def test_sa_absent_raises(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle", drop=("sa",))
    await setup_integration()
    with pytest.raises(HomeAssistantError, match="Steam heater state unknown"):
        await _switch(hass, "turn_on")
    assert _toggles(fake_gaggimate) == []


async def test_disconnected_raises(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, dual: None
) -> None:
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    with patch.object(coordinator, "_schedule_reconnect"):
        real_ws = coordinator._ws
        coordinator._ws = None
        try:
            with pytest.raises(HomeAssistantError, match="not connected"):
                await _switch(hass, "turn_on")
        finally:
            coordinator._ws = real_ws
    assert coordinator._steam_pending is None
    assert fake_gaggimate.requests == []


async def test_failed_send_reverts_pending(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, dual: None
) -> None:
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    with patch.object(coordinator, "send_message", side_effect=UpdateFailed("boom")):
        with pytest.raises(HomeAssistantError, match="boom"):
            await _switch(hass, "turn_on")
    assert coordinator._steam_pending is None
    await _switch(hass, "turn_on")  # next call can send again
    await fake_gaggimate.wait_for_request(TOGGLE)
    assert len(_toggles(fake_gaggimate)) == 1


async def test_cancellation_during_send_reverts_pending(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, dual: None
) -> None:
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    started = asyncio.Event()

    async def hang(message: dict[str, Any]) -> None:
        started.set()
        await asyncio.Event().wait()

    with patch.object(coordinator, "send_message", side_effect=hang):
        task = asyncio.create_task(coordinator.set_steam_heater(True))
        await asyncio.wait_for(started.wait(), 5)
        assert coordinator._steam_pending is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert coordinator._steam_pending is None
    await coordinator.set_steam_heater(True)
    await fake_gaggimate.wait_for_request(TOGGLE)
    assert len(_toggles(fake_gaggimate)) == 1


# -- unchanged entities and logging ------------------------------------------------------------------------


async def test_existing_buttons_and_power_switch_unchanged(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_integration()
    for key in ("brew_start", "brew_stop", "flush"):
        assert er.async_get(hass).async_get(_need(hass, "button", key)).unique_id == f"{HOST}_{key}"
    power = _need(hass, "switch", "power")
    assert hass.states.get(power).state == "on"  # m == 1
    fake_gaggimate.clear_requests()
    await hass.services.async_call("switch", "turn_off", {"entity_id": power}, blocking=True)
    [request] = await fake_gaggimate.wait_for_request("req:change-mode")
    assert request["mode"] == 0
    await hass.services.async_call("switch", "turn_on", {"entity_id": power}, blocking=True)
    await fake_gaggimate.wait_for_request("req:change-mode", 2)
    assert fake_gaggimate.requests_of("req:change-mode")[1]["mode"] == 1
    assert _own_warnings(caplog) == []


async def test_no_warnings_on_happy_paths(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _use_frames(fake_gaggimate, "dual_boiler_idle", state={"hs": True})
    coordinator = await setup_integration()
    caplog.clear()
    for key in ("tare_scale", "flush_stop", "process_clear"):
        await _press(hass, key)
    await _switch(hass, "turn_on")
    await _push(fake_gaggimate, coordinator, sa=True)
    await _switch(hass, "turn_off")
    await settle(0.1)
    assert _own_warnings(caplog) == []
