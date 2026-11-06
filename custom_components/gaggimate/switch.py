"""Switch platform for GaggiMate integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MachineMode, UNIQUE_ID_POWER, UNIQUE_ID_STEAM_BOILER
from .coordinator import GaggiMateCoordinator
from .sensor import GaggiMateEntity, async_setup_steam_entities

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GaggiMate switches."""
    coordinator: GaggiMateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        GaggiMatePowerSwitch(coordinator, entry),
    ]

    async_add_entities(entities)

    async_setup_steam_entities(
        hass,
        entry,
        coordinator,
        async_add_entities,
        "switch",
        [GaggiMateSteamBoilerSwitch(coordinator, entry)],
    )


class GaggiMatePowerSwitch(GaggiMateEntity, SwitchEntity):
    """Machine power switch."""

    _attr_icon = "mdi:power"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, entry)
        self._attr_name = "Machine Active"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_POWER}"

    @property
    def is_on(self) -> bool | None:
        """Return true if the machine is on (not in standby)."""
        if self.coordinator.data is None:
            return None

        mode_value = self.coordinator.data.get("m")
        if mode_value is None:
            return None

        # Machine is "on" if it's in any mode except standby
        return mode_value != MachineMode.STANDBY

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the machine on (set to brew mode)."""
        try:
            await self.coordinator.set_mode(MachineMode.BREW)
            _LOGGER.debug("Turned on GaggiMate (set to brew mode)")
        except Exception as err:
            _LOGGER.error("Failed to turn on GaggiMate: %s", err)
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the machine off (set to standby mode)."""
        try:
            await self.coordinator.set_mode(MachineMode.STANDBY)
            _LOGGER.debug("Turned off GaggiMate (set to standby mode)")
        except Exception as err:
            _LOGGER.error("Failed to turn off GaggiMate: %s", err)
            raise


class GaggiMateSteamBoilerSwitch(GaggiMateEntity, SwitchEntity):
    """Steam boiler heater switch (dual-boiler machines only)."""

    _attr_icon = "mdi:kettle-steam"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, entry)
        self._attr_name = "Steam Boiler"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_STEAM_BOILER}"

    @property
    def available(self) -> bool:
        """Only available when the board reports steam controls."""
        return super().available and self.coordinator.steam_controls_available

    @property
    def is_on(self) -> bool | None:
        """Return true if the steam boiler is heating."""
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get("sa")
        if value is None:
            return None
        return bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the steam boiler on."""
        await self.coordinator.set_steam_boiler(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the steam boiler off."""
        await self.coordinator.set_steam_boiler(False)
