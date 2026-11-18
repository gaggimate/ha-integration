"""The GaggiMate integration."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    ATTR_MAX_SHOTS,
    ATTR_KEEP_ANNOTATED,
    DATA_SERVICES,
    DEFAULT_PORT,
    DOMAIN,
    SERVICE_TRIM_SHOT_HISTORY,
)
from .coordinator import GaggiMateCoordinator
from .history import async_trim_shot_history

_LOGGER = logging.getLogger(__name__)

SERVICE_TRIM_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MAX_SHOTS): cv.positive_int,
        vol.Optional(ATTR_KEEP_ANNOTATED, default=False): cv.boolean,
    }
)

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

        coordinators: list[GaggiMateCoordinator] = list(hass.data.get(DOMAIN, {}).values())
        await async_trim_shot_history(
            coordinators,
            call.data.get(ATTR_MAX_SHOTS),
            bool(call.data.get(ATTR_KEEP_ANNOTATED)),
        )

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
