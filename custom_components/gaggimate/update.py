"""Update platform for GaggiMate integration."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    OTA_PHASE_CONTROLLER,
    OTA_PHASE_DISPLAY,
    OTA_PHASE_FINISHED,
    UNIQUE_ID_FIRMWARE_UPDATE_BOTH,
    UNIQUE_ID_FIRMWARE_UPDATE_CONTROLLER,
    UNIQUE_ID_FIRMWARE_UPDATE_DISPLAY,
)
from .coordinator import GaggiMateCoordinator
from .entity import GaggiMateEntity, async_setup_gated, has_key

_LOGGER = logging.getLogger(__name__)

_DISPLAY_FLAG = "displayUpdateAvailable"
_CONTROLLER_FLAG = "controllerUpdateAvailable"


@dataclass(frozen=True, kw_only=True)
class GaggiMateUpdateEntityDescription(UpdateEntityDescription):
    """Describe a GaggiMate firmware update entity."""

    component: str  # value sent as "cp" in req:ota-start
    phases: tuple[int, ...]  # progress phases that belong to this entity
    supported_fn: Callable[[dict[str, Any]], bool] | None = None


UPDATES: tuple[GaggiMateUpdateEntityDescription, ...] = (
    GaggiMateUpdateEntityDescription(
        key=UNIQUE_ID_FIRMWARE_UPDATE_DISPLAY,
        name="Display Firmware Update",
        component="display",
        phases=(OTA_PHASE_DISPLAY,),
        supported_fn=has_key("up"),
    ),
    GaggiMateUpdateEntityDescription(
        key=UNIQUE_ID_FIRMWARE_UPDATE_CONTROLLER,
        name="Controller Firmware Update",
        component="controller",
        phases=(OTA_PHASE_CONTROLLER,),
        supported_fn=has_key("up"),
    ),
    GaggiMateUpdateEntityDescription(
        key=UNIQUE_ID_FIRMWARE_UPDATE_BOTH,
        name="Display and Controller Firmware Update",
        component="both",
        phases=(OTA_PHASE_DISPLAY, OTA_PHASE_CONTROLLER),
        supported_fn=has_key("up"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GaggiMate update entities."""
    coordinator: GaggiMateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_gated(coordinator, entry, async_add_entities, UPDATES, GaggiMateUpdate)


def _version(value: Any) -> str | None:
    """Return a non-empty version string, or None."""
    if isinstance(value, str) and value:
        return value
    return None


class GaggiMateUpdate(GaggiMateEntity, UpdateEntity):
    """Firmware update of the display, the controller or both."""

    entity_description: GaggiMateUpdateEntityDescription

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = None  # UpdateEntityDescription defaults to CONFIG
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS

    def __init__(
        self,
        coordinator: GaggiMateCoordinator,
        entry: ConfigEntry,
        description: GaggiMateUpdateEntityDescription,
    ) -> None:
        """Initialize the update entity."""
        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.host}_{description.key}"
        self._attr_name = description.name

    @property
    def _display_version(self) -> str | None:
        return _version(self.coordinator.ota_settings.get("displayVersion"))

    @property
    def _controller_version(self) -> str | None:
        return _version(self.coordinator.ota_settings.get("controllerVersion"))

    @property
    def installed_version(self) -> str | None:
        """Return the installed version."""
        component = self.entity_description.component
        if component == "display":
            return self._display_version
        if component == "controller":
            return self._controller_version
        display, controller = self._display_version, self._controller_version
        if display is None or controller is None:
            return None
        if display == controller:
            return display
        return f"Display {display}, Controller {controller}"

    @property
    def latest_version(self) -> str | None:
        """Return the latest version (the installed one when nothing is available)."""
        settings = self.coordinator.ota_settings
        component = self.entity_description.component
        if component == "display":
            flags = [_DISPLAY_FLAG]
        elif component == "controller":
            flags = [_CONTROLLER_FLAG]
        else:
            flags = [_DISPLAY_FLAG, _CONTROLLER_FLAG]
        if all(flag not in settings for flag in flags):
            return None
        if any(settings.get(flag) is True for flag in flags):
            return _version(settings.get("latestVersion"))
        return self.installed_version

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """Trust the firmware's update-available flag instead of comparing versions."""
        return latest_version != installed_version

    @property
    def in_progress(self) -> bool:
        """Return whether a firmware update is running."""
        return self.coordinator.ota_updating

    @property
    def update_percentage(self) -> int | None:
        """Return the progress of the phase that belongs to this entity."""
        phase = self.coordinator.ota_phase
        if phase in self.entity_description.phases:
            return self.coordinator.ota_percent
        if phase == OTA_PHASE_FINISHED and self.coordinator._ota_ran:
            return 100
        return None

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Start the update (version and backup are not supported by the firmware)."""
        await self.coordinator.start_ota(self.entity_description.component)
