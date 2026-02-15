"""Button platform for GaggiMate integration."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, UNIQUE_ID_BREW_START, UNIQUE_ID_BREW_STOP, UNIQUE_ID_FLUSH
from .coordinator import GaggiMateCoordinator
from .sensor import GaggiMateEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GaggiMate buttons."""
    coordinator: GaggiMateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        GaggiMateStartBrewButton(coordinator, entry),
        GaggiMateStopBrewButton(coordinator, entry),
        GaggiMateFlushButton(coordinator, entry),
    ]

    async_add_entities(entities)


class GaggiMateStartBrewButton(GaggiMateEntity, ButtonEntity):
    """Start brew button."""

    _attr_icon = "mdi:coffee"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(coordinator, entry)
        self._attr_name = "Start Brew"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_BREW_START}"

    async def async_press(self) -> None:
        """Handle the button press."""
        try:
            await self.coordinator.start_brew()
            _LOGGER.debug("Started brew on GaggiMate")
        except Exception as err:
            _LOGGER.error("Failed to start brew: %s", err)
            raise


class GaggiMateStopBrewButton(GaggiMateEntity, ButtonEntity):
    """Stop brew button."""

    _attr_icon = "mdi:stop"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(coordinator, entry)
        self._attr_name = "Stop Brew"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_BREW_STOP}"

    async def async_press(self) -> None:
        """Handle the button press."""
        try:
            await self.coordinator.stop_brew()
            _LOGGER.debug("Stopped brew on GaggiMate")
        except Exception as err:
            _LOGGER.error("Failed to stop brew: %s", err)
            raise


class GaggiMateFlushButton(GaggiMateEntity, ButtonEntity):
    """Flush button."""

    _attr_icon = "mdi:water-pump"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(coordinator, entry)
        self._attr_name = "Flush"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_FLUSH}"

    async def async_press(self) -> None:
        """Handle the button press."""
        try:
            await self.coordinator.start_flush()
            _LOGGER.debug("Started flush on GaggiMate")
        except Exception as err:
            _LOGGER.error("Failed to start flush: %s", err)
            raise
