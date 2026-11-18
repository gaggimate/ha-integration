"""Number platform for GaggiMate integration."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfMass, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    GRIND_DURATION_MAX_S,
    GRIND_DURATION_MIN_S,
    GRIND_WEIGHT_MAX_G,
    GRIND_WEIGHT_MIN_G,
    UNIQUE_ID_GRIND_TARGET_DURATION,
    UNIQUE_ID_GRIND_TARGET_WEIGHT,
    UNIQUE_ID_TARGET_TEMP_SETPOINT,
)
from .coordinator import GaggiMateCoordinator, int_flag
from .entity import async_setup_gated, flag_is_set, has_key
from .sensor import GaggiMateEntity

_LOGGER = logging.getLogger(__name__)

MIN_TEMP_C = 0
MAX_TEMP_C = 160


@dataclass(frozen=True, kw_only=True)
class GaggiMateNumberEntityDescription(NumberEntityDescription):
    """Describe a GaggiMate number."""

    value_fn: Callable[[dict[str, Any], GaggiMateCoordinator], float | None]
    set_value_fn: Callable[[GaggiMateCoordinator, float], Awaitable[None]]
    supported_fn: Callable[[dict[str, Any]], bool] | None = None
    # Extra availability condition on top of the connection (None = always available)
    available_fn: Callable[[dict[str, Any]], bool] | None = None


def _grind_value(data: dict[str, Any], key: str, divisor: float) -> float | None:
    """Read a grind target from the status data (None when absent or not a number)."""
    raw = data.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw) / divisor


SENSORS: tuple[GaggiMateNumberEntityDescription, ...] = (
    GaggiMateNumberEntityDescription(
        key=UNIQUE_ID_TARGET_TEMP_SETPOINT,
        name="Target Temperature Setpoint",
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=MIN_TEMP_C,
        native_max_value=MAX_TEMP_C,
        native_step=1,
        mode=NumberMode.BOX,
        icon="mdi:thermometer",
        value_fn=lambda data, _: None if data.get("tt") is None else float(data.get("tt")),
        set_value_fn=lambda coordinator, value: coordinator.set_temperature(value),
    ),
    GaggiMateNumberEntityDescription(
        key=UNIQUE_ID_GRIND_TARGET_DURATION,
        name="Grind Target Duration",
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        native_min_value=GRIND_DURATION_MIN_S,
        native_max_value=GRIND_DURATION_MAX_S,
        native_step=1,
        mode=NumberMode.BOX,
        icon="mdi:timer-cog-outline",
        # gtd is in milliseconds
        value_fn=lambda data, _: _grind_value(data, "gtd", 1000.0),
        set_value_fn=lambda coordinator, value: coordinator.set_grind_target("duration", value),
        supported_fn=has_key("gtd"),
        # Only the active target type can be changed (gt == 0: by time)
        available_fn=lambda data: int_flag(data, "gt") is False,
    ),
    GaggiMateNumberEntityDescription(
        key=UNIQUE_ID_GRIND_TARGET_WEIGHT,
        name="Grind Target Weight",
        device_class=NumberDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        native_min_value=GRIND_WEIGHT_MIN_G,
        native_max_value=GRIND_WEIGHT_MAX_G,
        native_step=0.5,
        mode=NumberMode.BOX,
        icon="mdi:scale",
        value_fn=lambda data, _: _grind_value(data, "gtv", 1.0),
        set_value_fn=lambda coordinator, value: coordinator.set_grind_target("weight", value),
        # bta is a 1/0 integer; gt == 1 only while a volumetric source is available
        supported_fn=lambda data: flag_is_set("bta")(data) and "gtv" in data,
        available_fn=lambda data: int_flag(data, "gt") is True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GaggiMate number entities."""
    coordinator: GaggiMateCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_setup_gated(coordinator, entry, async_add_entities, SENSORS, GaggiMateNumber)


class GaggiMateNumber(GaggiMateEntity, NumberEntity):
    """Generic GaggiMate number driven by descriptions."""

    entity_description: GaggiMateNumberEntityDescription

    def __init__(
        self,
        coordinator: GaggiMateCoordinator,
        entry: ConfigEntry,
        description: GaggiMateNumberEntityDescription,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.host}_{description.key}"
        self._attr_name = description.name

    @property
    def native_value(self) -> float | None:
        """Return the current value."""
        data = self.coordinator.data or {}
        return self.entity_description.value_fn(data, self.coordinator)

    @property
    def available(self) -> bool:
        """Return if entity is available (and, where described, its extra condition holds)."""
        if not super().available:
            return False
        available_fn = self.entity_description.available_fn
        return available_fn is None or bool(available_fn(self.coordinator.data or {}))

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        try:
            await self.entity_description.set_value_fn(self.coordinator, value)
            _LOGGER.debug("Set %s to %s", self.entity_description.key, value)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to set %s: %s", self.entity_description.key, err)
            raise
