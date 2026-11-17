"""Update platform for GaggiMate: firmware versions and update progress."""
from __future__ import annotations

from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    OTA_STATE_UPDATING_CONTROLLER,
    OTA_STATE_UPDATING_DISPLAY,
    UNIQUE_ID_FW_UPDATE_CONTROLLER,
    UNIQUE_ID_FW_UPDATE_DISPLAY,
)
from .coordinator import GaggiMateCoordinator
from .sensor import GaggiMateEntity, _get_status

PARALLEL_UPDATES = 1

RELEASES_URL = "https://github.com/jniebuhr/gaggimate/releases"

# component -> (unique id, entity name, ota-settings keys, state while flashing it)
COMPONENTS: dict[str, tuple[str, str, str, str, str]] = {
    "display": (
        UNIQUE_ID_FW_UPDATE_DISPLAY,
        "Display Firmware",
        "displayVersion",
        "displayUpdateAvailable",
        OTA_STATE_UPDATING_DISPLAY,
    ),
    "controller": (
        UNIQUE_ID_FW_UPDATE_CONTROLLER,
        "Controller Firmware",
        "controllerVersion",
        "controllerUpdateAvailable",
        OTA_STATE_UPDATING_CONTROLLER,
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the GaggiMate firmware update entities."""
    coordinator: GaggiMateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        GaggiMateFirmwareUpdate(coordinator, entry, component)
        for component in COMPONENTS
    )


class GaggiMateFirmwareUpdate(GaggiMateEntity, UpdateEntity):
    """Firmware version and update progress of one GaggiMate component.

    Reports the installed and the latest version and, while the machine
    flashes the component, the progress. Install asks the firmware to flash
    the component (the same as "Update Display"/"Update Controller" in the
    web UI); the machine restarts afterwards.
    """

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.PROGRESS | UpdateEntityFeature.INSTALL
    )

    def __init__(
        self,
        coordinator: GaggiMateCoordinator,
        entry: ConfigEntry,
        component: str,
    ) -> None:
        super().__init__(coordinator, entry)
        key, name, version_key, flag_key, flashing_state = COMPONENTS[component]
        self._component = component
        self._version_key = version_key
        self._flag_key = flag_key
        self._flashing_state = flashing_state
        self._attr_unique_id = f"{coordinator.host}_{key}"
        self._attr_name = name
        self._attr_title = f"GaggiMate {component} firmware"

    @property
    def _ota(self) -> dict[str, Any]:
        return self.coordinator.ota_settings

    @property
    def available(self) -> bool:
        """Stay available while the machine flashes or restarts after an update."""
        if not self._ota.get(self._version_key):
            return False
        if self.coordinator.ota_busy and self.coordinator.ota_component == self._component:
            return True
        return super().available

    @property
    def installed_version(self) -> str | None:
        return self._ota.get(self._version_key) or None

    @property
    def latest_version(self) -> str | None:
        """The firmware decides whether an update is needed (it compares versions
        itself; its nightly versions look like v1.9.0-135-gabcdef and do not
        compare naively). So report the latest release only when it says so."""
        installed = self.installed_version
        if installed is None:
            return None
        latest = self._ota.get("latestVersion")
        if not latest:
            return None   # the firmware has not checked for updates yet: unknown, not "up to date"
        if self._ota.get(self._flag_key):
            return latest
        return installed

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """Home Assistant's own version comparison cannot rank git-describe
        versions (v1.9.0-135-gabc vs 1.9.0-138-gdef) and says "not newer".
        The firmware already decided, and latest_version only differs from
        installed_version when it did, so any difference means an update."""
        return latest_version != installed_version

    @property
    def release_url(self) -> str | None:
        if self._ota.get("channel") == "nightly":
            return f"{RELEASES_URL}/tag/nightly"
        latest = self._ota.get("latestVersion")
        return f"{RELEASES_URL}/tag/v{latest}" if latest else RELEASES_URL

    @property
    def in_progress(self) -> bool:
        return (
            self.coordinator.ota_busy
            and self.coordinator.ota_component == self._component
        )

    @property
    def update_percentage(self) -> int | None:
        if self.coordinator.ota_state == self._flashing_state:
            return self.coordinator.ota_progress
        return None

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Start flashing this component. Refuse while the machine is busy."""
        if self.coordinator.ota_busy:
            raise HomeAssistantError("A firmware update is already in progress")
        if not self.coordinator.available:
            raise HomeAssistantError("The GaggiMate is not connected")
        status = _get_status(self.coordinator.data or {})
        if status != "Idle":
            raise HomeAssistantError(
                f"Not updating while the machine is active ({status.lower()})"
            )
        if not self._ota.get("latestVersion") or not self._ota.get(self._flag_key):
            raise HomeAssistantError(
                "The firmware has not confirmed an update for this component yet "
                "(it checks for updates about 30 minutes after a restart)"
            )
        await self.coordinator.start_ota(self._component)
