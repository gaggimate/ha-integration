"""Sensor platform for GaggiMate integration."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfTemperature,
    UnitOfMass
    )
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    MODE_ICONS,
    MODE_NAMES,
    MachineMode,
    UNIQUE_ID_CURRENT_TEMP,
    UNIQUE_ID_MODE,
    UNIQUE_ID_SELECTED_PROFILE,
    UNIQUE_ID_HW_MODEL,
    UNIQUE_ID_SW_DISPLAY,
    UNIQUE_ID_SW_CONTROLLER,
    UNIQUE_ID_TARGET_TEMP,
    UNIQUE_ID_UPDATE_DISPLAY,
    UNIQUE_ID_UPDATE_CONTROLLER,
    UNIQUE_ID_LATEST_VERSION,
    UNIQUE_ID_SCALE_CONNECTED,
    UNIQUE_ID_CURRENT_WEIGHT
)
from .coordinator import GaggiMateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GaggiMate sensors."""
    coordinator: GaggiMateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        GaggiMateCurrentTemperatureSensor(coordinator, entry),
        GaggiMateTargetTemperatureSensor(coordinator, entry),
        GaggiMateModeSensor(coordinator, entry),
        GaggiMateSelectedProfileSensor(coordinator, entry),
        GaggiMateHardwareModelSensor(coordinator, entry),
        GaggiMateDisplayVersionSensor(coordinator, entry),
        GaggiMateDisplayUpdateSensor(coordinator, entry),
        GaggiMateControllerUpdateSensor(coordinator, entry),
        GaggiMateControllerVersionSensor(coordinator, entry),
        GaggiMateScaleConnected(coordinator, entry),
        GaggiMateCurrentWeight(coordinator, entry),
    ]

    async_add_entities(entities)


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


class GaggiMateCurrentTemperatureSensor(GaggiMateEntity, SensorEntity):
    """Current temperature sensor."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_icon = "mdi:thermometer"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_name = "Current Temperature"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_CURRENT_TEMP}"

    @property
    def native_value(self) -> float | None:
        """Return the current temperature."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("ct")

class GaggiMateCurrentWeight(GaggiMateEntity, SensorEntity):
    """Current weight from scale."""

    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfMass.GRAMS
    _attr_icon = "mdi:scale"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_name = "Current Weight"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_CURRENT_WEIGHT}"

    @property
    def native_value(self) -> float | None:
        """Return the current weight."""
        if self.coordinator.data is None:
            return None

        if not self.coordinator.data.get("bc"):
            return None  # shows as Unavailable

        return self.coordinator.data.get("cw")




class GaggiMateTargetTemperatureSensor(GaggiMateEntity, SensorEntity):
    """Target temperature sensor."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_icon = "mdi:thermometer-auto"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_name = "Target Temperature"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_TARGET_TEMP}"

    @property
    def native_value(self) -> float | None:
        """Return the target temperature."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("tt")


class GaggiMateModeSensor(GaggiMateEntity, SensorEntity):
    """Machine mode sensor."""

    _attr_device_class = None
    _attr_state_class = None

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_name = "Mode"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_MODE}"

    @property
    def native_value(self) -> str | None:
        """Return the machine mode."""
        if self.coordinator.data is None:
            return None

        mode_value = self.coordinator.data.get("m")
        if mode_value is None:
            return None

        try:
            mode = MachineMode(mode_value)
            return MODE_NAMES.get(mode, "Unknown")
        except ValueError:
            return "Unknown"

    @property
    def icon(self) -> str:
        """Return the icon based on current mode."""
        if self.coordinator.data is None:
            return "mdi:coffee-maker"

        mode_value = self.coordinator.data.get("m")
        if mode_value is None:
            return "mdi:coffee-maker"

        try:
            mode = MachineMode(mode_value)
            return MODE_ICONS.get(mode, "mdi:coffee-maker")
        except ValueError:
            return "mdi:coffee-maker"

    @property
    def extra_state_attributes(self) -> dict[str, any]:
        """Return additional attributes."""
        if self.coordinator.data is None:
            return {}

        return {
            "mode_id": self.coordinator.data.get("m"),
        }


class GaggiMateSelectedProfileSensor(GaggiMateEntity, SensorEntity):
    """Selected profile sensor."""

    _attr_icon = "mdi:coffee"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_name = "Selected Profile"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_SELECTED_PROFILE}"

    @property
    def native_value(self) -> str | None:
        """Return the selected profile label."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("p")

    
class GaggiMateScaleConnected(GaggiMateEntity, SensorEntity):
    """Scale connected Status"""

    _attr_icon = "mdi:scale"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry)
        self._attr_name = "Scale Connection"
        self._attr_unique_id = f"{coordinator.host}_{GaggiMateScaleConnected}"

    @property
    def native_value(self) -> str | None:
        """Return the scale connection status."""
        if self.coordinator.data is None:
            return None

        bc = self.coordinator.data.get("bc")
        if bc is None:
            return None

        return "Connected" if bc else "Disconnected"

class _GaggiMateDiagnosticSensor(GaggiMateEntity, SensorEntity):
    """Base class for diagnostic sensors."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC


class GaggiMateHardwareModelSensor(_GaggiMateDiagnosticSensor):
    """Hardware model sensor."""

    _attr_icon = "mdi:chip"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Hardware Model"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_HW_MODEL}"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.ota_settings.get("hardware")


class GaggiMateDisplayVersionSensor(_GaggiMateDiagnosticSensor):
    """Display firmware version sensor."""

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Display Firmware Version"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_SW_DISPLAY}"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.ota_settings.get("displayVersion")
        
class GaggiMateDisplayUpdateSensor(_GaggiMateDiagnosticSensor):
    """Display Update Available."""

    _attr_icon = "mdi:update"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Display Update Available"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_UPDATE_DISPLAY}"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.ota_settings.get("displayUpdateAvailable")


class GaggiMateControllerVersionSensor(_GaggiMateDiagnosticSensor):
    """Controller firmware version sensor."""

    _attr_icon = "mdi:application-braces"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Controller Firmware Version"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_SW_CONTROLLER}"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.ota_settings.get("controllerVersion")

class GaggiMateControllerUpdateSensor(_GaggiMateDiagnosticSensor):
    """Controller Update Available."""

    _attr_icon = "mdi:update"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Controller Update Available"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_UPDATE_CONTROLLER}"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.ota_settings.get("controllerUpdateAvailable")

class GaggiMateLatestVersionSensor(_GaggiMateDiagnosticSensor):
    """Latest Software Version."""

    _attr_icon = "mdi:application-braces"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Latest Software Version"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_LATEST_VERSION}"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.ota_settings.get("controllerUpdateAvailable")