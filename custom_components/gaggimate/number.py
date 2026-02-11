"""Number platform for GaggiMate integration."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, UNIQUE_ID_TARGET_TEMP_SETPOINT
from .coordinator import GaggiMateCoordinator
from .sensor import GaggiMateEntity

_LOGGER = logging.getLogger(__name__)

MIN_TEMP_C = 0
MAX_TEMP_C = 160


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GaggiMate number entities."""
    coordinator: GaggiMateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        GaggiMateTargetTemperatureNumber(coordinator, entry),
    ]

    async_add_entities(entities)


class GaggiMateTargetTemperatureNumber(GaggiMateEntity, NumberEntity):
    """Target temperature setpoint."""

    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = MIN_TEMP_C
    _attr_native_max_value = MAX_TEMP_C
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:thermometer"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator, entry)
        self._attr_name = "Target Temperature Setpoint"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_TARGET_TEMP_SETPOINT}"

    @property
    def native_value(self) -> float | None:
        """Return the current target temperature."""
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get("tt")
        if value is None:
            return None
        return float(value)

    async def async_set_native_value(self, value: float) -> None:
        """Set new target temperature."""
        try:
            await self.coordinator.set_temperature(value)
            _LOGGER.debug("Set target temperature to %s", value)
        except Exception as err:
            _LOGGER.error("Failed to set target temperature: %s", err)
            raise
