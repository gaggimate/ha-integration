"""The GaggiMate integration."""
from __future__ import annotations

import asyncio
import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    ATTR_MAX_SHOTS,
    DATA_SERVICES,
    DEFAULT_PORT,
    DOMAIN,
    SERVICE_TRIM_SHOT_HISTORY,
)
from .coordinator import GaggiMateCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_TRIM_SCHEMA = vol.Schema({vol.Required(ATTR_MAX_SHOTS): cv.positive_int})

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.BUTTON,
    Platform.NUMBER,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up GaggiMate from a config entry."""
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)

    coordinator = GaggiMateCoordinator(hass, host, port)

    try:
        await coordinator.async_start()
    except Exception as err:
        _LOGGER.error("Failed to connect to GaggiMate: %s", err)
        raise ConfigEntryNotReady from err

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _async_trim_shot_history(call: ServiceCall) -> None:
        """Trim shot history on all configured devices, keeping only newest max_shots entries each."""

        max_shots = call.data.get(ATTR_MAX_SHOTS)
        if not isinstance(max_shots, int) or max_shots <= 0:
            raise ValueError("max_shots must be a positive integer")

        coordinators: list[GaggiMateCoordinator] = list(hass.data.get(DOMAIN, {}).values())
        if not coordinators:
            raise ValueError("No GaggiMate devices are configured")

        # Process each coordinator independently
        for coordinator in coordinators:
            history = await coordinator.request_history_list()

            # Sort by timestamp ascending to delete oldest first; fall back to ID when timestamp missing
            def _sort_key(item: dict) -> tuple:
                ts = item.get("timestamp")
                try:
                    ts_val = int(ts)
                except (TypeError, ValueError):
                    ts_val = 0
                try:
                    id_val = int(item.get("id"))
                except (TypeError, ValueError):
                    id_val = 0
                return ts_val, id_val

            sorted_history = sorted(history, key=_sort_key)

            if len(sorted_history) <= max_shots:
                _LOGGER.info(
                    "Shot history trim skipped for %s: %s entries <= max_shots=%s",
                    coordinator.host,
                    len(sorted_history),
                    max_shots,
                )
                continue

            to_delete = sorted_history[:-max_shots]
            deleted = 0
            failures: list[str] = []
            for idx, item in enumerate(to_delete, start=1):
                shot_id = item.get("id")
                if shot_id is None:
                    continue
                try:
                    await coordinator.delete_history_item(shot_id)
                    deleted += 1
                except Exception as err:  # noqa: BLE001 - best-effort cleanup
                    failures.append(f"{shot_id}: {err}")
                # Yield to event loop between deletions and avoid hammering device
                if idx % 10 == 0:
                    await asyncio.sleep(0.1)
                else:
                    await asyncio.sleep(0)

            msg = (
                "Trimmed shot history for %s, kept %s newest, deleted %s older entries"
                % (coordinator.host, max_shots, deleted)
            )
            if failures:
                _LOGGER.warning("%s; failed deletions: %s", msg, "; ".join(failures))
            else:
                _LOGGER.info(msg)

    # Register the service once globally
    if DATA_SERVICES not in hass.data:
        hass.data[DATA_SERVICES] = 0

    if hass.data[DATA_SERVICES] == 0:
        hass.services.async_register(
            DOMAIN,
            SERVICE_TRIM_SHOT_HISTORY,
            _async_trim_shot_history,
            schema=SERVICE_TRIM_SCHEMA,
        )
    hass.data[DATA_SERVICES] += 1

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: GaggiMateCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

        # Remove service handler when last entry is removed
        if DATA_SERVICES in hass.data:
            hass.data[DATA_SERVICES] -= 1
            if hass.data[DATA_SERVICES] <= 0:
                hass.services.async_remove(DOMAIN, SERVICE_TRIM_SHOT_HISTORY)
                hass.data.pop(DATA_SERVICES, None)

    return unload_ok
