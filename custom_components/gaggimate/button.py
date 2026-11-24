"""Button platform for GaggiMate integration."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    UNIQUE_ID_BACKFLUSH_COMPLETE,
    UNIQUE_ID_BACKFLUSH_START,
    UNIQUE_ID_BREW_CONFIRM,
    UNIQUE_ID_BREW_CONFIRM_CANCEL,
    UNIQUE_ID_BREW_START,
    UNIQUE_ID_BREW_STOP,
    UNIQUE_ID_CHECK_UPDATES,
    UNIQUE_ID_DESCALING_COMPLETE,
    UNIQUE_ID_DESCALING_START,
    UNIQUE_ID_FLUSH,
    UNIQUE_ID_FLUSH_STOP,
    UNIQUE_ID_PROCESS_CLEAR,
    UNIQUE_ID_TARE_SCALE,
)
from .coordinator import GaggiMateCoordinator
from .entity import async_setup_gated, cap, has_key, has_warning_key
from .sensor import GaggiMateEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class GaggiMateButtonEntityDescription(ButtonEntityDescription):
    """Describe a GaggiMate button."""

    press_fn: Callable[[GaggiMateCoordinator], Awaitable[None]]
    supported_fn: Callable[[dict[str, Any]], bool] | None = None
    available_fn: Callable[[GaggiMateCoordinator], bool] | None = None


BUTTONS: tuple[GaggiMateButtonEntityDescription, ...] = (
    GaggiMateButtonEntityDescription(
        key=UNIQUE_ID_TARE_SCALE,
        name="Tare Scale",
        icon="mdi:scale-balance",
        press_fn=lambda coordinator: coordinator.tare_scale(),
        # hs = hardware scale (false until the controller link is up); the firmware
        # tare only reaches the hardware scale, not BLE scales
        supported_fn=cap("hs"),
    ),
    GaggiMateButtonEntityDescription(
        key=UNIQUE_ID_FLUSH_STOP,
        name="Flush Stop",
        icon="mdi:water-pump-off",
        press_fn=lambda coordinator: coordinator.stop_flush(),
    ),
    GaggiMateButtonEntityDescription(
        key=UNIQUE_ID_PROCESS_CLEAR,
        name="Clear Process",
        icon="mdi:broom",
        press_fn=lambda coordinator: coordinator.clear_process(),
    ),
    GaggiMateButtonEntityDescription(
        key=UNIQUE_ID_BREW_CONFIRM,
        name="Confirm Brew",
        icon="mdi:coffee-maker-check",
        # Starts the brew although an error-level warning is active
        press_fn=lambda coordinator: coordinator.start_brew(ignore_warnings=True),
        available_fn=lambda coordinator: coordinator.brew_confirm is not None,
    ),
    GaggiMateButtonEntityDescription(
        key=UNIQUE_ID_BREW_CONFIRM_CANCEL,
        name="Cancel Brew Confirmation",
        icon="mdi:coffee-maker-off",
        press_fn=lambda coordinator: coordinator.cancel_brew_confirm(),
        available_fn=lambda coordinator: coordinator.brew_confirm is not None,
    ),
    GaggiMateButtonEntityDescription(
        key=UNIQUE_ID_BACKFLUSH_START,
        name="Start Backflush",
        icon="mdi:coffee-maker",
        # Selects the backflush profile and runs the pump: disabled by default (see README)
        press_fn=lambda coordinator: coordinator.start_cleaning("backflush"),
        supported_fn=has_warning_key("backflush"),
        entity_registry_enabled_default=False,
    ),
    GaggiMateButtonEntityDescription(
        key=UNIQUE_ID_BACKFLUSH_COMPLETE,
        name="Mark Backflush Done",
        icon="mdi:check-circle-outline",
        press_fn=lambda coordinator: coordinator.complete_cleaning("backflush"),
        supported_fn=has_warning_key("backflush"),
    ),
    GaggiMateButtonEntityDescription(
        key=UNIQUE_ID_DESCALING_START,
        name="Start Descaling",
        icon="mdi:flask-outline",
        # Selects the descaling profile and runs the pump: disabled by default (see README)
        press_fn=lambda coordinator: coordinator.start_cleaning("descaling"),
        supported_fn=has_warning_key("descaling"),
        entity_registry_enabled_default=False,
    ),
    GaggiMateButtonEntityDescription(
        key=UNIQUE_ID_DESCALING_COMPLETE,
        name="Mark Descaling Done",
        icon="mdi:check-circle-outline",
        press_fn=lambda coordinator: coordinator.complete_cleaning("descaling"),
        supported_fn=has_warning_key("descaling"),
    ),
    GaggiMateButtonEntityDescription(
        key=UNIQUE_ID_CHECK_UPDATES,
        name="Check for Updates",
        icon="mdi:cloud-search-outline",
        press_fn=lambda coordinator: coordinator.check_for_updates(),
        supported_fn=has_key("up"),
        # Not while flashing: the firmware ignores the check during an update anyway
        available_fn=lambda coordinator: not coordinator.ota_updating,
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GaggiMate buttons."""
    coordinator: GaggiMateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        GaggiMateStartBrewButton(coordinator, entry),
        GaggiMateStopBrewButton(coordinator, entry),
        GaggiMateFlushButton(coordinator, entry),
    ]

    async_add_entities(entities)
    async_setup_gated(coordinator, entry, async_add_entities, BUTTONS, GaggiMateButton)


class GaggiMateStartBrewButton(GaggiMateEntity, ButtonEntity):
    """Start brew button."""

    _attr_icon = "mdi:coffee"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(coordinator, entry)
        self._attr_name = "Start Brew"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_BREW_START}"

    async def async_press(self) -> None:
        """Handle the button press."""
        try:
            await self.coordinator.start_brew()
            _LOGGER.debug("Started brew on GaggiMate")
        except Exception as err:
            _LOGGER.error("Failed to start brew: %s", err)
            raise


class GaggiMateStopBrewButton(GaggiMateEntity, ButtonEntity):
    """Stop brew button."""

    _attr_icon = "mdi:stop"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(coordinator, entry)
        self._attr_name = "Stop Brew"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_BREW_STOP}"

    async def async_press(self) -> None:
        """Handle the button press."""
        try:
            await self.coordinator.stop_brew()
            _LOGGER.debug("Stopped brew on GaggiMate")
        except Exception as err:
            _LOGGER.error("Failed to stop brew: %s", err)
            raise


class GaggiMateFlushButton(GaggiMateEntity, ButtonEntity):
    """Flush button."""

    _attr_icon = "mdi:water-pump"

    def __init__(self, coordinator: GaggiMateCoordinator, entry: ConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(coordinator, entry)
        self._attr_name = "Flush"
        self._attr_unique_id = f"{coordinator.host}_{UNIQUE_ID_FLUSH}"

    async def async_press(self) -> None:
        """Handle the button press."""
        try:
            await self.coordinator.start_flush()
            _LOGGER.debug("Started flush on GaggiMate")
        except Exception as err:
            _LOGGER.error("Failed to start flush: %s", err)
            raise


class GaggiMateButton(GaggiMateEntity, ButtonEntity):
    """Generic GaggiMate button driven by descriptions."""

    entity_description: GaggiMateButtonEntityDescription

    def __init__(
        self,
        coordinator: GaggiMateCoordinator,
        entry: ConfigEntry,
        description: GaggiMateButtonEntityDescription,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.host}_{description.key}"
        self._attr_name = description.name

    @property
    def available(self) -> bool:
        """Return if entity is available (some buttons only apply in certain states)."""
        if not super().available:
            return False
        available_fn = self.entity_description.available_fn
        return available_fn is None or available_fn(self.coordinator)

    async def async_press(self) -> None:
        """Handle the button press."""
        try:
            await self.entity_description.press_fn(self.coordinator)
            _LOGGER.debug("Pressed %s on GaggiMate", self.entity_description.key)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to press %s: %s", self.entity_description.key, err)
            raise
