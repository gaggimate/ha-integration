"""Check for Updates button and Update Channel select (chg003 step 3).

Most tests feed frames straight into ``coordinator._process_message``; the end-to-end ones let the
fake firmware answer requests and broadcast results (see the OTA block in fake_server.py).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gaggimate.const import MODE_NAMES

from .conftest import SetupIntegration, record_states, settle, wait_until
from .fake_server import FakeGaggiMate, load_json

HOST = "127.0.0.1"
MODULE = "custom_components.gaggimate.coordinator"
SETTINGS = "res:ota-settings"
REQ_SETTINGS = "req:ota-settings"
PROGRESS = "evt:ota-progress"

CHECK = "check_for_updates"
CHANNEL = "update_channel"
DISPLAY = "display_firmware_update"
CONTROLLER = "controller_firmware_update"
BOTH = "firmware_update"
UPDATE_KEYS = (DISPLAY, CONTROLLER, BOTH)
VARIATIONS = load_json("ota_settings")["variations"]


def eid(hass: HomeAssistant, domain: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, "gaggimate", f"{HOST}_{key}")
    assert entity_id is not None, f"{domain} {key} not registered"
    return entity_id


def st(hass: HomeAssistant, domain: str, key: str) -> Any:
    state = hass.states.get(eid(hass, domain, key))
    assert state is not None
    return state


def feed_settings(c: Any, **fields: Any) -> None:
    """Feed a full res:ota-settings frame: the current values plus the changes."""
    c._process_message({**c.ota_settings, "tp": SETTINGS, **fields})


def drop_channel(c: Any) -> None:
    c._ota_settings = {k: v for k, v in c.ota_settings.items() if k != "channel"}
    c.async_update_listeners()


def feed_progress(c: Any, phase: int, progress: int | None = None) -> None:
    frame: dict[str, Any] = {"tp": PROGRESS, "phase": phase}
    if progress is not None:
        frame["progress"] = progress
    c._process_message(frame)


async def select_option(hass: HomeAssistant, option: str) -> None:
    await hass.services.async_call(
        "select", "select_option", {"entity_id": eid(hass, "select", CHANNEL), "option": option}, blocking=True
    )


async def press(hass: HomeAssistant) -> None:
    await hass.services.async_call("button", "press", {"entity_id": eid(hass, "button", CHECK)}, blocking=True)


def control_unique_ids(hass: HomeAssistant, entry_id: str) -> list[str]:
    return sorted(
        e.unique_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry_id)
        if e.domain in ("button", "select") and e.unique_id in (f"{HOST}_{CHECK}", f"{HOST}_{CHANNEL}")
    )


@pytest.fixture(autouse=True)
def _slow_ota_refresh() -> Any:
    with patch(f"{MODULE}.OTA_REFRESH_DELAY", 30):
        yield


@pytest.fixture
async def quiet(hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration) -> Any:
    """Integration set up, fake keepalive off, no update running, no requests recorded yet."""
    fake_gaggimate.keepalive = False
    fake_gaggimate.ota_mode = "hang"
    fake_gaggimate.ota_script = []
    c = await setup_integration()
    fake_gaggimate.clear_requests()
    return c


# --- creation --------------------------------------------------------------------------------------
async def test_controls_created_with_registry_properties(hass: HomeAssistant, quiet: Any) -> None:
    registry = er.async_get(hass)
    for domain, key, name in (("button", CHECK, "Check for Updates"), ("select", CHANNEL, "Update Channel")):
        entry = registry.async_get(eid(hass, domain, key))
        assert entry.unique_id == f"{HOST}_{key}"
        assert entry.entity_category is EntityCategory.CONFIG
        assert entry.disabled_by is None
        assert entry.original_name == name
        assert st(hass, domain, key).attributes["friendly_name"].endswith(name)


async def test_controls_absent_without_up_then_late_creation_no_duplicates(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
) -> None:
    fake_gaggimate.state = {k: v for k, v in fake_gaggimate.state.items() if k != "up"}
    fake_gaggimate.telemetry = {k: v for k, v in fake_gaggimate.telemetry.items() if k != "up"}
    c = await setup_integration()
    assert control_unique_ids(hass, mock_config_entry.entry_id) == []
    assert c.ota_settings  # OTA settings still arrive without `up`

    await fake_gaggimate.push_state(up=False)
    await wait_until(lambda: len(control_unique_ids(hass, mock_config_entry.entry_id)) == 2)
    assert control_unique_ids(hass, mock_config_entry.entry_id) == sorted(f"{HOST}_{k}" for k in (CHECK, CHANNEL))

    # later frames never duplicate or remove the controls
    await fake_gaggimate.push_state(up=True)
    await settle()
    assert len(control_unique_ids(hass, mock_config_entry.entry_id)) == 2


async def test_controls_no_duplicates_after_reload(hass: HomeAssistant, mock_config_entry: MockConfigEntry, quiet: Any) -> None:
    expected = sorted(f"{HOST}_{k}" for k in (CHECK, CHANNEL))
    for _ in range(2):
        assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        c = hass.data["gaggimate"][mock_config_entry.entry_id]
        await wait_until(lambda: c.available)
        await settle()
        assert control_unique_ids(hass, mock_config_entry.entry_id) == expected
        assert st(hass, "button", CHECK).state != "unavailable"
        assert st(hass, "select", CHANNEL).state == "Stable"


# --- channel select: options and current option ----------------------------------------------------
async def test_channel_options_exact_and_ordered(hass: HomeAssistant, quiet: Any) -> None:
    assert st(hass, "select", CHANNEL).attributes["options"] == ["Stable", "Nightly"]


@pytest.mark.parametrize(("channel", "expected"), [("latest", "Stable"), ("nightly", "Nightly")])
async def test_current_option_maps_channel(hass: HomeAssistant, quiet: Any, channel: str, expected: str) -> None:
    feed_settings(quiet, channel=channel)
    await settle()
    assert st(hass, "select", CHANNEL).state == expected


@pytest.mark.parametrize("case", ["missing", "unrecognised", "not_a_string"])
async def test_current_option_unknown_when_channel_not_usable(hass: HomeAssistant, quiet: Any, case: str) -> None:
    if case == "missing":
        drop_channel(quiet)
    elif case == "unrecognised":
        feed_settings(quiet, channel="beta")
    else:
        feed_settings(quiet, channel=1)
    await settle()
    assert st(hass, "select", CHANNEL).state == "unknown"


# --- channel select: actions -----------------------------------------------------------------------
async def test_select_nightly_sends_channel_and_state_follows_broadcast(
    hass: HomeAssistant, quiet: Any, fake_gaggimate: FakeGaggiMate
) -> None:
    await select_option(hass, "Nightly")
    await fake_gaggimate.wait_for_request(REQ_SETTINGS)
    assert fake_gaggimate.requests_of(REQ_SETTINGS) == [{"tp": REQ_SETTINGS, "update": True, "channel": "nightly"}]
    await wait_until(lambda: st(hass, "select", CHANNEL).state == "Nightly")
    assert quiet.ota_channel == "nightly"

    # and back: Stable sends the firmware key "latest"
    await select_option(hass, "Stable")
    await wait_until(lambda: len(fake_gaggimate.requests_of(REQ_SETTINGS)) == 2)
    assert fake_gaggimate.requests_of(REQ_SETTINGS)[1] == {"tp": REQ_SETTINGS, "update": True, "channel": "latest"}
    await wait_until(lambda: st(hass, "select", CHANNEL).state == "Stable")


async def test_select_is_not_optimistic(hass: HomeAssistant, quiet: Any) -> None:
    with patch.object(quiet, "send_message", new=AsyncMock()) as send:
        await select_option(hass, "Nightly")
    send.assert_awaited_once_with({"tp": REQ_SETTINGS, "update": True, "channel": "nightly"})
    await settle()
    # the request was sent but the firmware has not confirmed it: the select keeps the reported channel
    assert st(hass, "select", CHANNEL).state == "Stable"

    feed_settings(quiet, channel="nightly")
    await settle()
    assert st(hass, "select", CHANNEL).state == "Nightly"


async def test_invalid_option_raises_and_sends_nothing(hass: HomeAssistant, quiet: Any, fake_gaggimate: FakeGaggiMate) -> None:
    with pytest.raises(ServiceValidationError):
        await select_option(hass, "Beta")
    entity = hass.data["select"].get_entity(eid(hass, "select", CHANNEL))
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("Beta")
    await settle()
    assert fake_gaggimate.requests_of(REQ_SETTINGS) == []
    assert quiet.ota_channel == "latest"


# --- check for updates button ----------------------------------------------------------------------
@pytest.mark.parametrize("channel", ["latest", "nightly"])
async def test_check_press_sends_current_channel(
    hass: HomeAssistant, quiet: Any, fake_gaggimate: FakeGaggiMate, channel: str
) -> None:
    feed_settings(quiet, channel=channel)
    await settle()
    await press(hass)
    await fake_gaggimate.wait_for_request(REQ_SETTINGS)
    assert fake_gaggimate.requests_of(REQ_SETTINGS) == [{"tp": REQ_SETTINGS, "update": True, "channel": channel}]


async def test_check_result_broadcast_updates_sensors_and_update_entities(
    hass: HomeAssistant, quiet: Any, fake_gaggimate: FakeGaggiMate
) -> None:
    fake_gaggimate.ota_check_result = {"latestVersion": "v1.8.0", "displayUpdateAvailable": True}
    await press(hass)
    await wait_until(lambda: st(hass, "sensor", "latest_software_version").state == "v1.8.0")
    await wait_until(lambda: st(hass, "update", DISPLAY).state == "on")
    # installed version is unchanged by a check; the update entities follow the flags
    assert st(hass, "sensor", "software_display_version").state == "v1.7.0"
    assert st(hass, "sensor", "display_software_update").state == "True"
    assert st(hass, "sensor", "controller_software_update").state == "False"
    assert st(hass, "update", CONTROLLER).state == "off"
    assert st(hass, "update", BOTH).state == "on"
    assert st(hass, "update", BOTH).attributes["latest_version"] == "v1.8.0"


async def test_check_result_both_flags_and_nightly_tag(hass: HomeAssistant, quiet: Any, fake_gaggimate: FakeGaggiMate) -> None:
    fake_gaggimate.ota_check_result = {k: v for k, v in VARIATIONS["nightly_update_available"].items() if k != "channel"}
    feed_settings(quiet, channel="nightly")
    await settle()
    await press(hass)
    await wait_until(lambda: st(hass, "sensor", "latest_software_version").state == "nightly-20261123")
    assert [st(hass, "update", k).state for k in UPDATE_KEYS] == ["on", "on", "on"]
    assert st(hass, "select", CHANNEL).state == "Nightly"


async def test_check_press_unknown_channel_raises_and_sends_nothing(
    hass: HomeAssistant, quiet: Any, fake_gaggimate: FakeGaggiMate
) -> None:
    drop_channel(quiet)
    await settle()
    entity = hass.data["button"].get_entity(eid(hass, "button", CHECK))
    with pytest.raises(UpdateFailed):
        await entity.async_press()
    with pytest.raises(HomeAssistantError):
        await press(hass)
    await settle()
    assert fake_gaggimate.requests_of(REQ_SETTINGS) == []


async def test_check_press_while_updating_raises_and_sends_nothing(
    hass: HomeAssistant, quiet: Any, fake_gaggimate: FakeGaggiMate
) -> None:
    feed_progress(quiet, 1, 10)
    await settle()
    button = hass.data["button"].get_entity(eid(hass, "button", CHECK))
    select = hass.data["select"].get_entity(eid(hass, "select", CHANNEL))
    with pytest.raises(UpdateFailed):
        await button.async_press()
    with pytest.raises(UpdateFailed):
        await select.async_select_option("Nightly")
    await settle()
    assert fake_gaggimate.requests_of(REQ_SETTINGS) == []


# --- availability ----------------------------------------------------------------------------------
async def test_controls_unavailable_while_update_runs(hass: HomeAssistant, quiet: Any) -> None:
    for phase, pct in ((1, 10), (3, 50)):
        feed_progress(quiet, phase, pct)
        await settle()
        assert st(hass, "button", CHECK).state == "unavailable"
        assert st(hass, "select", CHANNEL).state == "unavailable"

    # the failed-update phase ends the run: the controls come back
    feed_progress(quiet, 5, 0)
    await settle()
    assert st(hass, "button", CHECK).state != "unavailable"
    assert st(hass, "select", CHANNEL).state == "Stable"


async def test_controls_unavailable_during_reboot_then_back(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    fake_gaggimate.ota_mode = "reboot"
    fake_gaggimate.ota_downtime = 0.4
    fake_gaggimate.silence_keepalive = True
    c = await setup_integration()
    ids = [eid(hass, "button", CHECK), eid(hass, "select", CHANNEL)]
    history = record_states(hass, *ids)

    await c.start_ota("both")
    await wait_until(lambda: fake_gaggimate.connect_count == 2 and c.available and not c.ota_updating, timeout=10)
    await settle(0.2)

    for entity_id in ids:
        assert "unavailable" in history[entity_id]
    assert st(hass, "button", CHECK).state != "unavailable"
    assert st(hass, "select", CHANNEL).state == "Stable"


async def test_mode_and_profile_selects_stay_available_during_update(hass: HomeAssistant, quiet: Any) -> None:
    feed_progress(quiet, 1, 10)
    await settle()
    assert st(hass, "select", "mode_select").state != "unavailable"
    assert st(hass, "select", "profile_select").state != "unavailable"


# --- Mode and Profile selects unchanged -----------------------------------------------------------
async def test_mode_and_profile_selects_unchanged(hass: HomeAssistant, quiet: Any) -> None:
    mode = st(hass, "select", "mode_select")
    assert mode.attributes["options"] == list(MODE_NAMES.values())  # single boiler: no filtering
    assert mode.state == "Brew"
    profile = st(hass, "select", "profile_select")
    assert profile.attributes["options"] == ["Default", "Flush Utility", "Lever Style"]
    assert profile.state == "Default"


@pytest.mark.frames("dual_boiler_idle")
async def test_mode_options_dual_boiler_unchanged(hass: HomeAssistant, setup_integration: SetupIntegration) -> None:
    await setup_integration()
    assert st(hass, "select", "mode_select").attributes["options"] == ["Standby", "Brew", "Grind"]
