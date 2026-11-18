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

from .const import DOMAIN, UNIQUE_ID_CONTROLLER_ERROR, UNIQUE_ID_UPDATE_AVAILABLE
from .coordinator import GaggiMateCoordinator
from .entity import GaggiMateEntity, async_setup_gated, has_key

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


BINARY_SENSORS: tuple[GaggiMateBinarySensorEntityDescription, ...] = (
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
)
