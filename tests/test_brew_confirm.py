"""Step 10: brew confirmation flow (plan.md Step 10 and the audited design).

* ``evt:brew:confirm`` ({tp, warn: [{k, l}]}) turns ``Brew Confirmation Pending`` on; the ``warnings``
  attribute is a list of {key, severity, severity_level}.
* ``Confirm Brew`` sends ``{"tp": "req:process:activate", "ignoreWarnings": true}`` (JSON boolean),
  ``Cancel Brew Confirmation`` sends ``req:brew:confirm:cancel``; both are available only while pending.
* Start Brew still sends exactly ``{"tp": "req:process:activate"}``.
* Cleared by: cancel event (any client), a status frame with process.a == 1, a status whose merged
  ``m`` is present and not BREW (missing ``m`` does not clear), disconnect, availability timeout.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.gaggimate.coordinator import GaggiMateCoordinator

from .conftest import SetupIntegration, settle, wait_until
from .fake_server import FakeGaggiMate, load_json

HOST = "127.0.0.1"
OWN_LOGGERS = (
    "custom_components.gaggimate",
    "homeassistant.components.button",
    "homeassistant.components.binary_sensor",
    "homeassistant.helpers.entity",
)
ACTIVATE = "req:process:activate"
CANCEL = "req:brew:confirm:cancel"
EVT = "evt:brew:confirm"
EVT_CANCEL = "evt:brew:confirm:cancel"
UNAVAILABLE = "unavailable"
WARN_ERR = [{"k": "water_tank_low", "l": 2}]


@pytest.fixture(autouse=True)
def _no_keepalive(fake_gaggimate: FakeGaggiMate) -> None:
    fake_gaggimate.keepalive = False


def _own_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        f"{r.name}: {r.getMessage()}"
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith(OWN_LOGGERS)
    ]


def _eid(hass: HomeAssistant, domain: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, "gaggimate", f"{HOST}_{key}")
    assert entity_id is not None, f"{domain} {key} not registered"
    return entity_id


def _pending(hass: HomeAssistant) -> Any:
    return hass.states.get(_eid(hass, "binary_sensor", "brew_confirm_pending"))


def _btn_state(hass: HomeAssistant, key: str) -> str:
    return hass.states.get(_eid(hass, "button", key)).state


async def _press(hass: HomeAssistant, key: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": _eid(hass, "button", key)}, blocking=True)


async def _raise_pending(fake: FakeGaggiMate, coordinator: GaggiMateCoordinator, warn: Any = None) -> None:
    frame: dict[str, Any] = {"tp": EVT}
    if warn is not None:
        frame["warn"] = warn
    await fake.push(frame)
    await wait_until(lambda: coordinator.brew_confirm is not None)
    await settle()


async def _wait_cleared(coordinator: GaggiMateCoordinator) -> None:
    await wait_until(lambda: coordinator.brew_confirm is None)
    await settle()


async def test_pending_off_initially_and_buttons_unavailable(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    state = _pending(hass)
    assert state.state == "off"
    assert state.attributes["warnings"] == []
    assert coordinator.brew_confirm is None
    assert state.attributes["friendly_name"].endswith("Brew Confirmation Pending")
    assert _btn_state(hass, "brew_confirm") == UNAVAILABLE
    assert _btn_state(hass, "brew_confirm_cancel") == UNAVAILABLE
    # Start Brew is a normal always-available button
    assert _btn_state(hass, "brew_start") != UNAVAILABLE


async def test_event_sets_pending_with_attribute_shape(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await _raise_pending(fake_gaggimate, coordinator, [{"k": "water_tank_low", "l": 2}, {"k": "descale", "l": 1}])
    state = _pending(hass)
    assert state.state == "on"
    assert state.attributes["warnings"] == [
        {"key": "water_tank_low", "severity": "Error", "severity_level": 2},
        {"key": "descale", "severity": "Warning", "severity_level": 1},
    ]
    assert _btn_state(hass, "brew_confirm") != UNAVAILABLE
    assert _btn_state(hass, "brew_confirm_cancel") != UNAVAILABLE


@pytest.mark.parametrize("warn", [None, "oops", {"k": "x", "l": 2}, 5, []])
async def test_missing_or_non_list_warn_is_pending_with_empty_warnings(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, warn: Any
) -> None:
    coordinator = await setup_integration()
    await fake_gaggimate.push({"tp": EVT, **({} if warn is None else {"warn": warn})})
    await wait_until(lambda: coordinator.brew_confirm is not None)
    await settle()
    assert _pending(hass).state == "on"
    assert _pending(hass).attributes["warnings"] == []


async def test_non_dict_entries_ignored_and_unknown_severity(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await _raise_pending(
        fake_gaggimate, coordinator, ["junk", 3, None, {"k": "a", "l": 2}, {"k": "b", "l": 9}, {"k": "c"}]
    )
    warnings = _pending(hass).attributes["warnings"]
    assert [w["key"] for w in warnings] == ["a", "b", "c"]  # non-dict entries dropped
    assert warnings[0] == {"key": "a", "severity": "Error", "severity_level": 2}
    assert warnings[1]["severity"] == "Unknown"
    assert warnings[1]["severity_level"] == 9
    assert warnings[2]["severity"] == "Unknown"
    assert warnings[2]["severity_level"] is None


async def test_confirm_sends_ignore_warnings_json_true(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await _raise_pending(fake_gaggimate, coordinator, WARN_ERR)
    fake_gaggimate.clear_requests()
    await _press(hass, "brew_confirm")
    reqs = await fake_gaggimate.wait_for_request(ACTIVATE)
    assert reqs == [{"tp": ACTIVATE, "ignoreWarnings": True}]
    assert reqs[0]["ignoreWarnings"] is True  # not 1, not "true"
    # the pending state stays until the firmware's status shows the brew started
    assert coordinator.brew_confirm is not None


async def test_confirm_wire_is_json_boolean_literal(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await _raise_pending(fake_gaggimate, coordinator, WARN_ERR)
    sent: list[str] = []
    ws = coordinator._ws
    real = ws.send_json

    async def spy(data: Any, *a: Any, **kw: Any) -> None:
        sent.append(json.dumps(data))
        await real(data, *a, **kw)

    with patch.object(ws, "send_json", spy):
        await _press(hass, "brew_confirm")
    assert sent == ['{"tp": "req:process:activate", "ignoreWarnings": true}']


async def test_start_brew_wire_format_unchanged(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    await _press(hass, "brew_start")
    assert (await fake_gaggimate.wait_for_request(ACTIVATE)) == [{"tp": ACTIVATE}]
    # still no flag while a confirmation is pending
    await _raise_pending(fake_gaggimate, coordinator, WARN_ERR)
    fake_gaggimate.clear_requests()
    await _press(hass, "brew_start")
    reqs = await fake_gaggimate.wait_for_request(ACTIVATE)
    assert reqs == [{"tp": ACTIVATE}]
    assert "ignoreWarnings" not in reqs[0]


async def test_cancel_sends_request_and_event_clears(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await _raise_pending(fake_gaggimate, coordinator, WARN_ERR)
    fake_gaggimate.clear_requests()
    await _press(hass, "brew_confirm_cancel")
    assert (await fake_gaggimate.wait_for_request(CANCEL)) == [{"tp": CANCEL}]
    # the firmware answers with the cancel event; only that clears the state
    assert coordinator.brew_confirm is not None
    await fake_gaggimate.push({"tp": EVT_CANCEL})
    await _wait_cleared(coordinator)
    assert _pending(hass).state == "off"
    assert _pending(hass).attributes["warnings"] == []
    assert _btn_state(hass, "brew_confirm") == UNAVAILABLE
    assert _btn_state(hass, "brew_confirm_cancel") == UNAVAILABLE


async def test_cancel_event_from_other_client_clears(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await _raise_pending(fake_gaggimate, coordinator, WARN_ERR)
    await fake_gaggimate.push({"tp": EVT_CANCEL})  # no HA press involved
    await _wait_cleared(coordinator)
    assert _pending(hass).state == "off"


async def test_cancel_event_without_pending_is_harmless(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await fake_gaggimate.push({"tp": EVT_CANCEL})
    await settle(0.15)
    assert coordinator.brew_confirm is None
    assert _pending(hass).state == "off"


async def test_clear_on_process_active(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await _raise_pending(fake_gaggimate, coordinator, WARN_ERR)
    await fake_gaggimate.push({"tp": "evt:status", "process": {"a": 1, "s": "brew"}})
    await _wait_cleared(coordinator)
    assert _pending(hass).state == "off"
    assert _btn_state(hass, "brew_confirm") == UNAVAILABLE


@pytest.mark.parametrize("process", [{"a": 0}, None, {"s": "brew"}, "x"])
async def test_process_not_active_does_not_clear(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, process: Any
) -> None:
    coordinator = await setup_integration()
    await _raise_pending(fake_gaggimate, coordinator, WARN_ERR)
    await fake_gaggimate.push({"tp": "evt:status", "process": process})
    await settle(0.15)
    assert coordinator.brew_confirm is not None
    assert _pending(hass).state == "on"


@pytest.mark.parametrize("mode", [0, 2, 3, 4])
async def test_clear_on_mode_change_away_from_brew(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration, mode: int
) -> None:
    coordinator = await setup_integration()
    await _raise_pending(fake_gaggimate, coordinator, WARN_ERR)
    await fake_gaggimate.push({"tp": "evt:status", "m": mode})
    await _wait_cleared(coordinator)
    assert _pending(hass).state == "off"


async def test_mode_brew_does_not_clear(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await _raise_pending(fake_gaggimate, coordinator, WARN_ERR)
    await fake_gaggimate.push({"tp": "evt:status", "m": 1})
    await fake_gaggimate.push({"tp": "evt:status", "ct": 90.0})
    await settle(0.15)
    assert coordinator.brew_confirm is not None


async def test_missing_mode_does_not_clear(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """Merged data without ``m`` (and a null ``m``) must not count as a mode change."""
    state = dict(load_json("single_boiler_idle")["state"])
    state.pop("m")
    fake_gaggimate.state = state
    coordinator = await setup_integration()
    assert "m" not in (coordinator.data or {})
    await _raise_pending(fake_gaggimate, coordinator, WARN_ERR)
    await fake_gaggimate.push({"tp": "evt:status", "ct": 91.0})
    await fake_gaggimate.push({"tp": "evt:status", "m": None})
    await settle(0.15)
    assert coordinator.brew_confirm is not None
    assert _pending(hass).state == "on"


async def test_disconnect_makes_unavailable_then_cleared_after_reconnect(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await _raise_pending(fake_gaggimate, coordinator, WARN_ERR)
    await fake_gaggimate.drop_connections()
    await wait_until(lambda: not coordinator.available)
    await settle()
    assert _pending(hass).state == UNAVAILABLE
    assert coordinator.brew_confirm is None
    assert _btn_state(hass, "brew_confirm") == UNAVAILABLE
    assert _btn_state(hass, "brew_confirm_cancel") == UNAVAILABLE
    await wait_until(lambda: coordinator.available)
    await settle()
    assert _pending(hass).state == "off"
    assert _pending(hass).attributes["warnings"] == []
    assert _btn_state(hass, "brew_confirm") == UNAVAILABLE
    assert coordinator.brew_confirm is None


async def test_availability_timeout_clears(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    with patch("custom_components.gaggimate.coordinator.WS_UNAVAILABLE_TIMEOUT", 1.5):
        coordinator = await setup_integration()
        await _raise_pending(fake_gaggimate, coordinator, WARN_ERR)
        assert _pending(hass).state == "on"
        # no keepalive: the watchdog fires
        await wait_until(lambda: not coordinator.available, timeout=6)
        assert coordinator.brew_confirm is None
        await settle()
        assert _pending(hass).state == UNAVAILABLE
        await wait_until(lambda: coordinator.available, timeout=8)
        await settle()
        assert _pending(hass).state == "off"


async def test_confirm_not_ready_raises_and_keeps_pending(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = await setup_integration()
    await _raise_pending(fake_gaggimate, coordinator, WARN_ERR)
    await fake_gaggimate.push({"tp": "evt:status", "sys": {"s": "error", "m": "boom"}})
    await wait_until(lambda: (coordinator.data or {}).get("sys", {}).get("s") == "error")
    await settle()
    assert coordinator.brew_confirm is not None  # sys change alone does not clear
    fake_gaggimate.clear_requests()
    with pytest.raises(HomeAssistantError, match="not ready"):
        await _press(hass, "brew_confirm")
    await settle()
    assert fake_gaggimate.requests_of(ACTIVATE) == []
    assert coordinator.brew_confirm is not None
    assert _pending(hass).state == "on"
    # Start Brew is guarded the same way
    with pytest.raises(HomeAssistantError, match="not ready"):
        await _press(hass, "brew_start")
    assert fake_gaggimate.requests_of(ACTIVATE) == []
    # Cancel works regardless of the system state
    await _press(hass, "brew_confirm_cancel")
    assert (await fake_gaggimate.wait_for_request(CANCEL)) == [{"tp": CANCEL}]
    await fake_gaggimate.push({"tp": EVT_CANCEL})
    await _wait_cleared(coordinator)
    assert _pending(hass).state == "off"
    # the only logged problems are the expected press failures
    assert all(("Failed to press" in w or "Failed to start brew" in w) for w in _own_warnings(caplog)), _own_warnings(caplog)


async def test_second_event_replaces_list(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await _raise_pending(fake_gaggimate, coordinator, [{"k": "a", "l": 2}, {"k": "b", "l": 2}])
    assert [w["key"] for w in _pending(hass).attributes["warnings"]] == ["a", "b"]
    await fake_gaggimate.push({"tp": EVT, "warn": [{"k": "c", "l": 1}]})
    await wait_until(lambda: [w["k"] for w in coordinator.brew_confirm] == ["c"])
    await settle()
    assert _pending(hass).state == "on"
    assert _pending(hass).attributes["warnings"] == [{"key": "c", "severity": "Warning", "severity_level": 1}]


async def test_pending_stays_when_warning_stops_being_active(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """Documented behaviour: only the listed events/status paths clear; a vanished warning does not."""
    coordinator = await setup_integration()
    await _raise_pending(fake_gaggimate, coordinator, [{"k": "backflush", "l": 2}])
    await fake_gaggimate.push({"tp": "evt:status", "warn": []})
    await fake_gaggimate.push({"tp": "evt:status", "warn": None})
    await settle(0.15)
    assert coordinator.brew_confirm is not None
    assert _pending(hass).state == "on"
    assert [w["key"] for w in _pending(hass).attributes["warnings"]] == ["backflush"]


async def test_event_listeners_see_updated_state(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    seen: list[tuple[str, Any]] = []

    def listener(msg_type: str, message: dict[str, Any]) -> None:
        if msg_type in (EVT, EVT_CANCEL):
            seen.append((msg_type, coordinator.brew_confirm))

    remove = coordinator.async_add_event_listener(listener)
    try:
        await fake_gaggimate.push({"tp": EVT, "warn": WARN_ERR})
        await wait_until(lambda: len(seen) == 1)
        await fake_gaggimate.push({"tp": EVT_CANCEL})
        await wait_until(lambda: len(seen) == 2)
    finally:
        remove()
    assert seen[0] == (EVT, WARN_ERR)
    assert seen[1] == (EVT_CANCEL, None)


async def test_full_flow_confirm_then_brew_starts_clears(
    hass: HomeAssistant,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = await setup_integration()
    await _press(hass, "brew_start")
    await _raise_pending(fake_gaggimate, coordinator, WARN_ERR)
    await _press(hass, "brew_confirm")
    await fake_gaggimate.wait_for_request(ACTIVATE, count=2)
    await fake_gaggimate.push({"tp": "evt:status", "process": {"a": 1}})
    await _wait_cleared(coordinator)
    assert _pending(hass).state == "off"
    assert not _own_warnings(caplog), _own_warnings(caplog)


async def test_no_warnings_logged_and_no_lingering_tasks(
    hass: HomeAssistant,
    mock_config_entry: Any,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    coordinator = await setup_integration()
    for warn in (WARN_ERR, "bad", None):
        await _raise_pending(fake_gaggimate, coordinator, warn)
        await fake_gaggimate.push({"tp": EVT_CANCEL})
        await _wait_cleared(coordinator)
    await _raise_pending(fake_gaggimate, coordinator, WARN_ERR)
    await fake_gaggimate.push({"tp": "evt:status", "m": 0})
    await _wait_cleared(coordinator)
    assert not _own_warnings(caplog), _own_warnings(caplog)
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    for task in (coordinator._availability_check_task, coordinator._reconnect_task):
        assert task is None or task.done()
    # PHACC's verify_cleanup additionally fails the test on lingering tasks/timers
