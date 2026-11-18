"""Event platform for GaggiMate integration."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import math
from typing import Any

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, UNIQUE_ID_MACHINE_EVENTS, UNIQUE_ID_SHOT_EVENTS
from .coordinator import GaggiMateCoordinator
from .entity import GaggiMateEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

EVENT_SHOT_SAVED = "shot_saved"
EVENT_SHOT_FINISHED = "shot_finished"
EVENT_SCALE_DISCONNECTED = "scale_disconnected"
EVENT_AUTOTUNE_RESULT = "autotune_result"
EVENT_AUTOTUNE_FAILED = "autotune_failed"

PayloadFn = Callable[[dict[str, Any]], dict[str, Any]]


def _int_field(message: dict[str, Any], key: str, tp: str) -> int | None:
    """Return an integer field, or None (debug log) when missing or not an integer."""
    value = message.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    _LOGGER.debug("Event %s: missing or invalid %r (%r)", tp, key, value)
    return None


def _float_field(message: dict[str, Any], key: str, tp: str) -> float | None:
    """Return a finite number as float, or None (debug log) when missing or invalid."""
    value = message.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    _LOGGER.debug("Event %s: missing or invalid %r (%r)", tp, key, value)
    return None


def _str_field(message: dict[str, Any], key: str, tp: str) -> str | None:
    """Return a string field, or None (debug log) when missing or not a string."""
    value = message.get(key)
    if isinstance(value, str):
        return value
    _LOGGER.debug("Event %s: missing or invalid %r (%r)", tp, key, value)
    return None


def _shot_saved(message: dict[str, Any]) -> dict[str, Any]:
    return {"shot_id": _int_field(message, "id", "evt:history-shot-saved")}


def _shot_finished(message: dict[str, Any]) -> dict[str, Any]:
    tp = "evt:shot-finished-stats"
    return {
        "max_pressure_bar": _float_field(message, "maxPressure", tp),
        "avg_flow_ml_s": _float_field(message, "avgFlow", tp),
    }


def _scale_disconnected(message: dict[str, Any]) -> dict[str, Any]:
    tp = "evt:scale:disconnect"
    return {
        "name": _str_field(message, "name", tp),
        "address": _str_field(message, "address", tp),
    }


def _autotune_result(message: dict[str, Any]) -> dict[str, Any]:
    return {"pid": _str_field(message, "pid", "evt:autotune-result")}


def _no_payload(_: dict[str, Any]) -> dict[str, Any]:
    return {}


@dataclass(frozen=True, kw_only=True)
class GaggiMateEventEntityDescription(EventEntityDescription):
    """Describe a GaggiMate event entity.

    ``handlers`` maps a websocket message type to (event_type, payload builder).
    """

    handlers: dict[str, tuple[str, PayloadFn]]


EVENTS: tuple[GaggiMateEventEntityDescription, ...] = (
    GaggiMateEventEntityDescription(
        key=UNIQUE_ID_SHOT_EVENTS,
        name="Shot Events",
        icon="mdi:coffee",
        event_types=[EVENT_SHOT_SAVED, EVENT_SHOT_FINISHED],
        handlers={
            "evt:history-shot-saved": (EVENT_SHOT_SAVED, _shot_saved),
            "evt:shot-finished-stats": (EVENT_SHOT_FINISHED, _shot_finished),
        },
    ),
    GaggiMateEventEntityDescription(
        key=UNIQUE_ID_MACHINE_EVENTS,
        name="Machine Events",
        icon="mdi:bell-ring-outline",
        event_types=[EVENT_SCALE_DISCONNECTED, EVENT_AUTOTUNE_RESULT, EVENT_AUTOTUNE_FAILED],
        handlers={
            "evt:scale:disconnect": (EVENT_SCALE_DISCONNECTED, _scale_disconnected),
            "evt:autotune-result": (EVENT_AUTOTUNE_RESULT, _autotune_result),
            "evt:autotune-failed": (EVENT_AUTOTUNE_FAILED, _no_payload),
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GaggiMate event entities."""
    coordinator: GaggiMateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(GaggiMateEvent(coordinator, entry, d) for d in EVENTS)


class GaggiMateEvent(GaggiMateEntity, EventEntity):
    """Event entity fed by non-status ``evt:*`` websocket messages."""

    entity_description: GaggiMateEventEntityDescription

    def __init__(
        self,
        coordinator: GaggiMateCoordinator,
        entry: ConfigEntry,
        description: GaggiMateEventEntityDescription,
    ) -> None:
        """Initialize the event entity."""
        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.host}_{description.key}"
        self._attr_name = description.name

    async def async_added_to_hass(self) -> None:
        """Subscribe to the coordinator's event bus (removed with the entity)."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_event_listener(self._handle_event))

    @callback
    def _handle_event(self, tp: str, message: dict[str, Any]) -> None:
        """Turn a matching websocket event into an entity event; listener errors are logged by the coordinator."""
        handler = self.entity_description.handlers.get(tp)
        if handler is None:
            return
        event_type, payload_fn = handler
        # Payload builders tolerate bad fields; the coordinator logs listener exceptions.
        payload = payload_fn(message if isinstance(message, dict) else {})
        self._trigger_event(event_type, payload)
        self.async_write_ha_state()
