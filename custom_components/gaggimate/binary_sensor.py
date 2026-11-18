"""Binary sensor platform for GaggiMate integration."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    UNIQUE_ID_BREW_CONFIRM_PENDING,
    UNIQUE_ID_CONTROLLER_ERROR,
    UNIQUE_ID_STEAM_BOILER_LOW,
    UNIQUE_ID_TANK_LEVEL_LOW,
    UNIQUE_ID_UPDATE_AVAILABLE,
)
from .coordinator import GaggiMateCoordinator
from .entity import GateFn, GaggiMateEntity, async_setup_gated, cap, has_key
from .sensor import WARNING_SEVERITY

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class GaggiMateBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describe a GaggiMate binary sensor."""

    value_fn: Callable[[dict[str, Any], GaggiMateCoordinator], bool | None]
    extra_attrs_fn: Callable[[dict[str, Any], GaggiMateCoordinator], dict[str, Any]] | None = None
    supported_fn: Callable[[dict[str, Any]], bool] | None = None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GaggiMate binary sensors."""
    coordinator: GaggiMateCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_setup_gated(
        coordinator, entry, async_add_entities, BINARY_SENSORS, GaggiMateBinarySensor
    )


class GaggiMateBinarySensor(GaggiMateEntity, BinarySensorEntity):
    """Generic GaggiMate binary sensor driven by descriptions."""

    entity_description: GaggiMateBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: GaggiMateCoordinator,
        entry: ConfigEntry,
        description: GaggiMateBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.host}_{description.key}"
        self._attr_name = description.name

    @property
    def is_on(self) -> bool | None:
        """Return the current state."""
        data = self.coordinator.data or {}
        return self.entity_description.value_fn(data, self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return any extra attributes."""
        data = self.coordinator.data or {}
        if self.entity_description.extra_attrs_fn:
            return self.entity_description.extra_attrs_fn(data, self.coordinator)
        return {}


def _controller_error(data: dict[str, Any], _: GaggiMateCoordinator) -> bool | None:
    """On when the controller reports an error code or an error/mismatch state."""
    sys_state = data.get("sys")
    if not isinstance(sys_state, dict):
        return None
    state = sys_state.get("s")
    code = sys_state.get("c")
    if state is None and code is None:
        return None
    return bool(code) or state in ("error", "mismatch")


def _update_available(data: dict[str, Any], _: GaggiMateCoordinator) -> bool | None:
    """Display or controller firmware update available."""
    up = data.get("up")
    return None if up is None else bool(up)


def _tank_level_low(data: dict[str, Any], _: GaggiMateCoordinator) -> bool | None:
    """On when the tank sensor reports the tank as not OK; unknown without a reading."""
    tank_ok = data.get("tlo")
    return None if tank_ok is None else not bool(tank_ok)


def _steam_boiler_low(data: dict[str, Any], _: GaggiMateCoordinator) -> bool | None:
    """On when the steam boiler level sensor reports low water; unknown without a reading."""
    low = data.get("sbl")
    return None if low is None else bool(low)


def _brew_confirm_pending(_: dict[str, Any], coordinator: GaggiMateCoordinator) -> bool:
    """On while the firmware waits for a brew start to be confirmed."""
    return coordinator.brew_confirm is not None


def _brew_confirm_attrs(_: dict[str, Any], coordinator: GaggiMateCoordinator) -> dict[str, Any]:
    """The error-level warnings that blocked the brew start."""
    warnings = []
    for warning in coordinator.brew_confirm or []:
        level = warning.get("l")
        warnings.append(
            {
                "key": warning.get("k"),
                "severity": WARNING_SEVERITY.get(level, "Unknown"),
                "severity_level": level,
            }
        )
    return {"warnings": warnings}


def _has_reading(capability: str, key: str) -> GateFn:
    """Gate: the capability is true and the reading is not null (null means no reading)."""
    capable = cap(capability)
    return lambda data: capable(data) and data.get(key) is not None


BINARY_SENSORS: tuple[GaggiMateBinarySensorEntityDescription, ...] = (
    GaggiMateBinarySensorEntityDescription(
        key=UNIQUE_ID_BREW_CONFIRM_PENDING,
        name="Brew Confirmation Pending",
        icon="mdi:coffee-maker-outline",
        value_fn=_brew_confirm_pending,
        extra_attrs_fn=_brew_confirm_attrs,
    ),
    GaggiMateBinarySensorEntityDescription(
        key=UNIQUE_ID_CONTROLLER_ERROR,
        name="Controller Error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_controller_error,
        supported_fn=has_key("sys"),
    ),
    GaggiMateBinarySensorEntityDescription(
        key=UNIQUE_ID_UPDATE_AVAILABLE,
        name="Update Available",
        device_class=BinarySensorDeviceClass.UPDATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_update_available,
        supported_fn=has_key("up"),
    ),
    GaggiMateBinarySensorEntityDescription(
        key=UNIQUE_ID_TANK_LEVEL_LOW,
        name="Tank Level Low",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_tank_level_low,
        supported_fn=_has_reading("htl", "tlo"),
    ),
    GaggiMateBinarySensorEntityDescription(
        key=UNIQUE_ID_STEAM_BOILER_LOW,
        name="Steam Boiler Low",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_steam_boiler_low,
        supported_fn=_has_reading("hsl", "sbl"),
    ),
)
