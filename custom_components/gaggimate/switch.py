"""Switch platform for GaggiMate integration."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MachineMode, UNIQUE_ID_POWER, UNIQUE_ID_STEAM_HEATER
from .coordinator import GaggiMateCoordinator
from .entity import async_setup_gated, cap
from .sensor import GaggiMateEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class GaggiMateSwitchEntityDescription(SwitchEntityDescription):
    """Describe a GaggiMate switch."""

    supported_fn: Callable[[dict[str, Any]], bool] | None = None


SWITCHES: tuple[GaggiMateSwitchEntityDescription, ...] = (
    GaggiMateSwitchEntityDescription(
        key=UNIQUE_ID_STEAM_HEATER,
        name="Steam Heater",
        icon="mdi:kettle-steam",
        supported_fn=cap("db"),
    ),
)


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
    async_setup_gated(coordinator, entry, async_add_entities, SWITCHES, GaggiMateSteamSwitch)


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


class GaggiMateSteamSwitch(GaggiMateEntity, SwitchEntity):
    """Steam heater switch (dual boiler only).

    The firmware only has a toggle; the coordinator sends it only when the state
    actually has to change (see GaggiMateCoordinator.set_steam_heater).
    """

    entity_description: GaggiMateSwitchEntityDescription

    def __init__(
        self,
        coordinator: GaggiMateCoordinator,
        entry: ConfigEntry,
        description: GaggiMateSwitchEntityDescription,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.host}_{description.key}"
        self._attr_name = description.name

    @property
    def is_on(self) -> bool | None:
        """Return true if the steam heater is active."""
        sa = (self.coordinator.data or {}).get("sa")
        return sa if isinstance(sa, bool) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the steam heater on."""
        try:
            await self.coordinator.set_steam_heater(True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to turn on steam heater: %s", err)
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the steam heater off."""
        try:
            await self.coordinator.set_steam_heater(False)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to turn off steam heater: %s", err)
            raise
