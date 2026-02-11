"""Sensor platform for GaggiMate integration."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfMass, UnitOfPressure, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    MODE_ICONS,
    MODE_NAMES,
    MachineMode,
    UNIQUE_ID_CURRENT_PRESSURE,
    UNIQUE_ID_CURRENT_TEMP,
    UNIQUE_ID_CURRENT_WEIGHT,
    UNIQUE_ID_HW_MODEL,
    UNIQUE_ID_LATEST_VERSION,
    UNIQUE_ID_MODE,
    UNIQUE_ID_PUMP_FLOW,
    UNIQUE_ID_SCALE_CONNECTED,
    UNIQUE_ID_SELECTED_PROFILE,
    UNIQUE_ID_SHOT_VOLUME_PROGRESS,
    UNIQUE_ID_SW_CONTROLLER,
    UNIQUE_ID_SW_DISPLAY,
    UNIQUE_ID_TARGET_PRESSURE,
    UNIQUE_ID_TARGET_TEMP,
    UNIQUE_ID_TARGET_VOLUME,
    UNIQUE_ID_UPDATE_CONTROLLER,
    UNIQUE_ID_UPDATE_DISPLAY,
)
from .coordinator import GaggiMateCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class GaggiMateSensorEntityDescription(SensorEntityDescription):
    """Describe a GaggiMate sensor."""

    value_fn: Callable[[dict[str, Any], GaggiMateCoordinator], Any]
    available_fn: Callable[[dict[str, Any], GaggiMateCoordinator], bool] | None = None
    icon_fn: Callable[[dict[str, Any], GaggiMateCoordinator], str] | None = None
    extra_attrs_fn: Callable[[dict[str, Any], GaggiMateCoordinator], dict[str, Any]] | None = None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GaggiMate sensors."""
    coordinator: GaggiMateCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        GaggiMateSensor(coordinator, entry, description) for description in SENSORS
    )


class GaggiMateEntity(CoordinatorEntity[GaggiMateCoordinator]):
    """Base class for GaggiMate entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._entry = entry
        self._device_name = entry.title or f"GaggiMate {self.coordinator.host}"

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self.coordinator.host)},
            "name": self._device_name,
            "manufacturer": "GaggiMate",
            "model": "GaggiMate",
            "configuration_url": f"http://{self.coordinator.host}:{self.coordinator.port}",
        }

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success and self.coordinator.data is not None


class GaggiMateSensor(GaggiMateEntity, SensorEntity):
    """Generic GaggiMate sensor driven by descriptions."""

    entity_description: GaggiMateSensorEntityDescription

    def __init__(
        self,
        coordinator: GaggiMateCoordinator,
        entry: ConfigEntry,
        description: GaggiMateSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.host}_{description.key}"
        self._attr_name = description.name

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if not super().available:
            return False
        if self.entity_description.available_fn:
            data = self.coordinator.data or {}
            return self.entity_description.available_fn(data, self.coordinator)
        return True

    @property
    def native_value(self):
        """Return the current value."""
        data = self.coordinator.data or {}
        return self.entity_description.value_fn(data, self.coordinator)

    @property
    def icon(self) -> str | None:
        """Return dynamic icon when provided."""
        data = self.coordinator.data or {}
        if self.entity_description.icon_fn:
            return self.entity_description.icon_fn(data, self.coordinator)
        return self.entity_description.icon

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return any extra attributes."""
        data = self.coordinator.data or {}
        if self.entity_description.extra_attrs_fn:
            return self.entity_description.extra_attrs_fn(data, self.coordinator)
        return {}


def _get_mode_name(data: dict[str, Any]) -> str | None:
    """Map raw mode to friendly name."""
    mode_value = data.get("m")
    if mode_value is None:
        return None
    try:
        return MODE_NAMES.get(MachineMode(mode_value), "Unknown")
    except ValueError:
        return "Unknown"


def _get_mode_icon(data: dict[str, Any]) -> str:
    """Map raw mode to icon."""
    mode_value = data.get("m")
    if mode_value is None:
        return "mdi:coffee-maker"
    try:
        return MODE_ICONS.get(MachineMode(mode_value), "mdi:coffee-maker")
    except ValueError:
        return "mdi:coffee-maker"


SENSORS: tuple[GaggiMateSensorEntityDescription, ...] = (
    GaggiMateSensorEntityDescription(
        key=UNIQUE_ID_CURRENT_TEMP,
        name="Current Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        icon="mdi:thermometer",
        value_fn=lambda data, _: data.get("ct"),
    ),
    GaggiMateSensorEntityDescription(
        key=UNIQUE_ID_TARGET_TEMP,
        name="Target Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        icon="mdi:thermometer-auto",
        value_fn=lambda data, _: data.get("tt"),
    ),
    GaggiMateSensorEntityDescription(
        key=UNIQUE_ID_MODE,
        name="Mode",
        value_fn=lambda data, _: _get_mode_name(data),
        icon_fn=lambda data, _: _get_mode_icon(data),
        extra_attrs_fn=lambda data, _: {"mode_id": data.get("m")} if data.get("m") is not None else {},
    ),
    GaggiMateSensorEntityDescription(
        key=UNIQUE_ID_SELECTED_PROFILE,
        name="Selected Profile",
        icon="mdi:coffee",
        value_fn=lambda data, _: data.get("p"),
    ),
    GaggiMateSensorEntityDescription(
        key=UNIQUE_ID_SCALE_CONNECTED,
        name="Scale Connection",
        icon="mdi:scale",
        value_fn=lambda data, _: None
        if data.get("bc") is None
        else ("Connected" if data.get("bc") else "Disconnected"),
    ),
    GaggiMateSensorEntityDescription(
        key=UNIQUE_ID_CURRENT_WEIGHT,
        name="Current Weight",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=1,
        icon="mdi:scale",
        value_fn=lambda data, _: None if not data.get("bc") else data.get("cw"),
    ),
    GaggiMateSensorEntityDescription(
        key=UNIQUE_ID_CURRENT_PRESSURE,
        name="Current Pressure",
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPressure.BAR,
        icon="mdi:gauge",
        value_fn=lambda data, _: None if data.get("pr") is None else float(data.get("pr")),
    ),
    GaggiMateSensorEntityDescription(
        key=UNIQUE_ID_TARGET_PRESSURE,
        name="Target Pressure",
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPressure.BAR,
        icon="mdi:gauge-full",
        value_fn=lambda data, _: None if data.get("pt") is None else float(data.get("pt")),
    ),
    GaggiMateSensorEntityDescription(
        key=UNIQUE_ID_PUMP_FLOW,
        name="Pump Flow",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="mL/s",
        icon="mdi:water-pump",
        value_fn=lambda data, _: None if data.get("fl") is None else float(data.get("fl")),
    ),
    GaggiMateSensorEntityDescription(
        key=UNIQUE_ID_TARGET_VOLUME,
        name="Target Shot Volume",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=1,
        icon="mdi:cup-water",
        value_fn=lambda data, _: None if data.get("tw") is None else float(data.get("tw")),
    ),
    GaggiMateSensorEntityDescription(
        key=UNIQUE_ID_SHOT_VOLUME_PROGRESS,
        name="Shot Volume Progress",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=1,
        icon="mdi:chart-line",
        value_fn=lambda data, _: (
            None
            if (process := data.get("process") or {}).get("tt") != "volumetric"
            else (None if process.get("pp") is None else float(process.get("pp")))
        ),
    ),
    GaggiMateSensorEntityDescription(
        key=UNIQUE_ID_HW_MODEL,
        name="Hardware Model",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _, coordinator: coordinator.ota_settings.get("hardware"),
    ),
    GaggiMateSensorEntityDescription(
        key=UNIQUE_ID_SW_DISPLAY,
        name="Display Firmware Version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _, coordinator: coordinator.ota_settings.get("displayVersion"),
    ),
    GaggiMateSensorEntityDescription(
        key=UNIQUE_ID_UPDATE_DISPLAY,
        name="Display Update Available",
        icon="mdi:update",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _, coordinator: coordinator.ota_settings.get("displayUpdateAvailable"),
    ),
    GaggiMateSensorEntityDescription(
        key=UNIQUE_ID_SW_CONTROLLER,
        name="Controller Firmware Version",
        icon="mdi:application-braces",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _, coordinator: coordinator.ota_settings.get("controllerVersion"),
    ),
    GaggiMateSensorEntityDescription(
        key=UNIQUE_ID_UPDATE_CONTROLLER,
        name="Controller Update Available",
        icon="mdi:update",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _, coordinator: coordinator.ota_settings.get("controllerUpdateAvailable"),
    ),
    GaggiMateSensorEntityDescription(
        key=UNIQUE_ID_LATEST_VERSION,
        name="Latest Software Version",
        icon="mdi:application-braces",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _, coordinator: coordinator.ota_settings.get("latestVersion"),
    ),
)
