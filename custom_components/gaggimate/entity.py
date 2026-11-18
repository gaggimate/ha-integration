"""Shared entity base class and capability gating for GaggiMate."""
from __future__ import annotations

from collections.abc import Callable, Iterable
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GaggiMateCoordinator, int_flag

_LOGGER = logging.getLogger(__name__)

GateFn = Callable[[dict[str, Any]], bool]


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
        return (
            self.coordinator.available
            and self.coordinator.data is not None
        )


def has_key(key: str) -> GateFn:
    """Gate: the key is present in the merged status data (even when None)."""
    return lambda data: key in data


def cap(key: str) -> GateFn:
    """Gate: the capability flag is exactly True (false until the controller link is up)."""
    return lambda data: data.get(key) is True


def has_warning_key(key: str) -> GateFn:
    """Gate: the warning list contains an entry with this key."""

    def _gate(data: dict[str, Any]) -> bool:
        warnings = data.get("warn")
        if not isinstance(warnings, list):
            return False
        return any(isinstance(w, dict) and w.get("k") == key for w in warnings)

    return _gate


def flag_is_set(key: str) -> GateFn:
    """Gate: the 1/0 integer flag is 1. cap() never matches these, as they are not booleans."""
    return lambda data: int_flag(data, key) is True


@callback
def async_setup_gated(
    coordinator: GaggiMateCoordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    descriptions: Iterable[EntityDescription],
    entity_cls: Callable[[GaggiMateCoordinator, ConfigEntry, Any], Any],
) -> None:
    """Add entities for descriptions whose ``supported_fn`` is true, now and later.

    Capability flags arrive after the platforms are set up, so each coordinator update
    adds descriptions that have become supported. Add-only: an entity is never removed
    when its key disappears again, and each key is added at most once.
    """
    descriptions = tuple(descriptions)
    added: set[str] = set()
    failed_gates: set[str] = set()

    @callback
    def _add_supported() -> None:
        data = coordinator.data or {}
        new_entities = []
        for description in descriptions:
            if description.key in added:
                continue
            supported_fn = getattr(description, "supported_fn", None)
            try:
                supported = supported_fn is None or bool(supported_fn(data))
            except Exception:  # noqa: BLE001 - a bad gate must not break the update loop
                # Log once per key, then DEBUG (the gate runs on every update)
                level = logging.DEBUG if description.key in failed_gates else logging.ERROR
                failed_gates.add(description.key)
                _LOGGER.log(level, "Gate for %s failed", description.key, exc_info=True)
                continue
            if supported:
                try:
                    entity = entity_cls(coordinator, entry, description)
                except Exception:  # noqa: BLE001 - must not escape the coordinator listener
                    _LOGGER.exception("Failed to create entity %s", description.key)
                    continue
                added.add(description.key)
                new_entities.append(entity)
        if new_entities:
            async_add_entities(new_entities)

    _add_supported()
    entry.async_on_unload(coordinator.async_add_listener(_add_supported))
