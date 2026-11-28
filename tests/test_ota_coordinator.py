"""Coordinator side of firmware updates: OTA state, commands and the watchdog grace window.

Most tests feed frames straight into ``coordinator._process_message`` (synchronous, so nothing
races) with the fake server's keepalive switched off; a few end-to-end tests let the fake server
emulate a whole update (see the OTA block in ``tests/fake_server.py``).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.gaggimate.const import OTA_START_PENDING, OTA_WATCHDOG_GRACE

from .conftest import SetupIntegration, settle, wait_until
from .fake_server import FakeGaggiMate, load_json

MODULE = "custom_components.gaggimate.coordinator"
PROGRESS = "evt:ota-progress"
SETTINGS = "res:ota-settings"
START = "req:ota-start"
REQ_SETTINGS = "req:ota-settings"


@pytest.fixture(autouse=True)
def _slow_ota_refresh() -> Any:
    """Keep the debounced OTA refresh out of the way unless a test patches a short delay."""
    with patch(f"{MODULE}.OTA_REFRESH_DELAY", 30):
        yield


@pytest.fixture
async def quiet(fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration) -> Any:
    """Integration set up, fake keepalive off: only frames the test feeds in arrive."""
    fake_gaggimate.keepalive = False
    coordinator = await setup_integration()
    fake_gaggimate.clear_requests()
    return coordinator


def progress(coordinator: Any, phase: Any = ..., progress_: Any = ...) -> None:
    frame: dict[str, Any] = {"tp": PROGRESS}
    if phase is not ...:
        frame["phase"] = phase
    if progress_ is not ...:
        frame["progress"] = progress_
    coordinator._process_message(frame)


def settings(coordinator: Any, **fields: Any) -> None:
    coordinator._process_message({"tp": SETTINGS, **fields})


def status(coordinator: Any) -> None:
    coordinator._process_message({"tp": "evt:status"})


def count_notifications(coordinator: Any) -> list[int]:
    calls = [0]

    def _listener() -> None:
        calls[0] += 1

    coordinator.async_add_listener(_listener)
    return calls


# --- evt:ota-progress parsing -------------------------------------------------------------------
async def test_initial_state(quiet: Any) -> None:
    c = quiet
    assert c.ota_phase is None and c.ota_percent is None
    assert c.ota_updating is False
    assert c.ota_channel == "latest"
    assert c.ota_settings["updating"] is False


@pytest.mark.parametrize("phase", [1, 2, 3])
async def test_flashing_phases(quiet: Any, phase: int) -> None:
    c = quiet
    progress(c, phase, 42)
    assert (c.ota_phase, c.ota_percent) == (phase, 42)
    assert c.ota_updating is True
    assert c._ota_ran is True
    assert c._ota_grace_until is not None and c._ota_grace_until > time.monotonic() + OTA_WATCHDOG_GRACE - 5


async def test_phase_4_after_run(quiet: Any) -> None:
    c = quiet
    progress(c, 3, 80)
    progress(c, 4, 7)
    assert (c.ota_phase, c.ota_percent) == (4, 100)
    assert c._ota_ran is True
    assert c.ota_updating is True  # display is about to reboot
    assert c._ota_grace_until is None and c._ota_start_sent_at is None


async def test_phase_4_noop(quiet: Any) -> None:
    c = quiet
    progress(c, 4, 100)
    assert (c.ota_phase, c.ota_percent) == (4, 100)
    assert c._ota_ran is False
    assert c.ota_updating is False


async def test_phase_5_and_0_end_the_run(quiet: Any) -> None:
    c = quiet
    for phase in (5, 0):
        progress(c, 1, 10)
        progress(c, phase, 0)
        assert c.ota_phase == phase
        assert c.ota_percent is None
        assert c._ota_ran is False
        assert c.ota_updating is False
        assert c._ota_grace_until is None


@pytest.mark.parametrize(
    "frame",
    [
        {"tp": PROGRESS},
        {"tp": PROGRESS, "progress": 10},
        {"tp": PROGRESS, "phase": "1", "progress": 10},
        {"tp": PROGRESS, "phase": True, "progress": 10},
        {"tp": PROGRESS, "phase": False, "progress": 10},
        {"tp": PROGRESS, "phase": 9, "progress": 10},
        {"tp": PROGRESS, "phase": -1, "progress": 10},
        {"tp": PROGRESS, "phase": 1.5, "progress": 10},
        {"tp": PROGRESS, "phase": None, "progress": 10},
        {"tp": PROGRESS, "phase": [1], "progress": 10},
    ],
)
async def test_malformed_progress_is_ignored(quiet: Any, frame: dict[str, Any]) -> None:
    c = quiet
    progress(c, 3, 40)
    grace = c._ota_grace_until
    calls = count_notifications(c)
    c._process_message(frame)
    assert (c.ota_phase, c.ota_percent) == (3, 40)
    assert c._ota_grace_until == grace
    assert calls[0] == 0


async def test_malformed_progress_from_idle_stays_idle(quiet: Any) -> None:
    c = quiet
    c._process_message({"tp": PROGRESS, "phase": "x"})
    assert c.ota_phase is None and c.ota_updating is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("50", 0), (150, 100), (-5, 0), (True, 0), (None, 0), (2.5, 0), (0, 0), (100, 100), (57, 57)],
)
async def test_progress_value_is_clamped(quiet: Any, raw: Any, expected: int) -> None:
    c = quiet
    c._process_message({"tp": PROGRESS, "phase": 1, "progress": raw})
    assert c.ota_percent == expected


async def test_missing_progress_is_zero(quiet: Any) -> None:
    c = quiet
    progress(c, 1)
    assert (c.ota_phase, c.ota_percent) == (1, 0)


async def test_duplicate_frames_do_not_renotify(quiet: Any) -> None:
    c = quiet
    calls = count_notifications(c)
    progress(c, 1, 10)
    assert calls[0] == 1
    progress(c, 1, 10)
    assert calls[0] == 1
    progress(c, 1, 11)
    assert calls[0] == 2
    progress(c, 3, 11)
    assert calls[0] == 3


async def test_duplicate_frame_still_extends_grace(quiet: Any) -> None:
    c = quiet
    progress(c, 1, 10)
    c._ota_grace_until = time.monotonic() + 1
    progress(c, 1, 10)
    assert c._ota_grace_until > time.monotonic() + OTA_WATCHDOG_GRACE - 5


async def test_progress_event_is_still_dispatched(quiet: Any) -> None:
    c = quiet
    got: list[tuple[str, dict[str, Any]]] = []
    c.async_add_event_listener(lambda tp, msg: got.append((tp, msg)))
    progress(c, 1, 5)
    assert got and got[0][0] == PROGRESS and got[0][1]["phase"] == 1


async def test_terminal_phases_give_fresh_status_window(quiet: Any) -> None:
    c = quiet
    for phase in (4, 5, 0):
        c._last_status_time = time.monotonic() - 1000
        progress(c, 1, 1)
        progress(c, phase, 0)
        assert time.monotonic() - c._last_status_time < 1


@pytest.mark.parametrize("phase", [4, 5, 0])
async def test_terminal_phases_schedule_one_refresh(quiet: Any, phase: int) -> None:
    c = quiet
    with patch(f"{MODULE}.OTA_REFRESH_DELAY", 0.05), patch.object(c, "request_ota_settings", AsyncMock()) as mock:
        progress(c, 1, 1)
        progress(c, phase, 0)
        progress(c, phase, 0)  # debounced
        await wait_until(lambda: mock.await_count >= 1)
        await settle(0.2)
        assert mock.await_count == 1


async def test_flashing_phase_schedules_no_refresh(quiet: Any) -> None:
    c = quiet
    with patch(f"{MODULE}.OTA_REFRESH_DELAY", 0.05), patch.object(c, "request_ota_settings", AsyncMock()) as mock:
        progress(c, 1, 1)
        progress(c, 3, 1)
        await settle(0.2)
        assert mock.await_count == 0


# --- res:ota-settings ----------------------------------------------------------------------------
async def test_settings_store_channel_and_updating(quiet: Any) -> None:
    c = quiet
    settings(c, channel="nightly", updating=True, displayVersion="v1")
    assert c.ota_channel == "nightly"
    assert c.ota_settings["updating"] is True
    assert c.ota_updating is True
    assert c._ota_grace_until is not None and c._ota_grace_until > time.monotonic() + OTA_WATCHDOG_GRACE - 5
    settings(c, channel="latest", updating=False)
    assert c.ota_channel == "latest"
    assert c.ota_updating is False
    assert c._ota_grace_until is None  # nothing running: watchdog is not kept blind


async def test_old_firmware_without_new_fields(quiet: Any) -> None:
    c = quiet
    c._process_message({"tp": SETTINGS, "displayVersion": "v1.0.0", "displayUpdateAvailable": False})
    assert c.ota_channel is None
    assert c.ota_updating is False


@pytest.mark.parametrize("channel", ["beta", "", None, 3, "Latest", ["latest"]])
async def test_unrecognised_channel_is_unknown(quiet: Any, channel: Any) -> None:
    c = quiet
    settings(c, channel=channel)
    assert c.ota_channel is None


@pytest.mark.parametrize("value", ["true", 1, "yes", None])
async def test_updating_must_be_literal_true(quiet: Any, value: Any) -> None:
    c = quiet
    settings(c, updating=value)
    assert c.ota_updating is False


@pytest.mark.parametrize("terminal", [4, 5, 0])
async def test_updating_true_after_terminal_phase_starts_new_run(quiet: Any, terminal: int) -> None:
    c = quiet
    progress(c, 1, 50)
    progress(c, terminal, 0)
    settings(c, updating=False)
    assert c.ota_updating is (terminal == 4)  # phase 4 after flashing: waiting for the reboot
    # run started from the web UI: no phase yet
    settings(c, updating=True)
    assert c.ota_phase is None and c.ota_percent is None and c._ota_ran is False
    assert c.ota_updating is True


async def test_updating_true_during_flashing_keeps_phase(quiet: Any) -> None:
    c = quiet
    progress(c, 3, 60)
    settings(c, updating=True)
    assert (c.ota_phase, c.ota_percent) == (3, 60)
    assert c._ota_ran is True


async def test_settings_updating_true_extends_grace(quiet: Any) -> None:
    c = quiet
    c._ota_grace_until = time.monotonic() + 1
    settings(c, updating=True)
    assert c._ota_grace_until > time.monotonic() + OTA_WATCHDOG_GRACE - 5


async def test_settings_without_updating_keep_pending_start_grace(quiet: Any) -> None:
    c = quiet
    c._ota_start_sent_at = time.monotonic()
    c._ota_grace_until = time.monotonic() + 100
    settings(c, updating=False)
    assert c._ota_grace_until is not None


# --- ota_updating truth table --------------------------------------------------------------------
@pytest.mark.parametrize(
    ("phase", "ran", "settings_updating", "expected"),
    [
        (None, False, False, False),
        (None, False, True, True),
        (0, False, False, False),
        (0, False, True, True),
        (1, True, False, True),
        (2, True, False, True),
        (3, True, False, True),
        (3, True, True, True),
        (4, True, False, True),
        (4, True, True, True),
        (4, False, False, False),
        (4, False, True, False),
        (5, False, False, False),
        (5, False, True, True),
    ],
)
async def test_ota_updating_truth_table(
    quiet: Any, phase: int | None, ran: bool, settings_updating: bool, expected: bool
) -> None:
    c = quiet
    c._ota_phase = phase
    c._ota_ran = ran
    c._ota_settings = {**c.ota_settings, "updating": settings_updating}
    assert c.ota_updating is expected


async def test_ota_updating_start_pending_window(quiet: Any) -> None:
    c = quiet
    c._ota_start_sent_at = time.monotonic()
    assert c.ota_updating is True
    c._ota_start_sent_at = time.monotonic() - OTA_START_PENDING - 1
    assert c.ota_updating is False


# --- updating must clear (no waiting) ------------------------------------------------------------
async def test_updating_false_right_after_phase_5(quiet: Any) -> None:
    c = quiet
    settings(c, updating=True)
    progress(c, 1, 30)
    assert c.ota_updating is True
    progress(c, 5, 0)
    assert c.ota_updating is False
    assert c.ota_settings["updating"] is False


async def test_updating_false_right_after_phase_0(quiet: Any) -> None:
    c = quiet
    settings(c, updating=True)
    progress(c, 3, 30)
    progress(c, 0, 0)
    assert c.ota_updating is False
    assert c.ota_settings["updating"] is False


async def test_updating_false_after_noop_phase_4_and_status(quiet: Any) -> None:
    c = quiet
    settings(c, updating=True)
    progress(c, 4, 100)
    assert c.ota_updating is False
    status(c)
    assert c.ota_updating is False
    assert c.ota_phase is None and c.ota_percent is None
    assert c.ota_settings["updating"] is False


async def test_updating_false_after_ran_phase_4_and_status(quiet: Any) -> None:
    c = quiet
    settings(c, updating=True)
    progress(c, 1, 100)
    progress(c, 4, 100)
    assert c.ota_updating is True
    status(c)
    assert c.ota_updating is False
    assert c.ota_phase is None and c.ota_percent is None and c._ota_ran is False
    assert c.ota_settings["updating"] is False


async def test_status_frame_does_not_touch_other_phases(quiet: Any) -> None:
    c = quiet
    progress(c, 1, 20)
    status(c)
    assert (c.ota_phase, c.ota_percent) == (1, 20)
    assert c.ota_updating is True
    progress(c, 5, 0)
    status(c)
    assert c.ota_phase == 5


async def test_phase_4_then_new_run_from_elsewhere(quiet: Any) -> None:
    c = quiet
    progress(c, 1, 100)
    progress(c, 4, 100)
    settings(c, updating=True)  # a second run began
    assert c.ota_phase is None and c._ota_ran is False
    assert c.ota_updating is True


# --- start_ota -----------------------------------------------------------------------------------
@pytest.fixture
def hang(fake_gaggimate: FakeGaggiMate) -> FakeGaggiMate:
    """Fake firmware that accepts an install and then does nothing."""
    fake_gaggimate.ota_mode = "hang"
    fake_gaggimate.ota_script = []
    return fake_gaggimate


@pytest.mark.parametrize("component", ["display", "controller", "both"])
async def test_start_ota_sends_exact_request_then_settings(
    quiet: Any, hang: FakeGaggiMate, component: str
) -> None:
    c = quiet
    await c.start_ota(component)
    await hang.wait_for_request(START)
    await hang.wait_for_request(REQ_SETTINGS)
    assert hang.requests_of(START) == [{"tp": START, "cp": component}]
    tps = [r["tp"] for r in hang.requests]
    assert tps.index(START) < tps.index(REQ_SETTINGS)
    assert c.ota_updating is True
    assert c._ota_start_sent_at is not None
    assert c._ota_grace_until is not None


@pytest.mark.parametrize("component", ["", "DISPLAY", "Both", "all", "display,controller", None, 1])
async def test_start_ota_rejects_bad_component(quiet: Any, hang: FakeGaggiMate, component: Any) -> None:
    c = quiet
    with pytest.raises(ValueError):
        await c.start_ota(component)
    await settle()
    assert hang.requests == []
    assert c._ota_start_sent_at is None and c.ota_updating is False


async def test_second_start_within_pending_window_fails(quiet: Any, hang: FakeGaggiMate) -> None:
    c = quiet
    await c.start_ota("display")
    with pytest.raises(UpdateFailed):
        await c.start_ota("display")
    with pytest.raises(UpdateFailed):
        await c.start_ota("both")
    await settle()
    assert len(hang.requests_of(START)) == 1


async def test_concurrent_starts_send_one_request(quiet: Any, hang: FakeGaggiMate) -> None:
    c = quiet
    results = await asyncio.gather(c.start_ota("display"), c.start_ota("display"), return_exceptions=True)
    assert sum(isinstance(r, UpdateFailed) for r in results) == 1
    await settle()
    assert len(hang.requests_of(START)) == 1


async def test_start_while_updating_sends_nothing(quiet: Any, hang: FakeGaggiMate) -> None:
    c = quiet
    progress(c, 1, 10)
    with pytest.raises(UpdateFailed):
        await c.start_ota("controller")
    settings(c, updating=True)
    c._ota_phase = None
    with pytest.raises(UpdateFailed):
        await c.start_ota("controller")
    await settle()
    assert hang.requests == []


async def test_start_allowed_again_after_pending_window(quiet: Any, hang: FakeGaggiMate) -> None:
    c = quiet
    await c.start_ota("display")
    c._ota_start_sent_at = time.monotonic() - OTA_START_PENDING - 1
    settings(c, updating=False)
    await c.start_ota("controller")
    await hang.wait_for_request(START, 2)
    assert [r["cp"] for r in hang.requests_of(START)] == ["display", "controller"]


async def test_start_allowed_after_failed_run(quiet: Any, hang: FakeGaggiMate) -> None:
    c = quiet
    progress(c, 1, 10)
    progress(c, 5, 0)
    await c.start_ota("display")
    await hang.wait_for_request(START)


async def test_failed_send_resets_flags(quiet: Any) -> None:
    c = quiet
    with patch.object(c, "send_message", AsyncMock(side_effect=UpdateFailed("boom"))):
        with pytest.raises(UpdateFailed):
            await c.start_ota("display")
    assert c._ota_start_sent_at is None
    assert c._ota_grace_until is None
    assert c.ota_updating is False


async def test_cancelled_send_resets_flags(quiet: Any) -> None:
    c = quiet
    with patch.object(c, "send_message", AsyncMock(side_effect=asyncio.CancelledError())):
        with pytest.raises(asyncio.CancelledError):
            await c.start_ota("display")
    assert c._ota_start_sent_at is None
    assert c._ota_grace_until is None
    assert c.ota_updating is False


async def test_failed_send_restores_previous_values(quiet: Any) -> None:
    c = quiet
    c._ota_start_sent_at = time.monotonic() - OTA_START_PENDING - 1
    c._ota_grace_until = time.monotonic() - 5
    before = (c._ota_start_sent_at, c._ota_grace_until)
    with patch.object(c, "send_message", AsyncMock(side_effect=UpdateFailed("boom"))):
        with pytest.raises(UpdateFailed):
            await c.start_ota("both")
    assert (c._ota_start_sent_at, c._ota_grace_until) == before


async def test_start_without_connection_fails_cleanly(quiet: Any) -> None:
    c = quiet
    with patch.object(c, "_ws", None), patch.object(c, "_schedule_reconnect", AsyncMock()):
        with pytest.raises(UpdateFailed):
            await c.start_ota("display")
    assert c.ota_updating is False


@pytest.mark.frames("dual_boiler_brewing")
async def test_start_refused_during_brew(quiet: Any, hang: FakeGaggiMate) -> None:
    c = quiet
    with pytest.raises(UpdateFailed, match="busy"):
        await c.start_ota("both")
    assert hang.requests_of(START) == []


async def test_start_refused_while_grinding(quiet: Any, hang: FakeGaggiMate) -> None:
    c = quiet
    await hang.push_state(gact=1)
    await settle()
    with pytest.raises(UpdateFailed, match="busy"):
        await c.start_ota("display")
    assert hang.requests_of(START) == []


async def test_start_works_in_standby(quiet: Any, hang: FakeGaggiMate) -> None:
    c = quiet
    await hang.push_state(m=0)
    await settle()
    await c.start_ota("display")
    await hang.wait_for_request(START)
    assert hang.requests_of(START) == [{"tp": START, "cp": "display"}]


# --- check_for_updates / set_ota_channel ---------------------------------------------------------
async def test_check_for_updates_sends_current_channel(quiet: Any, fake_gaggimate: FakeGaggiMate) -> None:
    c = quiet
    await c.check_for_updates()
    await fake_gaggimate.wait_for_request(REQ_SETTINGS)
    assert fake_gaggimate.requests_of(REQ_SETTINGS) == [{"tp": REQ_SETTINGS, "update": True, "channel": "latest"}]
    fake_gaggimate.clear_requests()
    settings(c, channel="nightly")
    await c.check_for_updates()
    await fake_gaggimate.wait_for_request(REQ_SETTINGS)
    assert fake_gaggimate.requests_of(REQ_SETTINGS) == [{"tp": REQ_SETTINGS, "update": True, "channel": "nightly"}]


async def test_check_for_updates_unknown_channel(quiet: Any, fake_gaggimate: FakeGaggiMate) -> None:
    c = quiet
    c._process_message({"tp": SETTINGS, "displayVersion": "v1"})
    with pytest.raises(UpdateFailed, match="Update channel unknown"):
        await c.check_for_updates()
    await settle()
    assert fake_gaggimate.requests == []


async def test_check_for_updates_blocked_while_updating(quiet: Any, fake_gaggimate: FakeGaggiMate) -> None:
    c = quiet
    progress(c, 1, 5)
    with pytest.raises(UpdateFailed):
        await c.check_for_updates()
    await settle()
    assert fake_gaggimate.requests == []


async def test_check_result_arrives_as_later_broadcast(quiet: Any, fake_gaggimate: FakeGaggiMate) -> None:
    c = quiet
    fake_gaggimate.ota_check_result = load_json("ota_settings")["variations"]["display_update_available"]
    await c.check_for_updates()
    await wait_until(lambda: c.ota_settings.get("displayUpdateAvailable") is True)
    assert c.ota_settings["latestVersion"] == "v1.8.0"
    assert c.ota_settings["controllerUpdateAvailable"] is False


@pytest.mark.parametrize(("key", "stored"), [("nightly", "nightly"), ("latest", "latest")])
async def test_set_ota_channel_sends_and_follows_broadcast(
    quiet: Any, fake_gaggimate: FakeGaggiMate, key: str, stored: str
) -> None:
    c = quiet
    if key == "latest":
        settings(c, channel="nightly")
    await c.set_ota_channel(key)
    await fake_gaggimate.wait_for_request(REQ_SETTINGS)
    assert fake_gaggimate.requests_of(REQ_SETTINGS) == [{"tp": REQ_SETTINGS, "update": True, "channel": key}]
    await wait_until(lambda: c.ota_channel == stored and fake_gaggimate.ota_settings["channel"] == stored)


async def test_set_ota_channel_not_optimistic(quiet: Any, fake_gaggimate: FakeGaggiMate) -> None:
    c = quiet
    with patch.object(fake_gaggimate, "broadcast_ota_settings", AsyncMock()):
        await c.set_ota_channel("nightly")
        await fake_gaggimate.wait_for_request(REQ_SETTINGS)
        await settle()
    assert c.ota_channel == "latest"


@pytest.mark.parametrize("key", ["beta", "Nightly", "Stable", "", None, 1])
async def test_set_ota_channel_rejects_invalid(quiet: Any, fake_gaggimate: FakeGaggiMate, key: Any) -> None:
    c = quiet
    with pytest.raises(ValueError):
        await c.set_ota_channel(key)
    await settle()
    assert fake_gaggimate.requests == []


async def test_set_ota_channel_blocked_while_updating(quiet: Any, fake_gaggimate: FakeGaggiMate) -> None:
    c = quiet
    progress(c, 3, 5)
    with pytest.raises(UpdateFailed):
        await c.set_ota_channel("nightly")
    await settle()
    assert fake_gaggimate.requests == []


# --- watchdog ------------------------------------------------------------------------------------
async def test_check_once_skips_stale_handling_in_grace_window(quiet: Any) -> None:
    c = quiet
    with patch(f"{MODULE}.WS_UNAVAILABLE_TIMEOUT", 1), patch.object(c, "_mark_stale", AsyncMock()) as stale:
        c._last_status_time = time.monotonic() - 100
        c._ota_grace_until = time.monotonic() + 50
        await c._check_once()
        stale.assert_not_awaited()
        # after expiry normal staleness applies immediately
        c._ota_grace_until = time.monotonic() - 0.01
        await c._check_once()
        stale.assert_awaited_once()


async def test_check_once_without_grace_is_unchanged(quiet: Any) -> None:
    c = quiet
    with patch(f"{MODULE}.WS_UNAVAILABLE_TIMEOUT", 1), patch.object(c, "_mark_stale", AsyncMock()) as stale:
        c._last_status_time = time.monotonic() - 100
        await c._check_once()
        stale.assert_awaited_once()


async def test_start_ota_grace_covers_the_watchdog(quiet: Any, hang: FakeGaggiMate) -> None:
    c = quiet
    await c.start_ota("display")
    with patch(f"{MODULE}.WS_UNAVAILABLE_TIMEOUT", 1), patch.object(c, "_mark_stale", AsyncMock()) as stale:
        c._last_status_time = time.monotonic() - 100
        await c._check_once()
        stale.assert_not_awaited()


async def test_terminal_phase_gives_fifteen_second_window(quiet: Any) -> None:
    c = quiet
    c._last_status_time = time.monotonic() - 100
    progress(c, 1, 1)
    progress(c, 4, 100)
    with patch(f"{MODULE}.WS_UNAVAILABLE_TIMEOUT", 15), patch.object(c, "_mark_stale", AsyncMock()) as stale:
        await c._check_once()
        stale.assert_not_awaited()  # window is fresh, grace cleared
        c._last_status_time = time.monotonic() - 100
        await c._check_once()
        stale.assert_awaited_once()  # no grace left: normal staleness


async def test_watchdog_tolerates_silent_update_then_expires(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    """Real watchdog loop: silence is fine while progress events arrive, stale once they stop."""
    with patch(f"{MODULE}.WS_UNAVAILABLE_TIMEOUT", 0.5), patch(f"{MODULE}.OTA_WATCHDOG_GRACE", 2.0):
        fake_gaggimate.ota_mode = "hang"
        fake_gaggimate.ota_script = []
        fake_gaggimate.silence_keepalive = True
        c = await setup_integration()
        seen: list[bool] = []
        c.async_add_listener(lambda: seen.append(c.available))
        await c.start_ota("display")
        await wait_until(lambda: fake_gaggimate.ota_run_active)
        for pct in range(0, 100, 20):  # ~2.4 s of silence apart from progress events
            await fake_gaggimate.push_ota_progress(1, pct)
            await asyncio.sleep(0.5)
        assert c.available is True
        assert False not in seen
        assert fake_gaggimate.connect_count == 1
        # events stop: after the grace window the normal watchdog drops the link
        await wait_until(lambda: fake_gaggimate.connect_count >= 2, timeout=8)
        assert False in seen


# --- end to end with the fake firmware -----------------------------------------------------------
async def test_update_with_reboot_end_to_end(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    fake_gaggimate.ota_settings = {
        **fake_gaggimate.ota_settings,
        **load_json("ota_settings")["variations"]["both_update_available"],
    }
    fake_gaggimate.ota_mode = "reboot"
    fake_gaggimate.ota_downtime = 0.4
    fake_gaggimate.silence_keepalive = True
    c = await setup_integration()
    assert c.ota_settings["displayVersion"] == "v1.7.0"
    seen: list[bool] = []
    c.async_add_listener(lambda: seen.append(c.available))

    await c.start_ota("both")
    await wait_until(lambda: c.ota_phase in (1, 4) or fake_gaggimate.connect_count > 1)
    await wait_until(lambda: fake_gaggimate.connect_count == 2 and c.available, timeout=10)
    await wait_until(lambda: c.ota_settings.get("displayVersion") == "v1.8.0")

    assert False in seen  # the whole device was unavailable during the reboot
    assert c.ota_settings["controllerVersion"] == "v1.8.0"
    assert c.ota_settings["displayUpdateAvailable"] is False
    assert c.ota_settings["updating"] is False
    assert c.ota_updating is False
    assert c.ota_phase is None and c.ota_percent is None and c._ota_ran is False
    assert c._ota_start_sent_at is None and c._ota_grace_until is None
    assert len(fake_gaggimate.requests_of(START)) == 1


async def test_update_noop_end_to_end(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    fake_gaggimate.ota_mode = "noop"
    c = await setup_integration()
    seen: list[bool] = []
    c.async_add_listener(lambda: seen.append(c.available))
    with patch(f"{MODULE}.OTA_REFRESH_DELAY", 0.05):
        await c.start_ota("display")
        await wait_until(lambda: fake_gaggimate.requests_of(START) != [])
        await wait_until(lambda: not fake_gaggimate.ota_run_active)
        await wait_until(lambda: c.ota_phase is None and not c.ota_updating)  # a status frame cleared phase 4
        await wait_until(lambda: c.ota_settings.get("updating") is False)
    await settle(0.3)
    assert c.available is True
    assert False not in seen
    assert fake_gaggimate.connect_count == 1
    assert c.ota_updating is False


async def test_update_failure_end_to_end(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    fake_gaggimate.ota_mode = "fail"
    fake_gaggimate.ota_script = [(1, 10)]
    fake_gaggimate.silence_keepalive = True
    c = await setup_integration()
    seen: list[bool] = []
    c.async_add_listener(lambda: seen.append(c.available))
    with patch(f"{MODULE}.OTA_REFRESH_DELAY", 0.05):
        await c.start_ota("controller")
        await wait_until(lambda: c.ota_phase == 5)
        assert c.ota_updating is False
        before = len(fake_gaggimate.requests_of(REQ_SETTINGS))
        await wait_until(lambda: len(fake_gaggimate.requests_of(REQ_SETTINGS)) > before)  # refresh
    assert c.ota_settings["updating"] is False
    assert c.ota_updating is False
    assert c.ota_phase == 5  # stays until the next run or reconnect
    assert c.available is True and False not in seen
    assert fake_gaggimate.connect_count == 1


async def test_connection_loss_mid_update_resets_state(
    hass: HomeAssistant, quiet: Any, fake_gaggimate: FakeGaggiMate
) -> None:
    c = quiet
    await c.start_ota("display")  # sets start flags and grace
    progress(c, 1, 40)
    settings(c, updating=True, channel="latest")
    old_settings = c.ota_settings
    assert c.ota_updating is True

    fake_gaggimate._refuse_until = asyncio.get_running_loop().time() + 60  # reconnect fails: observe the reset
    await fake_gaggimate.drop_connections()
    await wait_until(lambda: not c.available)

    assert c.ota_phase is None and c.ota_percent is None and c._ota_ran is False
    assert c._ota_start_sent_at is None and c._ota_grace_until is None
    assert c.ota_settings["updating"] is False
    assert c.ota_updating is False
    assert old_settings["updating"] is True  # replaced by a copy, not mutated
    assert c.ota_channel == "latest"  # the rest of the settings is kept


async def test_connection_loss_without_update_keeps_settings(quiet: Any, fake_gaggimate: FakeGaggiMate) -> None:
    c = quiet
    before = c.ota_settings
    fake_gaggimate._refuse_until = asyncio.get_running_loop().time() + 60
    await fake_gaggimate.drop_connections()
    await wait_until(lambda: not c.available)
    assert c.ota_settings is before
    assert c.ota_updating is False
