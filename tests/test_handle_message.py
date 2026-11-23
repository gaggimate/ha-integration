"""Characterization tests for GaggiMateCoordinator._handle_message (guard for a behaviour-preserving refactor)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.gaggimate import coordinator as coordinator_module

from .conftest import SetupIntegration
from .fake_server import FakeGaggiMate

COORD_LOGGER = "custom_components.gaggimate.coordinator"


def _frame(**fields: Any) -> str:
    return json.dumps(fields)


def _count_notifications(coordinator: Any) -> list[int]:
    counter = [0]

    def _cb() -> None:
        counter[0] += 1

    coordinator.async_add_listener(_cb)
    return counter


async def _drain_ota(coordinator: Any) -> None:
    task = coordinator._ota_refresh_task
    if task is not None:
        await task


# -- 1. `up` change -> debounced OTA refresh --------------------------------------------------------


@pytest.fixture
def fast_ota() -> Any:
    with patch.object(coordinator_module, "OTA_REFRESH_DELAY", 0.01):
        yield


async def _ota_calls_after(coordinator: Any, previous: dict[str, Any], frame: dict[str, Any]) -> int:
    mock = AsyncMock()
    with patch.object(coordinator, "request_ota_settings", mock):
        coordinator.data = previous
        await coordinator._handle_message(json.dumps({"tp": "evt:status", **frame}))
        await _drain_ota(coordinator)
    return mock.await_count


async def test_up_change_schedules_exactly_one_refresh(
    hass: HomeAssistant, setup_integration: SetupIntegration, fast_ota: None
) -> None:
    coordinator = await setup_integration()
    assert await _ota_calls_after(coordinator, {"up": False}, {"up": True}) == 1


async def test_up_change_to_and_from_null_schedules_refresh(
    hass: HomeAssistant, setup_integration: SetupIntegration, fast_ota: None
) -> None:
    coordinator = await setup_integration()
    assert await _ota_calls_after(coordinator, {"up": None}, {"up": True}) == 1
    assert await _ota_calls_after(coordinator, {"up": True}, {"up": None}) == 1


async def test_up_unchanged_schedules_none(
    hass: HomeAssistant, setup_integration: SetupIntegration, fast_ota: None
) -> None:
    coordinator = await setup_integration()
    assert await _ota_calls_after(coordinator, {"up": True}, {"up": True}) == 0
    assert coordinator._ota_refresh_task is None or coordinator._ota_refresh_task.done()


async def test_up_absent_in_previous_schedules_none(
    hass: HomeAssistant, setup_integration: SetupIntegration, fast_ota: None
) -> None:
    coordinator = await setup_integration()
    assert await _ota_calls_after(coordinator, {"ct": 1}, {"up": True}) == 0


async def test_up_absent_in_message_schedules_none(
    hass: HomeAssistant, setup_integration: SetupIntegration, fast_ota: None
) -> None:
    coordinator = await setup_integration()
    # `up` is in the merged data (carried over from previous) but not in the frame itself
    assert await _ota_calls_after(coordinator, {"up": True}, {"ct": 5}) == 0


async def test_up_change_in_both_schedules_via_previous_snapshot(
    hass: HomeAssistant, setup_integration: SetupIntegration, fast_ota: None
) -> None:
    """Previous data is captured before the merge: the old dict is not mutated and drives the diff."""
    coordinator = await setup_integration()
    previous = {"up": 1, "ct": 10}
    mock = AsyncMock()
    with patch.object(coordinator, "request_ota_settings", mock):
        coordinator.data = previous
        await coordinator._handle_message(_frame(tp="evt:status", up=2))
        await _drain_ota(coordinator)
    assert previous == {"up": 1, "ct": 10}
    assert coordinator.data is not previous
    assert coordinator.data["up"] == 2 and coordinator.data["ct"] == 10
    assert mock.await_count == 1


async def test_up_changes_are_debounced_to_one_pending_task(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    mock = AsyncMock()
    with patch.object(coordinator_module, "OTA_REFRESH_DELAY", 0.05), patch.object(
        coordinator, "request_ota_settings", mock
    ):
        coordinator.data = {"up": 0}
        await coordinator._handle_message(_frame(tp="evt:status", up=1))
        first = coordinator._ota_refresh_task
        assert first is not None and not first.done()
        await coordinator._handle_message(_frame(tp="evt:status", up=2))
        await coordinator._handle_message(_frame(tp="evt:status", up=3))
        assert coordinator._ota_refresh_task is first
        await first
    assert mock.await_count == 1


# -- 2. profiles list response ---------------------------------------------------------------------


async def test_profiles_skip_invalid_entries(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    coordinator = await setup_integration()
    notified = _count_notifications(coordinator)
    profiles = [
        "string",
        42,
        None,
        ["list"],
        {"id": "no-label"},
        {"label": "No id"},
        {"label": "", "id": "empty-label"},
        {"label": "Empty id", "id": ""},
        {"label": "Good", "id": "good"},
    ]
    await coordinator._handle_message(_frame(tp="res:profiles:list", profiles=profiles))
    assert coordinator.profiles == {"Good": "good"}
    assert notified[0] == 1


async def test_profiles_duplicate_label_last_wins(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await coordinator._handle_message(
        _frame(tp="res:profiles:list", profiles=[{"label": "A", "id": "1"}, {"label": "A", "id": "2"}])
    )
    assert coordinator.profiles == {"A": "2"}


async def test_profiles_empty_list_replaces_and_notifies(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    assert coordinator.profiles  # primed on connect
    notified = _count_notifications(coordinator)
    await coordinator._handle_message(_frame(tp="res:profiles:list", profiles=[]))
    assert coordinator.profiles == {}
    assert notified[0] == 1


async def test_profiles_all_invalid_entries_yield_empty_and_notify(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    notified = _count_notifications(coordinator)
    await coordinator._handle_message(_frame(tp="res:profiles:list", profiles=["x", {"id": "a"}]))
    assert coordinator.profiles == {}
    assert notified[0] == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"error": "Update in progress"},
        {"error": "boom", "profiles": [{"label": "X", "id": "x"}]},
        {"profiles": "not-a-list"},
        {"profiles": None},
        {"profiles": {"label": "X", "id": "x"}},
        {},
    ],
)
async def test_profiles_error_or_invalid_leaves_profiles_and_does_not_notify(
    hass: HomeAssistant, setup_integration: SetupIntegration, fields: dict[str, Any]
) -> None:
    coordinator = await setup_integration()
    before = dict(coordinator.profiles)
    assert before
    notified = _count_notifications(coordinator)
    await coordinator._handle_message(_frame(tp="res:profiles:list", **fields))
    assert coordinator.profiles == before
    assert notified[0] == 0


async def test_profiles_response_with_rid_resolves_and_updates(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    coordinator._pending_requests["p1"] = fut
    await coordinator._handle_message(
        _frame(tp="res:profiles:list", rid="p1", profiles=[{"label": "L", "id": "i"}])
    )
    assert fut.result()["rid"] == "p1"
    assert coordinator.profiles == {"L": "i"}


# -- 3. status frames and availability -------------------------------------------------------------


async def test_status_sets_available_and_notifies_once(
    hass: HomeAssistant, setup_integration: SetupIntegration, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = await setup_integration()
    coordinator._available = False
    coordinator._reconnect_attempt = 3
    notified = _count_notifications(coordinator)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=COORD_LOGGER):
        await coordinator._handle_message(_frame(tp="evt:status", ct=1.0))
        assert coordinator.available is True
        # one notification from the availability flip, one from the data update
        assert notified[0] == 2
        assert coordinator._reconnect_attempt == 0
        await coordinator._handle_message(_frame(tp="evt:status", ct=2.0))
        await coordinator._handle_message(_frame(tp="evt:status", ct=3.0))
    assert caplog.text.count("GaggiMate became available") == 1
    assert notified[0] == 4  # later frames notify once each (data update only)
    assert coordinator.data["ct"] == 3.0


async def test_status_while_available_logs_nothing_and_notifies_once(
    hass: HomeAssistant, setup_integration: SetupIntegration, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = await setup_integration()
    assert coordinator.available
    notified = _count_notifications(coordinator)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=COORD_LOGGER):
        await coordinator._handle_message(_frame(tp="evt:status", ct=9.0))
    assert "became available" not in caplog.text
    assert notified[0] == 1


async def test_status_frame_updates_last_status_time(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    coordinator._last_status_time = 0.0
    await coordinator._handle_message(_frame(tp="evt:status", ct=9.0))
    assert coordinator._last_status_time > 0.0


async def test_status_merge_keeps_tp_key_of_message(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await coordinator._handle_message(_frame(tp="evt:status", ct=9.0))
    assert coordinator.data["tp"] == "evt:status"


# -- 4. non-dict and invalid JSON ------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["[]", '"x"', "123", "null", "true", "[1, 2]"])
async def test_valid_json_non_dict_raises_attribute_error(
    hass: HomeAssistant, setup_integration: SetupIntegration, raw: str
) -> None:
    """CURRENT behaviour: `.get` on a non-dict escapes _handle_message (caught only by _listen)."""
    coordinator = await setup_integration()
    before = dict(coordinator.data)
    with pytest.raises(AttributeError):
        await coordinator._handle_message(raw)
    assert coordinator.data == before


@pytest.mark.parametrize("raw", ["{not json", "", "{", "nope"])
async def test_invalid_json_logged_and_not_raised(
    hass: HomeAssistant, setup_integration: SetupIntegration, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    coordinator = await setup_integration()
    before = dict(coordinator.data)
    notified = _count_notifications(coordinator)
    with caplog.at_level(logging.ERROR, logger=COORD_LOGGER):
        await coordinator._handle_message(raw)
    assert "Invalid WebSocket JSON" in caplog.text
    assert coordinator.data == before
    assert notified[0] == 0


async def test_non_dict_frame_over_websocket_drops_connection_via_listen_loop(
    hass: HomeAssistant, setup_integration: SetupIntegration, fake_gaggimate: FakeGaggiMate
) -> None:
    """CURRENT behaviour: the AttributeError ends the listen loop, which reconnects."""
    from .conftest import wait_until

    coordinator = await setup_integration()
    assert fake_gaggimate.connect_count == 1
    await fake_gaggimate.push_raw("[]")
    await wait_until(lambda: fake_gaggimate.connect_count >= 2, timeout=15)
    await wait_until(lambda: coordinator.available)


# -- 5. tp / rid handling --------------------------------------------------------------------------


async def test_frame_without_tp_is_ignored_but_resolves_rid(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    got: list[str] = []
    coordinator.async_add_event_listener(lambda tp, msg: got.append(tp))
    data_before = coordinator.data
    profiles_before = dict(coordinator.profiles)
    ota_before = coordinator.ota_settings
    notified = _count_notifications(coordinator)

    await coordinator._handle_message(_frame(foo=1))
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    coordinator._pending_requests["r0"] = fut
    await coordinator._handle_message(_frame(rid="r0", foo=2))

    assert fut.result() == {"rid": "r0", "foo": 2}
    assert got == []
    assert coordinator.data is data_before
    assert coordinator.profiles == profiles_before
    assert coordinator.ota_settings == ota_before
    assert notified[0] == 0


async def test_unknown_tp_with_rid_resolved_then_ignored(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    got: list[str] = []
    coordinator.async_add_event_listener(lambda tp, msg: got.append(tp))
    data_before = coordinator.data
    notified = _count_notifications(coordinator)
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    coordinator._pending_requests["r1"] = fut

    await coordinator._handle_message(_frame(tp="res:something-new", rid="r1", ok=True))

    assert fut.result()["tp"] == "res:something-new"
    assert "r1" not in coordinator._pending_requests
    assert got == []
    assert coordinator.data is data_before
    assert notified[0] == 0


async def test_rid_not_pending_is_harmless(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    coordinator = await setup_integration()
    other: asyncio.Future = asyncio.get_running_loop().create_future()
    coordinator._pending_requests["other"] = other
    await coordinator._handle_message(_frame(tp="res:x", rid="nobody"))
    assert not other.done()
    assert "other" in coordinator._pending_requests
    other.cancel()
    coordinator._pending_requests.clear()


async def test_empty_rid_is_not_resolved(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    coordinator = await setup_integration()
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    coordinator._pending_requests[""] = fut
    await coordinator._handle_message(_frame(tp="res:x", rid=""))
    assert not fut.done()
    fut.cancel()
    coordinator._pending_requests.clear()


async def test_rid_bearing_event_resolved_and_dispatched(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    got: list[dict[str, Any]] = []
    coordinator.async_add_event_listener(lambda tp, msg: got.append(msg))
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    coordinator._pending_requests["e1"] = fut
    await coordinator._handle_message(_frame(tp="evt:thing", rid="e1", v=1))
    assert fut.result() == {"tp": "evt:thing", "rid": "e1", "v": 1}
    assert got == [fut.result()]
    assert got[0] is fut.result()  # same shared message object


async def test_status_frames_never_resolve_pending_futures(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    coordinator._pending_requests["s1"] = fut
    await coordinator._handle_message(_frame(tp="evt:status", rid="s1", ct=5.0))
    assert not fut.done()
    assert coordinator._pending_requests["s1"] is fut
    assert coordinator.data["rid"] == "s1"  # the rid is merged like any other key
    fut.cancel()
    coordinator._pending_requests.clear()


async def test_late_response_with_done_future_does_not_crash(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    fut.set_result({"first": True})
    coordinator._pending_requests["late"] = fut
    await coordinator._handle_message(_frame(tp="res:x", rid="late"))
    assert fut.result() == {"first": True}
    assert "late" not in coordinator._pending_requests

    cancelled: asyncio.Future = asyncio.get_running_loop().create_future()
    cancelled.cancel()
    coordinator._pending_requests["gone"] = cancelled
    await coordinator._handle_message(_frame(tp="res:x", rid="gone"))
    assert cancelled.cancelled()
    assert "gone" not in coordinator._pending_requests


async def test_ota_settings_response_stores_message_and_notifies(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    notified = _count_notifications(coordinator)
    await coordinator._handle_message(_frame(tp="res:ota-settings", channel="latest"))
    assert coordinator.ota_settings == {"tp": "res:ota-settings", "channel": "latest"}
    assert notified[0] == 1


# -- 6. order guarantees ---------------------------------------------------------------------------


async def test_brew_confirm_set_before_dispatch(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    coordinator = await setup_integration()
    seen: list[Any] = []
    coordinator.async_add_event_listener(lambda tp, msg: seen.append((tp, coordinator.brew_confirm)))
    await coordinator._handle_message(
        _frame(tp="evt:brew:confirm", warn=[{"k": "descaling"}, "junk", {"k": "backflush"}])
    )
    assert seen == [("evt:brew:confirm", [{"k": "descaling"}, {"k": "backflush"}])]


async def test_brew_confirm_non_list_warn_sets_empty_before_dispatch(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    seen: list[Any] = []
    coordinator.async_add_event_listener(lambda tp, msg: seen.append(coordinator.brew_confirm))
    await coordinator._handle_message(_frame(tp="evt:brew:confirm", warn="oops"))
    assert seen == [[]]


async def test_brew_confirm_cancel_clears_before_dispatch(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await coordinator._handle_message(_frame(tp="evt:brew:confirm", warn=[{"k": "x"}]))
    assert coordinator.brew_confirm == [{"k": "x"}]
    seen: list[Any] = []
    coordinator.async_add_event_listener(lambda tp, msg: seen.append((tp, coordinator.brew_confirm)))
    notified = _count_notifications(coordinator)
    await coordinator._handle_message(_frame(tp="evt:brew:confirm:cancel"))
    assert seen == [("evt:brew:confirm:cancel", None)]
    assert coordinator.brew_confirm is None
    assert notified[0] == 1


async def test_brew_confirm_cancel_without_pending_does_not_notify_but_dispatches(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    got: list[str] = []
    coordinator.async_add_event_listener(lambda tp, msg: got.append(tp))
    notified = _count_notifications(coordinator)
    await coordinator._handle_message(_frame(tp="evt:brew:confirm:cancel"))
    assert got == ["evt:brew:confirm:cancel"]
    assert notified[0] == 0


async def test_brew_confirm_set_notifies_listeners_once(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    notified = _count_notifications(coordinator)
    await coordinator._handle_message(_frame(tp="evt:brew:confirm", warn=[]))
    assert coordinator.brew_confirm == []
    assert notified[0] == 1


async def test_status_clears_stale_brew_confirm_with_single_notification(
    hass: HomeAssistant, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    await coordinator._handle_message(_frame(tp="evt:brew:confirm", warn=[{"k": "x"}]))
    notified = _count_notifications(coordinator)
    await coordinator._handle_message(_frame(tp="evt:status", process={"a": 1}))
    assert coordinator.brew_confirm is None
    assert notified[0] == 1  # the data update notifies; the clear itself does not
