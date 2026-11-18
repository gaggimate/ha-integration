"""Select platform for GaggiMate integration."""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MODE_NAMES, MachineMode, UNIQUE_ID_MODE_SELECT, UNIQUE_ID_PROFILE_SELECT
from .coordinator import GaggiMateCoordinator
from .sensor import GaggiMateEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GaggiMate select entities."""
    coordinator: GaggiMateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        GaggiMateModeSelect(coordinator, entry),
        GaggiMateProfileSelect(coordinator, entry),
    ]

    async_add_entities(entities)


class GaggiMateModeSelect(GaggiMateEntity, SelectEntity):
    """Mode selection entity."""

    _attr_icon = "mdi:coffee-maker"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the select entity."""
        super().__init__(coordinator, entry)
        self._attr_name = "Mode"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_MODE_SELECT}"

    @property
    def options(self) -> list[str]:
        """Return selectable modes; dual boiler firmware maps Steam/Hot Water to Brew."""
        data = self.coordinator.data or {}
        dual_boiler = data.get("db") is True
        return [
            name
            for mode, name in MODE_NAMES.items()
            if not (dual_boiler and mode in (MachineMode.STEAM, MachineMode.WATER))
        ]

    @property
    def current_option(self) -> str | None:
        """Return the current mode."""
        if self.coordinator.data is None:
            return None

        mode_value = self.coordinator.data.get("m")
        if mode_value is None:
            return None

        try:
            mode = MachineMode(mode_value)
            name = MODE_NAMES.get(mode)
        except ValueError:
            return None
        return name if name in self.options else None

    async def async_select_option(self, option: str) -> None:
        """Change the mode."""
        # Find the mode ID from the name
        mode_id = None
        for mode, name in MODE_NAMES.items():
            if name == option:
                mode_id = mode
                break

        if mode_id is None or option not in self.options:
            raise ServiceValidationError(f"Invalid mode option: {option}")

        try:
            await self.coordinator.set_mode(mode_id)
            _LOGGER.debug("Set GaggiMate mode to %s (%s)", option, mode_id)
        except Exception as err:
            _LOGGER.error("Failed to set mode: %s", err)
            raise


class GaggiMateProfileSelect(GaggiMateEntity, SelectEntity):
    """Profile selection entity."""

    _attr_icon = "mdi:coffee"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the select entity."""
        super().__init__(coordinator, entry)
        self._attr_name = "Profile"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_PROFILE_SELECT}"

    @property
    def current_option(self) -> str | None:
        """Return the currently selected profile."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("p")

    @property
    def options(self) -> list[str]:
        """Return available profile options."""
        return list(self.coordinator.profiles.keys())

    async def async_select_option(self, option: str) -> None:
        """Change the selected profile."""
        profile_id = self.coordinator.profiles.get(option)
        if profile_id is None:
            raise ServiceValidationError(f"Profile not found: {option}")

        try:
            await self.coordinator.select_profile(profile_id)
            _LOGGER.debug("Selected GaggiMate profile %s (%s)", option, profile_id)
        except Exception as err:
            _LOGGER.error("Failed to select profile: %s", err)
            raise
