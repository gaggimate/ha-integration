"""Update platform (chg003 step 2): three firmware update entities fed by the coordinator's OTA state.

Most tests feed frames straight into ``coordinator._process_message`` with the fake keepalive off;
the end-to-end ones let the fake server emulate a whole update (see the OTA block in fake_server.py).
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import SetupIntegration, record_states, settle, wait_until
from .fake_server import FakeGaggiMate, load_json

HOST = "127.0.0.1"
MODULE = "custom_components.gaggimate.coordinator"
PROGRESS = "evt:ota-progress"
SETTINGS = "res:ota-settings"
START = "req:ota-start"

DISPLAY = "display_firmware_update"
CONTROLLER = "controller_firmware_update"
BOTH = "firmware_update"
KEYS = (DISPLAY, CONTROLLER, BOTH)
VARIATIONS = load_json("ota_settings")["variations"]


def eid(hass: HomeAssistant, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("update", "gaggimate", f"{HOST}_{key}")
    assert entity_id is not None, f"update {key} not registered"
    return entity_id


def st(hass: HomeAssistant, key: str) -> Any:
    state = hass.states.get(eid(hass, key))
    assert state is not None
    return state


def feed_settings(c: Any, **fields: Any) -> None:
    """Feed a full res:ota-settings frame (the firmware always sends all fields): current values + changes."""
    c._process_message({**c.ota_settings, "tp": SETTINGS, **fields})


def feed_progress(c: Any, phase: int, progress: int | None = None) -> None:
    frame: dict[str, Any] = {"tp": PROGRESS, "phase": phase}
    if progress is not None:
        frame["progress"] = progress
    c._process_message(frame)


@pytest.fixture(autouse=True)
def _slow_ota_refresh() -> Any:
    with patch(f"{MODULE}.OTA_REFRESH_DELAY", 30):
        yield


@pytest.fixture
async def quiet(hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration) -> Any:
    """Integration set up, fake keepalive off, no requests recorded yet."""
    fake_gaggimate.keepalive = False
    fake_gaggimate.ota_mode = "hang"
    fake_gaggimate.ota_script = []
    c = await setup_integration()
    fake_gaggimate.clear_requests()
    return c


# --- creation ------------------------------------------------------------------------------------
async def test_entities_created_with_registry_properties(hass: HomeAssistant, quiet: Any) -> None:
    registry = er.async_get(hass)
    names = {
        DISPLAY: "Display Firmware Update",
        CONTROLLER: "Controller Firmware Update",
        BOTH: "Display and Controller Firmware Update",
    }
    for key in KEYS:
        entry = registry.async_get(eid(hass, key))
        assert entry.unique_id == f"{HOST}_{key}"
        assert entry.entity_category is None
        assert entry.disabled_by is None
        assert entry.original_device_class == "firmware"
        assert entry.original_name == names[key]
        attrs = st(hass, key).attributes
        assert attrs["device_class"] == "firmware"
        assert attrs["supported_features"] == 1 | 4  # INSTALL | PROGRESS
        assert attrs["friendly_name"].endswith(names[key])
    assert len({eid(hass, k) for k in KEYS}) == 3


async def test_no_update_entities_without_up_then_late_creation(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
) -> None:
    fake_gaggimate.state = {k: v for k, v in fake_gaggimate.state.items() if k != "up"}
    fake_gaggimate.telemetry = {k: v for k, v in fake_gaggimate.telemetry.items() if k != "up"}
    await setup_integration()
    assert not [e for e in er.async_entries_for_config_entry(er.async_get(hass), mock_config_entry.entry_id) if e.domain == "update"]
    assert hass.states.async_entity_ids("update") == []

    await fake_gaggimate.push_state(up=False)
    await wait_until(lambda: len(hass.states.async_entity_ids("update")) == 3)
    for key in KEYS:
        assert st(hass, key).state != "unavailable"

    # later frames never duplicate or remove entities (add-only)
    await fake_gaggimate.push_state(up=True)
    await settle()
    assert len(hass.states.async_entity_ids("update")) == 3
    assert len([e for e in er.async_entries_for_config_entry(er.async_get(hass), mock_config_entry.entry_id) if e.domain == "update"]) == 3


async def test_no_duplicates_after_reload(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, quiet: Any
) -> None:
    for _ in range(2):
        assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        c = hass.data["gaggimate"][mock_config_entry.entry_id]
        await wait_until(lambda: c.available)
        await settle()
        entries = [e for e in er.async_entries_for_config_entry(er.async_get(hass), mock_config_entry.entry_id) if e.domain == "update"]
        assert sorted(e.unique_id for e in entries) == sorted(f"{HOST}_{k}" for k in KEYS)
        assert len(hass.states.async_entity_ids("update")) == 3


# --- state ---------------------------------------------------------------------------------------
async def test_state_off_when_up_to_date(hass: HomeAssistant, quiet: Any) -> None:
    for key in KEYS:
        s = st(hass, key)
        assert s.state == "off"
        assert s.attributes["installed_version"] == "v1.7.0"
        assert s.attributes["latest_version"] == "v1.7.0"
        assert s.attributes["in_progress"] is False
        assert s.attributes["update_percentage"] is None


async def test_display_update_available(hass: HomeAssistant, quiet: Any) -> None:
    feed_settings(quiet, **VARIATIONS["display_update_available"])
    await settle()
    d, c, b = (st(hass, k) for k in KEYS)
    assert d.state == "on" and d.attributes["latest_version"] == "v1.8.0"
    assert c.state == "off" and c.attributes["latest_version"] == "v1.7.0"
    assert b.state == "on" and b.attributes["latest_version"] == "v1.8.0"


async def test_controller_only_update_available(hass: HomeAssistant, quiet: Any) -> None:
    feed_settings(quiet, latestVersion="v1.8.0", controllerUpdateAvailable=True)
    await settle()
    assert st(hass, DISPLAY).state == "off"
    assert st(hass, CONTROLLER).state == "on"
    assert st(hass, BOTH).state == "on"


async def test_both_update_available(hass: HomeAssistant, quiet: Any) -> None:
    feed_settings(quiet, **VARIATIONS["both_update_available"])
    await settle()
    assert [st(hass, k).state for k in KEYS] == ["on", "on", "on"]


async def test_unknown_when_flags_absent(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    fake_gaggimate: FakeGaggiMate,
    setup_integration: SetupIntegration,
) -> None:
    """Older firmware: res:ota-settings without update flags -> unknown (version known, latest unknown)."""
    fake_gaggimate.ota_settings = {"tp": SETTINGS, "displayVersion": "v1.0.0", "controllerVersion": "v1.0.0"}
    await setup_integration()
    for key in KEYS:
        assert st(hass, key).state == "unknown"


async def test_unknown_when_versions_missing(hass: HomeAssistant, quiet: Any) -> None:
    quiet._ota_settings = {"tp": SETTINGS}
    quiet.async_update_listeners()
    await settle()
    for key in KEYS:
        assert st(hass, key).state == "unknown"


async def test_unknown_before_any_ota_settings(hass: HomeAssistant, quiet: Any) -> None:
    quiet._ota_settings = {}
    quiet.async_update_listeners()
    await settle()
    for key in KEYS:
        assert st(hass, key).state == "unknown"


async def test_mismatched_versions_give_combined_string_for_both(hass: HomeAssistant, quiet: Any) -> None:
    feed_settings(quiet, displayVersion="v1.7.0", controllerVersion="v1.6.0")
    await settle()
    assert st(hass, DISPLAY).attributes["installed_version"] == "v1.7.0"
    assert st(hass, CONTROLLER).attributes["installed_version"] == "v1.6.0"
    assert st(hass, BOTH).attributes["installed_version"] == "Display v1.7.0, Controller v1.6.0"


async def test_both_unknown_if_one_version_missing(hass: HomeAssistant, quiet: Any) -> None:
    quiet._ota_settings = {k: v for k, v in quiet.ota_settings.items() if k != "controllerVersion"}
    quiet.async_update_listeners()
    await settle()
    assert st(hass, BOTH).state == "unknown"
    assert st(hass, DISPLAY).state == "off"
    assert st(hass, CONTROLLER).state == "unknown"


async def test_nightly_tags_update_available(hass: HomeAssistant, quiet: Any) -> None:
    feed_settings(quiet, **VARIATIONS["nightly_update_available"])
    await settle()
    for key in KEYS:
        s = st(hass, key)
        assert s.state == "on"
        assert s.attributes["installed_version"] == "v1.7.0"
        assert s.attributes["latest_version"] == "nightly-20261123"


async def test_flag_true_with_equal_version_is_off(hass: HomeAssistant, quiet: Any) -> None:
    """Documented limit: the flag is true but latestVersion equals the installed string."""
    feed_settings(quiet, latestVersion="v1.7.0", displayUpdateAvailable=True)
    await settle()
    assert st(hass, DISPLAY).state == "off"


async def test_non_string_versions_are_unknown(hass: HomeAssistant, quiet: Any) -> None:
    feed_settings(quiet, displayVersion=170, controllerVersion="", displayUpdateAvailable=True, latestVersion=None)
    await settle()
    assert st(hass, DISPLAY).state == "unknown"
    assert st(hass, CONTROLLER).state == "unknown"


# --- install -------------------------------------------------------------------------------------
async def install(hass: HomeAssistant, key: str, **data: Any) -> None:
    await hass.services.async_call("update", "install", {"entity_id": eid(hass, key), **data}, blocking=True)


@pytest.mark.parametrize(("key", "cp"), [(DISPLAY, "display"), (CONTROLLER, "controller"), (BOTH, "both")])
async def test_install_sends_right_component(
    hass: HomeAssistant, quiet: Any, fake_gaggimate: FakeGaggiMate, key: str, cp: str
) -> None:
    feed_settings(quiet, **VARIATIONS["both_update_available"])
    await install(hass, key)
    await fake_gaggimate.wait_for_request(START)
    assert fake_gaggimate.requests_of(START) == [{"tp": START, "cp": cp}]
    # a settings refresh follows the install
    await fake_gaggimate.wait_for_request("req:ota-settings")
    await wait_until(lambda: st(hass, key).attributes["in_progress"] is True)


async def test_install_with_version_or_backup_is_rejected_by_ha(
    hass: HomeAssistant, quiet: Any, fake_gaggimate: FakeGaggiMate
) -> None:
    """Neither SPECIFIC_VERSION nor BACKUP is supported, so HA refuses before the entity is called."""
    feed_settings(quiet, **VARIATIONS["display_update_available"])
    with pytest.raises(HomeAssistantError):
        await install(hass, DISPLAY, version="v9.9.9")
    with pytest.raises(HomeAssistantError):
        await install(hass, DISPLAY, backup=True)
    await settle()
    assert fake_gaggimate.requests_of(START) == []


async def test_async_install_ignores_version_and_backup_args(hass: HomeAssistant, quiet: Any, fake_gaggimate: FakeGaggiMate) -> None:
    feed_settings(quiet, **VARIATIONS["display_update_available"])
    entity = hass.data["update"].get_entity(eid(hass, DISPLAY))
    await entity.async_install("v9.9.9", True)
    await fake_gaggimate.wait_for_request(START)
    assert fake_gaggimate.requests_of(START) == [{"tp": START, "cp": "display"}]


async def test_install_twice_quickly_one_request(hass: HomeAssistant, quiet: Any, fake_gaggimate: FakeGaggiMate) -> None:
    feed_settings(quiet, **VARIATIONS["both_update_available"])
    await install(hass, DISPLAY)
    with pytest.raises(HomeAssistantError):
        await install(hass, DISPLAY)
    with pytest.raises(HomeAssistantError):
        await install(hass, BOTH)
    await settle(0.2)
    assert len(fake_gaggimate.requests_of(START)) == 1


async def test_install_concurrent_one_request(hass: HomeAssistant, quiet: Any, fake_gaggimate: FakeGaggiMate) -> None:
    feed_settings(quiet, **VARIATIONS["both_update_available"])
    results = await asyncio.gather(install(hass, DISPLAY), install(hass, CONTROLLER), return_exceptions=True)
    assert sum(isinstance(r, HomeAssistantError) for r in results) == 1
    await settle(0.2)
    assert len(fake_gaggimate.requests_of(START)) == 1


async def test_install_while_updating_raises_and_sends_nothing(
    hass: HomeAssistant, quiet: Any, fake_gaggimate: FakeGaggiMate
) -> None:
    feed_progress(quiet, 1, 10)
    for key in KEYS:
        with pytest.raises(HomeAssistantError):
            await install(hass, key)
    await settle()
    assert fake_gaggimate.requests_of(START) == []


@pytest.mark.frames("dual_boiler_brewing")
async def test_install_works_during_brew(hass: HomeAssistant, quiet: Any, fake_gaggimate: FakeGaggiMate) -> None:
    feed_settings(quiet, **VARIATIONS["both_update_available"])
    await install(hass, BOTH)
    await fake_gaggimate.wait_for_request(START)
    assert fake_gaggimate.requests_of(START) == [{"tp": START, "cp": "both"}]


async def test_install_failure_surfaces_as_ha_error(hass: HomeAssistant, quiet: Any) -> None:
    feed_settings(quiet, **VARIATIONS["display_update_available"])
    with patch.object(quiet, "send_message", side_effect=UpdateFailed("boom")):
        with pytest.raises(HomeAssistantError):
            await install(hass, DISPLAY)
    assert quiet.ota_updating is False
    assert st(hass, DISPLAY).attributes["in_progress"] is False


# --- progress ------------------------------------------------------------------------------------
async def test_display_progress(hass: HomeAssistant, quiet: Any) -> None:
    feed_settings(quiet, **VARIATIONS["both_update_available"])
    for pct in (0, 50, 100):
        feed_progress(quiet, 1, pct)
        await settle()
        d, c, b = (st(hass, k) for k in KEYS)
        assert d.attributes["in_progress"] is True and d.attributes["update_percentage"] == pct
        assert b.attributes["in_progress"] is True and b.attributes["update_percentage"] == pct
        # firmware does not say which component flashes: all three show in_progress, controller no percent
        assert c.attributes["in_progress"] is True and c.attributes["update_percentage"] is None


async def test_controller_phase_progress(hass: HomeAssistant, quiet: Any) -> None:
    feed_settings(quiet, **VARIATIONS["both_update_available"])
    feed_progress(quiet, 3, 40)
    await settle()
    d, c, b = (st(hass, k) for k in KEYS)
    assert c.attributes["in_progress"] is True and c.attributes["update_percentage"] == 40
    assert b.attributes["update_percentage"] == 40
    assert d.attributes["in_progress"] is True and d.attributes["update_percentage"] is None


async def test_both_shows_both_phases(hass: HomeAssistant, quiet: Any) -> None:
    feed_settings(quiet, **VARIATIONS["both_update_available"])
    feed_progress(quiet, 3, 90)
    await settle()
    assert st(hass, BOTH).attributes["update_percentage"] == 90
    feed_progress(quiet, 1, 10)
    await settle()
    assert st(hass, BOTH).attributes["update_percentage"] == 10


async def test_phase_4_shows_100_until_drop(hass: HomeAssistant, quiet: Any) -> None:
    feed_settings(quiet, **VARIATIONS["both_update_available"])
    feed_progress(quiet, 1, 100)
    feed_progress(quiet, 4, 100)
    await settle()
    for key in KEYS:
        s = st(hass, key)
        assert s.attributes["in_progress"] is True
        assert s.attributes["update_percentage"] == 100


async def test_phase_5_clears_progress_state_stays_on(hass: HomeAssistant, quiet: Any) -> None:
    feed_settings(quiet, **VARIATIONS["both_update_available"])
    feed_progress(quiet, 1, 30)
    feed_progress(quiet, 5, 0)
    await settle()
    for key in KEYS:
        s = st(hass, key)
        assert s.state == "on"
        assert s.attributes["in_progress"] is False
        assert s.attributes["update_percentage"] is None


async def test_noop_phase_4_not_in_progress(hass: HomeAssistant, quiet: Any) -> None:
    feed_progress(quiet, 4, 100)
    await settle()
    for key in KEYS:
        s = st(hass, key)
        assert s.attributes["in_progress"] is False
        assert s.attributes["update_percentage"] is None


async def test_update_started_elsewhere_shows_in_progress(hass: HomeAssistant, quiet: Any) -> None:
    feed_settings(quiet, updating=True)
    await settle()
    for key in KEYS:
        assert st(hass, key).attributes["in_progress"] is True
        assert st(hass, key).attributes["update_percentage"] is None


# --- end to end with the fake firmware -----------------------------------------------------------
async def test_reboot_end_to_end(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    fake_gaggimate.ota_settings = {**fake_gaggimate.ota_settings, **VARIATIONS["both_update_available"]}
    fake_gaggimate.ota_mode = "reboot"
    fake_gaggimate.ota_downtime = 0.4
    fake_gaggimate.silence_keepalive = True
    c = await setup_integration()
    ids = [eid(hass, k) for k in KEYS]
    history = record_states(hass, *ids)
    assert st(hass, BOTH).state == "on"

    await install(hass, BOTH)
    await wait_until(lambda: fake_gaggimate.connect_count == 2 and c.available, timeout=10)
    await wait_until(lambda: c.ota_settings.get("displayVersion") == "v1.8.0")
    await settle(0.2)

    for entity_id in ids:
        assert "unavailable" in history[entity_id]
        assert "on" in history[entity_id]
    for key in KEYS:
        s = st(hass, key)
        assert s.state == "off"
        assert s.attributes["installed_version"] == "v1.8.0"
        assert s.attributes["in_progress"] is False
        assert s.attributes["update_percentage"] is None


async def test_noop_end_to_end_stays_available(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    fake_gaggimate.ota_settings = {**fake_gaggimate.ota_settings, **VARIATIONS["display_update_available"]}
    fake_gaggimate.ota_mode = "noop"
    c = await setup_integration()
    ids = [eid(hass, k) for k in KEYS]
    history = record_states(hass, *ids)
    with patch(f"{MODULE}.OTA_REFRESH_DELAY", 0.05):
        await install(hass, DISPLAY)
        await wait_until(lambda: not fake_gaggimate.ota_run_active)
        await wait_until(lambda: c.ota_phase is None and not c.ota_updating)
    await settle(0.3)
    assert fake_gaggimate.connect_count == 1
    for entity_id in ids:
        assert "unavailable" not in history[entity_id]
    for key in KEYS:
        assert st(hass, key).attributes["in_progress"] is False
        assert st(hass, key).state in ("on", "off")


async def test_failure_end_to_end(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    fake_gaggimate.ota_settings = {**fake_gaggimate.ota_settings, **VARIATIONS["display_update_available"]}
    fake_gaggimate.ota_mode = "fail"
    fake_gaggimate.ota_script = [(1, 10)]
    fake_gaggimate.silence_keepalive = True
    c = await setup_integration()
    with patch(f"{MODULE}.OTA_REFRESH_DELAY", 0.05):
        await install(hass, DISPLAY)
        await wait_until(lambda: c.ota_phase == 5)
        await wait_until(lambda: c.ota_settings.get("updating") is False and not c.ota_updating)
    await settle(0.2)
    s = st(hass, DISPLAY)
    assert s.state == "on"
    assert s.attributes["in_progress"] is False
    assert fake_gaggimate.connect_count == 1


async def test_check_result_updates_entities(hass: HomeAssistant, quiet: Any, fake_gaggimate: FakeGaggiMate) -> None:
    await fake_gaggimate.set_ota_settings(**VARIATIONS["display_update_available"])
    await wait_until(lambda: st(hass, DISPLAY).state == "on")
    assert st(hass, CONTROLLER).state == "off"
