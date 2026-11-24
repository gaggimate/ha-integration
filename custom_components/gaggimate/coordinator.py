"""WebSocket coordinator for GaggiMate integration."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
import time
from collections.abc import Callable
from typing import Any, NamedTuple
from contextlib import suppress

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CLEANING_COMPLETE_MESSAGES,
    CLEANING_START_MESSAGES,
    DOMAIN,
    HISTORY_INDEX_MAX_BYTES,
    HISTORY_INDEX_PATH,
    MSG_TYPE_BREW_CONFIRM_CANCEL,
    MSG_TYPE_CHANGE_GRIND_TARGET,
    MSG_TYPE_EVENT_BREW_CONFIRM,
    MSG_TYPE_EVENT_BREW_CONFIRM_CANCEL,
    MSG_TYPE_EVENT_OTA_PROGRESS,
    MSG_TYPE_EVENT_PREFIX,
    MSG_TYPE_FLUSH_START,
    MSG_TYPE_FLUSH_STOP,
    MSG_TYPE_GRIND_ACTIVATE,
    MSG_TYPE_GRIND_DEACTIVATE,
    MSG_TYPE_GRIND_TARGET_LOWER,
    MSG_TYPE_GRIND_TARGET_RAISE,
    MSG_TYPE_MODE_CHANGE,
    MSG_TYPE_OTA_SETTINGS,
    MSG_TYPE_OTA_SETTINGS_RESPONSE,
    MSG_TYPE_OTA_START,
    MSG_TYPE_PROCESS_ACTIVATE,
    MSG_TYPE_PROCESS_CLEAR,
    MSG_TYPE_PROCESS_DEACTIVATE,
    MSG_TYPE_PROFILES_LIST,
    MSG_TYPE_PROFILES_LIST_RESPONSE,
    MSG_TYPE_PROFILES_SELECT,
    MSG_TYPE_SCALE_TARE,
    MSG_TYPE_STATUS,
    MSG_TYPE_STEAM_TOGGLE,
    MSG_TYPE_TEMP_LOWER,
    MSG_TYPE_TEMP_RAISE,
    MachineMode,
    OTA_CHANNELS,
    OTA_COMPONENTS,
    OTA_PHASE_CONTROLLER,
    OTA_PHASE_DISPLAY,
    OTA_PHASE_DISPLAY_FS,
    OTA_PHASE_FAILED,
    OTA_PHASE_FINISHED,
    OTA_PHASE_IDLE,
    OTA_REFRESH_DELAY,
    OTA_START_PENDING,
    OTA_WATCHDOG_GRACE,
    GRIND_DURATION_MAX_S,
    GRIND_DURATION_MIN_S,
    GRIND_TARGET_MESSAGE_DELAY,
    GRIND_TARGET_SETTLE,
    GRIND_TARGET_STALL,
    GRIND_TARGET_TIMEOUT,
    GRIND_WEIGHT_MAX_G,
    GRIND_WEIGHT_MIN_G,
    STEAM_TOGGLE_SETTLE,
    MSG_TYPE_HISTORY_DELETE,
    MSG_TYPE_HISTORY_LIST,
    MSG_TYPE_HISTORY_NOTES_GET,
    WS_CONNECT_TIMEOUT,
    WS_RECONNECT_DELAYS,
    WS_REQUEST_TIMEOUT,
    WS_UNAVAILABLE_TIMEOUT,
)

from .history import parse_shot_index

_LOGGER = logging.getLogger(__name__)


def int_flag(data: dict[str, Any], key: str) -> bool | None:
    """Read a 1/0 status flag (bta, gt and gact are integers, not booleans); None when absent or not 0/1."""
    value = data.get(key)
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def other_process_running(data: dict[str, Any]) -> bool:
    """True while a process other than the grind is active (a brew, flush, steam or water)."""
    process = data.get("process")
    return isinstance(process, dict) and process.get("a") == 1 and process.get("s") != "grind"


class _GrindTarget(NamedTuple):
    """How one grind target kind maps onto the status keys, step size and allowed range."""

    by_weight: bool
    key: str
    scale: float
    step: float
    low: float
    high: float


# Only the range constants are bound here; the GRIND_TARGET_* timings are read at call time
_GRIND_TARGETS = {
    "duration": _GrindTarget(False, "gtd", 1000.0, 1.0, GRIND_DURATION_MIN_S, GRIND_DURATION_MAX_S),
    "weight": _GrindTarget(True, "gtv", 1.0, 0.5, GRIND_WEIGHT_MIN_G, GRIND_WEIGHT_MAX_G),
}


def _ws_timeout(seconds: float) -> Any:
    """Build the ws_connect timeout (ClientWSTimeout needs aiohttp >= 3.11)."""
    ws_timeout = getattr(aiohttp, "ClientWSTimeout", None)
    if ws_timeout is None:
        return seconds
    return ws_timeout(ws_close=seconds)


class GaggiMateCoordinator(DataUpdateCoordinator):
    """Coordinator to manage WebSocket connection to GaggiMate."""

    def __init__(self, hass: HomeAssistant, host: str, port: int = 80, use_ssl: bool = False) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,  # Push updates over WebSocket
        )
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._reconnect_attempt = 0
        self._last_status_time: float | None = None
        self._available = False
        self._availability_check_task: asyncio.Task | None = None
        self._listen_task: asyncio.Task | None = None
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._profiles: dict[str, str] = {}
        self._ota_settings: dict[str, Any] = {}
        self._event_listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._ota_refresh_task: asyncio.Task | None = None
        # Firmware update run: last evt:ota-progress phase/percent, whether a flashing phase
        # (1/2/3) was seen, when an install was sent and until when watchdog silence is tolerated
        # (the last two are time.monotonic() values)
        self._ota_phase: int | None = None
        self._ota_percent: int | None = None
        self._ota_ran = False
        self._ota_start_sent_at: float | None = None
        self._ota_grace_until: float | None = None
        # Last steam toggle sent: (requested state, monotonic time); see set_steam_heater
        self._steam_pending: tuple[bool, float] | None = None
        self._grind_target_lock = asyncio.Lock()  # one grind target change at a time
        # Pending brew confirmation: the warnings from evt:brew:confirm (None = nothing pending)
        self._brew_confirm: list[dict[str, Any]] | None = None

        # NEW: Protect connection logic and shutdown state
        self._connect_lock = asyncio.Lock()
        self._shutting_down = False

    @property
    def ws_url(self) -> str:
        """Return WebSocket URL."""
        protocol = "wss" if self.use_ssl else "ws"
        return f"{protocol}://{self.host}:{self.port}/ws"

    async def async_start(self) -> None:
        """Start the WebSocket connection."""
        if self._shutting_down:
            return

        if self._session is None:
            self._session = aiohttp.ClientSession()

        try:
            await self._connect()
        except Exception:
            # Setup will be retried with a new coordinator; do not leak the session
            await self._session.close()
            self._session = None
            raise

        # Start availability checker once
        if self._availability_check_task is None:
            self._availability_check_task = asyncio.create_task(self._check_availability())

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator cleanly."""
        self._shutting_down = True
        self._event_listeners.clear()

        if self._ota_refresh_task:
            self._ota_refresh_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._ota_refresh_task
            self._ota_refresh_task = None

        if self._availability_check_task:
            self._availability_check_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._availability_check_task
            self._availability_check_task = None

        if self._reconnect_task:
            self._reconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None

        # Close the websocket first, then cancel the listener.
        ws = self._ws
        self._ws = None
        if ws is not None and not ws.closed:
            with suppress(Exception):
                await ws.close()

        if self._listen_task:
            self._listen_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._listen_task
            self._listen_task = None

        if self._session:
            await self._session.close()
            self._session = None

    async def _connect(self) -> None:
        """Connect to WebSocket, ensuring only one connection attempt at a time."""
        async with self._connect_lock:
            if self._shutting_down:
                return

            # If already connected, do nothing
            if self._ws and not self._ws.closed:
                with suppress(Exception):
                    await self._ws.close()
                self._ws = None

            # Kill stale listener if present
            if self._listen_task and not self._listen_task.done():
                self._listen_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._listen_task
                self._listen_task = None

            try:
                if self._session is None:
                    self._session = aiohttp.ClientSession()

                _LOGGER.info("Connecting to GaggiMate at %s", self.ws_url)

                self._ws = await asyncio.wait_for(
                    self._session.ws_connect(
                        self.ws_url,
                        timeout=_ws_timeout(WS_CONNECT_TIMEOUT),
                    ),
                    timeout=WS_CONNECT_TIMEOUT,
                )

                _LOGGER.info("Successfully connected to GaggiMate")
                # Grace period for the availability watchdog (no heartbeat is used)
                self._last_status_time = time.monotonic()
                self._reconnect_attempt = 0

                # Start listener
                self._listen_task = asyncio.create_task(self._listen())

                # Prime data
                self.hass.async_create_task(self.request_ota_settings())
                self.hass.async_create_task(self.request_profiles_list())

            except Exception as err:
                reason = str(err) or type(err).__name__
                if isinstance(err, asyncio.TimeoutError):
                    reason = str(err) or "timed out"
                # Log level is chosen by the caller (first failure vs. retries)
                # DO NOT trigger reconnect from inside locked context
                # just let caller handle it
                raise UpdateFailed(f"Failed to connect: {reason}") from err

    async def _listen(self) -> None:
        """Listen for messages from the WebSocket."""
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.error("WebSocket error: %s", self._ws.exception())
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    _LOGGER.warning("WebSocket closed")
                    break
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.error("WebSocket listen loop error: %s", err)
        finally:
            # A confirmation cannot be answered over a dead link
            self._clear_brew_confirm(notify=False)
            ota_changed = self._reset_ota_state()
            if self._available:
                self._available = False
                self.async_update_listeners()
            elif ota_changed:
                self.async_update_listeners()
            if self._ws:
                with suppress(Exception):
                    await self._ws.close()
                self._ws = None

            # optional but strongly recommended
            for future in self._pending_requests.values():
                if not future.done():
                    future.set_exception(UpdateFailed("Connection lost"))
            self._pending_requests.clear()

            if not self._shutting_down:
                await self._schedule_reconnect()

    async def _handle_message(self, data: str) -> None:
        """Handle an incoming WebSocket message."""
        try:
            message = json.loads(data)
            self._process_message(message)
        except json.JSONDecodeError as err:
            _LOGGER.error("Invalid WebSocket JSON: %s", err)

    def _process_message(self, message: dict[str, Any]) -> None:
        """Route a decoded message to its handler."""
        msg_type = message.get("tp")

        if msg_type == MSG_TYPE_STATUS:
            self._handle_status(message)
            return

        # Resolve the pending request first, for every response type
        self._resolve_pending(message)

        if isinstance(msg_type, str) and msg_type.startswith(MSG_TYPE_EVENT_PREFIX):
            self._handle_event(msg_type, message)
            return

        if msg_type == MSG_TYPE_OTA_SETTINGS_RESPONSE:
            self._handle_ota_response(message)
            return

        if msg_type == MSG_TYPE_PROFILES_LIST_RESPONSE:
            self._handle_profiles_response(message)

    def _handle_status(self, message: dict[str, Any]) -> None:
        """Merge a (partial) status frame into the coordinator data."""
        self._reconnect_attempt = 0
        self._last_status_time = time.monotonic()

        if self._ota_phase == OTA_PHASE_FINISHED:
            # Still sending status after "finished": the display did not reboot (nothing to flash)
            self._ota_phase = None
            self._ota_percent = None
            self._ota_ran = False
            self._clear_ota_updating()

        if not self._available:
            _LOGGER.info("GaggiMate became available")
            self._available = True
            self.async_update_listeners()
        # Firmware sends partial status frames: fast telemetry every tick and the
        # slow-changing state only when it changes (plus a full snapshot on connect).
        # Merge onto the last known status; a key sent as null clears it.
        previous = self.data or {}
        merged = {**previous, **message}
        if self._brew_confirm is not None and self._brew_confirm_stale(merged):
            self._clear_brew_confirm(notify=False)  # the update below notifies
        self.async_set_updated_data(merged)
        # Update availability changed: the OTA settings (versions, update flags) are stale
        if "up" in previous and "up" in message and previous["up"] != message["up"]:
            self._schedule_ota_refresh()

    def _resolve_pending(self, message: dict[str, Any]) -> None:
        """Complete the pending request matching the message's rid, if any."""
        rid = message.get("rid")
        if rid and rid in self._pending_requests:
            future = self._pending_requests.pop(rid)
            if not future.done():
                future.set_result(message)

    def _handle_event(self, msg_type: str, message: dict[str, Any]) -> None:
        """Track brew-confirm state for events, then pass them to listeners."""
        if msg_type == MSG_TYPE_EVENT_BREW_CONFIRM:
            self._set_brew_confirm(message.get("warn"))
        elif msg_type == MSG_TYPE_EVENT_BREW_CONFIRM_CANCEL:
            self._clear_brew_confirm()
        elif msg_type == MSG_TYPE_EVENT_OTA_PROGRESS:
            self._handle_ota_progress(message)
        self._dispatch_event(msg_type, message)

    def _handle_ota_progress(self, message: dict[str, Any]) -> None:
        """Track the update phase and progress; malformed frames are ignored."""
        phase = message.get("phase")
        if isinstance(phase, bool) or not isinstance(phase, int) or not 0 <= phase <= OTA_PHASE_FAILED:
            _LOGGER.debug("Ignoring malformed OTA progress: %s", message)
            return
        progress = message.get("progress")
        if isinstance(progress, bool) or not isinstance(progress, int):
            progress = 0
        progress = max(0, min(100, progress))

        now = time.monotonic()
        if phase in (OTA_PHASE_DISPLAY, OTA_PHASE_DISPLAY_FS, OTA_PHASE_CONTROLLER):
            changed = (self._ota_phase, self._ota_percent) != (phase, progress)
            self._ota_phase = phase
            self._ota_percent = progress
            self._ota_ran = True
            self._ota_grace_until = now + OTA_WATCHDOG_GRACE
            self._ota_start_sent_at = None
            if changed:
                self.async_update_listeners()
            return

        # Finished (4), failed (5) or idle (0): the run is over. Give the status stream a fresh
        # window (a successful update reboots the display, a no-op or failure does not).
        if phase == OTA_PHASE_FINISHED:
            self._ota_phase = phase
            self._ota_percent = 100
        else:
            self._ota_phase = phase
            self._ota_percent = None
            self._ota_ran = False
        self._ota_grace_until = None
        self._ota_start_sent_at = None
        self._clear_ota_updating()
        self._last_status_time = now
        self._schedule_ota_refresh()  # the settings still say updating until re-read
        self.async_update_listeners()

    def _handle_ota_response(self, message: dict[str, Any]) -> None:
        """Store the OTA settings response."""
        self._ota_settings = message
        if message.get("updating") is True:
            if self._ota_phase in (None, OTA_PHASE_IDLE, OTA_PHASE_FINISHED, OTA_PHASE_FAILED):
                # A new run began elsewhere (web UI, display): forget the old one
                self._ota_phase = None
                self._ota_percent = None
                self._ota_ran = False
            self._ota_grace_until = time.monotonic() + OTA_WATCHDOG_GRACE
        elif self._ota_phase is None and not self._start_pending():
            # Nothing is running: do not keep the watchdog blind
            self._ota_grace_until = None
        self.async_update_listeners()

    def _start_pending(self) -> bool:
        """True while an install request was sent a moment ago and no progress arrived yet."""
        sent_at = self._ota_start_sent_at
        return sent_at is not None and time.monotonic() - sent_at < OTA_START_PENDING

    def _clear_ota_updating(self) -> bool:
        """Drop a stale updating=true from the stored settings; True if it was set.

        The firmware's last broadcast still says updating after a run ended; a later
        broadcast with updating=true starts a new run via _handle_ota_response.
        """
        if self._ota_settings.get("updating") is not True:
            return False
        self._ota_settings = {**self._ota_settings, "updating": False}
        return True

    def _reset_ota_state(self) -> bool:
        """Forget the update run (the connection is gone); True if anything changed."""
        changed = (
            self._ota_phase is not None
            or self._ota_percent is not None
            or self._ota_ran
            or self._ota_start_sent_at is not None
            or self._ota_grace_until is not None
        )
        self._ota_phase = None
        self._ota_percent = None
        self._ota_ran = False
        self._ota_start_sent_at = None
        self._ota_grace_until = None
        if self._ota_settings.get("updating") is True:
            self._ota_settings = {**self._ota_settings, "updating": False}
            changed = True
        return changed

    def _handle_profiles_response(self, message: dict[str, Any]) -> None:
        """Store the profile list; an error or malformed list keeps the current one."""
        profiles = message.get("profiles")
        if message.get("error") or not isinstance(profiles, list):
            return
        self._profiles = self._parse_profiles(profiles)
        self.async_update_listeners()

    @staticmethod
    def _parse_profiles(profiles: list[Any]) -> dict[str, str]:
        """Map profile label to id, skipping entries without both."""
        parsed: dict[str, str] = {}
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            label = profile.get("label")
            pid = profile.get("id")
            if label and pid:
                parsed[label] = pid
        return parsed

    @property
    def brew_confirm(self) -> list[dict[str, Any]] | None:
        """Warnings of the pending brew confirmation, or None when nothing is pending."""
        return self._brew_confirm

    def _set_brew_confirm(self, warnings: Any) -> None:
        """Record a brew start that the firmware blocked and wants confirmed."""
        self._brew_confirm = [w for w in warnings if isinstance(w, dict)] if isinstance(warnings, list) else []
        self.async_update_listeners()

    def _clear_brew_confirm(self, notify: bool = True) -> None:
        """Drop the pending confirmation; notify listeners only if one was pending."""
        if self._brew_confirm is None:
            return
        self._brew_confirm = None
        if notify:
            self.async_update_listeners()

    def _brew_confirm_stale(self, data: dict[str, Any]) -> bool:
        """True when the status shows the confirmation was answered or is moot.

        The firmware has no "confirmed" event: a confirmed start is only visible as
        process.a == 1. Leaving brew mode also dismisses it (the firmware asks in brew mode only).
        """
        process = data.get("process")
        if isinstance(process, dict) and process.get("a") == 1:
            return True
        mode = data.get("m")
        return mode is not None and mode != MachineMode.BREW

    def _schedule_ota_refresh(self) -> None:
        """Re-read the OTA settings shortly after `up` changed (debounced: one pending refresh)."""
        if self._shutting_down:
            return
        if self._ota_refresh_task and not self._ota_refresh_task.done():
            return
        self._ota_refresh_task = self.hass.async_create_background_task(
            self._refresh_ota_settings(), f"{DOMAIN}_ota_refresh"
        )

    async def _refresh_ota_settings(self) -> None:
        await asyncio.sleep(OTA_REFRESH_DELAY)
        await self.request_ota_settings()

    @callback
    def async_add_event_listener(
        self, listener: Callable[[str, dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Register a listener for non-status ``evt:*`` messages; returns an unsubscribe.

        Listeners receive the shared message dict and must not mutate it.
        """
        self._event_listeners.append(listener)

        @callback
        def _remove() -> None:
            with suppress(ValueError):
                self._event_listeners.remove(listener)

        return _remove

    def _dispatch_event(self, msg_type: str, message: dict[str, Any]) -> None:
        """Pass an event message to all listeners; one failing listener does not stop the rest."""
        # Iterate a snapshot: listeners may unsubscribe while being notified
        for listener in self._event_listeners.copy():
            try:
                listener(msg_type, message)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error in event listener for %s", msg_type)

    async def _schedule_reconnect(self) -> None:
        """Ensure a reconnect loop is running."""
        if self._shutting_down:
            return

        if self._reconnect_task and not self._reconnect_task.done():
            return

        self._reconnect_task = asyncio.create_task(self._reconnect_loop())


    async def _reconnect_loop(self) -> None:
        """Reconnect loop with incremental backoff."""
        try:
            while not self._shutting_down:
                delay_index = min(self._reconnect_attempt, len(WS_RECONNECT_DELAYS) - 1)
                delay = WS_RECONNECT_DELAYS[delay_index]

                _LOGGER.info(
                    "Reconnect attempt %s in %s seconds",
                    self._reconnect_attempt + 1,
                    delay,
                )

                await asyncio.sleep(delay)

                try:
                    await self._connect()

                    # SUCCESS → reset and exit loop
                    self._reconnect_attempt = 0
                    _LOGGER.info("Reconnect successful")
                    return

                except Exception as err:
                    self._reconnect_attempt += 1
                    # First failure of an outage is a warning, retries are debug noise
                    level = logging.WARNING if self._reconnect_attempt == 1 else logging.DEBUG
                    _LOGGER.log(level, "Reconnect failed: %s", err)

        finally:
            self._reconnect_task = None

    async def _check_availability(self) -> None:
        """Monitor status updates and reconnect if stale."""
        while not self._shutting_down:
            try:
                await asyncio.sleep(1)
                await self._check_once()
            except Exception as err:
                _LOGGER.error("Availability check error: %s", err)

    async def _check_once(self) -> None:
        """Run one availability check; mark stale if no recent status."""
        if self._last_status_time is None:
            return

        elapsed = time.monotonic() - self._last_status_time

        # The display is busy flashing and sends no status: tolerate silence during an update
        if self._ota_grace_until is not None and time.monotonic() < self._ota_grace_until:
            return

        if elapsed > WS_UNAVAILABLE_TIMEOUT:
            await self._mark_stale(elapsed)

    async def _mark_stale(self, elapsed: float) -> None:
        """Mark the device unavailable and close the WebSocket to reconnect."""
        _LOGGER.warning(
            "No status update for %.1f seconds - marking unavailable",
            elapsed,
        )

        self._last_status_time = None

        self._clear_brew_confirm(notify=False)
        if self._available:
            self._available = False
            self.async_set_updated_data({})
            self.async_update_listeners()

        # Closing WS causes listener to trigger reconnect
        if self._ws and not self._ws.closed:
            await self._ws.close()

    async def send_message(self, message: dict[str, Any]) -> None:
        """Send message to the device."""
        if self._ws is None or self._ws.closed:
            await self._schedule_reconnect()
            raise UpdateFailed("WebSocket not connected")
        try:
            await self._ws.send_json(message)
            _LOGGER.debug("Sent message: %s", message)
        except Exception as err:
            await self._schedule_reconnect()
            raise UpdateFailed(f"Failed to send message: {err}") from err

    async def _request(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send a request and await the matching response."""
        rid = str(uuid.uuid4())
        message["rid"] = rid
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_requests[rid] = future

        try:
            await self.send_message(message)
            response = await asyncio.wait_for(future, timeout=WS_REQUEST_TIMEOUT)
        except asyncio.TimeoutError as err:
            raise UpdateFailed("Timed out waiting for response") from err
        finally:
            self._pending_requests.pop(rid, None)

        error = response.get("error")
        if error:
            raise UpdateFailed(str(error))
        return response

    def _require_ready(self) -> None:
        """Raise if the controller reports a state other than ready.

        Older firmware sends no "sys" object; then nothing is checked.
        """
        sys_state = (self.data or {}).get("sys")
        if not isinstance(sys_state, dict):
            return
        state = sys_state.get("s")
        if state is None:
            return
        if state != "ready":
            raise UpdateFailed(
                f"GaggiMate is not ready (state: {state}: {sys_state.get('m', '')})"
            )

    async def set_mode(self, mode: int) -> None:
        self._require_ready()
        # Firmware ignores the mode unless it is a JSON integer
        await self.send_message({"tp": MSG_TYPE_MODE_CHANGE, "mode": int(mode)})

    async def set_temperature(self, temperature: float) -> None:
        if self.data is None:
            raise UpdateFailed("No status data available")
        current_target = self.data.get("tt")
        if current_target is None:
            raise UpdateFailed("Target temperature missing")

        delta = int(round(temperature - float(current_target)))
        if delta == 0:
            return

        msg_type = MSG_TYPE_TEMP_RAISE if delta > 0 else MSG_TYPE_TEMP_LOWER
        steps = abs(delta)

        for _ in range(steps):
            await self.send_message({"tp": msg_type})
            await asyncio.sleep(0.05)

    async def start_brew(self, ignore_warnings: bool = False) -> None:
        """Start the brew; with ignore_warnings the firmware skips its error-warning prompt.

        The flag is only sent when true (a plain start must not carry it). The firmware
        does not reply: an error-level warning gives evt:brew:confirm instead of a start.
        """
        self._require_ready()
        message: dict[str, Any] = {"tp": MSG_TYPE_PROCESS_ACTIVATE}
        if ignore_warnings:
            message["ignoreWarnings"] = True  # must be a JSON boolean
        await self.send_message(message)

    async def cancel_brew_confirm(self) -> None:
        """Decline the pending confirmation; the state clears on evt:brew:confirm:cancel."""
        await self.send_message({"tp": MSG_TYPE_BREW_CONFIRM_CANCEL})

    async def stop_brew(self) -> None:
        await self.send_message({"tp": MSG_TYPE_PROCESS_DEACTIVATE})

    async def start_flush(self) -> None:
        await self._request({"tp": MSG_TYPE_FLUSH_START})

    async def stop_flush(self) -> None:
        """Release a hold-to-flush (the firmware ignores it during a fixed-length flush)."""
        response = await self._request({"tp": MSG_TYPE_FLUSH_STOP})
        if response.get("success") is False:
            raise UpdateFailed("Flush stop failed")

    async def clear_process(self) -> None:
        """Clear the finished process state (no response from the firmware)."""
        await self.send_message({"tp": MSG_TYPE_PROCESS_CLEAR})

    async def start_cleaning(self, kind: str) -> None:
        """Select the cleaning profile and start it; kind is "backflush" or "descaling".

        The firmware answers success false while a process runs or the profile cannot be
        selected. success true only means the start was accepted: an error-level warning
        gives a brew confirmation instead, and an activate the firmware ignores still
        reports true. The start also forces brew mode and wakes the machine.
        """
        self._require_ready()
        response = await self._request({"tp": CLEANING_START_MESSAGES[kind]})
        if response.get("success") is False:
            raise UpdateFailed("Machine busy or profile unavailable")

    async def complete_cleaning(self, kind: str) -> None:
        """Mark a cleaning as done, resetting its schedule (the firmware has no precondition)."""
        response = await self._request({"tp": CLEANING_COMPLETE_MESSAGES[kind]})
        if response.get("success") is False:
            raise UpdateFailed("Could not mark cleaning as done")

    async def tare_scale(self) -> None:
        """Tare the scale (no response from the firmware)."""
        await self.send_message({"tp": MSG_TYPE_SCALE_TARE})

    async def set_steam_heater(self, on: bool) -> None:
        """Switch the dual-boiler steam heater on or off.

        The firmware only offers a toggle, so this sends it only when the effective
        state differs from the requested one. The reported state (`sa`) lags a toggle
        by up to one status frame, so a toggle sent within STEAM_TOGGLE_SETTLE seconds
        counts as applied until `sa` confirms it; this makes a rapid repeated call a
        no-op instead of toggling back. Not covered: another client (web UI, display)
        toggling at the same moment, which cannot be detected from here.
        """
        sa = (self.data or {}).get("sa")
        if not isinstance(sa, bool):
            raise UpdateFailed("Steam heater state unknown")

        effective = sa
        pending = self._steam_pending
        if pending is not None:
            requested, sent_at = pending
            if sa == requested or time.monotonic() - sent_at >= STEAM_TOGGLE_SETTLE:
                self._steam_pending = None
            else:
                effective = requested

        if effective == on:
            return
        if (self.data or {}).get("m") == MachineMode.STANDBY:
            raise UpdateFailed("Steam heater cannot be changed in standby")

        # Record before the await so a concurrent call sees it
        self._steam_pending = (on, time.monotonic())
        try:
            await self.send_message({"tp": MSG_TYPE_STEAM_TOGGLE})
        except BaseException:
            # Includes cancellation: a toggle that was never sent must not count as applied
            self._steam_pending = None
            raise

    async def activate_grind(self) -> None:
        """Start the grinder (no response from the firmware).

        Sent only while `gact` reports the grinder idle. Raises when the machine is not ready
        (the firmware would drop the request silently) or while a brew runs (the firmware
        would clear the running brew's scale source before refusing the start).
        """
        active = int_flag(self.data or {}, "gact")
        if active is None:
            raise UpdateFailed("Grind state unknown")
        if active:
            return
        self._require_ready()
        if other_process_running(self.data or {}):
            raise UpdateFailed("Cannot start grinding while a brew is running")
        await self.send_message({"tp": MSG_TYPE_GRIND_ACTIVATE})

    async def deactivate_grind(self) -> None:
        """Stop the grinder (no response from the firmware).

        The firmware stops whatever process is running, so this is sent only while `gact`
        reports a grind and no other process is active; otherwise a brew in progress would
        be stopped.
        """
        active = int_flag(self.data or {}, "gact")
        if active is None:
            raise UpdateFailed("Grind state unknown")
        if not active or other_process_running(self.data or {}):
            return
        await self.send_message({"tp": MSG_TYPE_GRIND_DEACTIVATE})

    async def set_grind_by_weight(self, on: bool) -> None:
        """Select the grind target type: by weight (on) or by time (off).

        The firmware only stores the choice (no response). Sent only when `gt` differs.
        """
        by_weight = int_flag(self.data or {}, "gt")
        if by_weight is None:
            raise UpdateFailed("Grind target type unknown")
        if by_weight == on:
            return
        # Firmware reads the target only as a JSON integer
        await self.send_message({"tp": MSG_TYPE_CHANGE_GRIND_TARGET, "target": 1 if on else 0})

    async def set_grind_target(self, kind: str, value: float) -> None:
        """Set the grind target duration (kind "duration", seconds) or weight (kind "weight", grams).

        The firmware only has raise/lower messages, each moving the active target by one step
        (1 s or 0.5 g, clamped to 5-300 s / 5-250 g), so the delta is sent as that many
        messages. Only the target type selected by `gt` can be changed. Stops early when `gt`
        flips to the other type, when the reported target has reached the request, or when it
        has not moved for GRIND_TARGET_STALL seconds; the whole call is capped by
        GRIND_TARGET_TIMEOUT. Relative stepping can race with another client changing the target.
        """
        spec = _GRIND_TARGETS.get(kind)
        if spec is None:
            raise ValueError(f"Unknown grind target kind: {kind}")

        # One change at a time (shared by both numbers); the start value is read under the lock
        async with self._grind_target_lock:
            self._require_grind_type(spec.by_weight, kind)
            start = self._grind_value(spec.key, spec.scale)
            if start is None:
                raise UpdateFailed(f"Grind target {kind} unknown")

            target = min(max(float(value), spec.low), spec.high)
            steps = int(round((target - start) / spec.step))
            if steps == 0:
                return
            end = start + steps * spec.step

            try:
                async with asyncio.timeout(GRIND_TARGET_TIMEOUT):
                    reached = await self._step_grind_target(spec, kind, start, end, steps)
                    # Let the reported value settle so the next call starts from a fresh one
                    if not reached:
                        await self._settle_grind_target(spec, kind, end)
            except TimeoutError as err:
                raise UpdateFailed("Timed out setting the grind target") from err

    def _grind_value(self, key: str, scale: float) -> float | None:
        """The reported grind target in user units, or None when absent or not numeric."""
        raw = (self.data or {}).get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        return float(raw) / scale

    def _require_grind_type(self, by_weight: bool, kind: str) -> None:
        """Raise unless the device's grind target type matches the requested kind."""
        if int_flag(self.data or {}, "gt") != by_weight:
            raise UpdateFailed(f"Grind target is not set to {kind}")

    async def _step_grind_target(
        self, spec: _GrindTarget, kind: str, start: float, end: float, steps: int
    ) -> bool:
        """Send raise/lower messages until the reported target reaches `end`; True when it did."""
        raising = steps > 0
        msg_type = MSG_TYPE_GRIND_TARGET_RAISE if raising else MSG_TYPE_GRIND_TARGET_LOWER
        observed = start
        moved_at = time.monotonic()
        for _ in range(abs(steps)):
            self._require_grind_type(spec.by_weight, kind)
            now_value = self._grind_value(spec.key, spec.scale)
            if now_value is not None:
                if now_value != observed:
                    observed, moved_at = now_value, time.monotonic()
                # Reported value lags the messages in flight, so it never overshoots
                if (raising and now_value >= end) or (not raising and now_value <= end):
                    return True
            if time.monotonic() - moved_at > GRIND_TARGET_STALL:
                raise UpdateFailed("Grind target stopped moving")
            await self.send_message({"tp": msg_type})
            await asyncio.sleep(GRIND_TARGET_MESSAGE_DELAY)
        return False

    async def _settle_grind_target(self, spec: _GrindTarget, kind: str, end: float) -> None:
        """Wait (up to GRIND_TARGET_SETTLE) for the reported target to equal `end`."""
        deadline = time.monotonic() + GRIND_TARGET_SETTLE
        while self._grind_value(spec.key, spec.scale) != end and time.monotonic() < deadline:
            self._require_grind_type(spec.by_weight, kind)
            await asyncio.sleep(GRIND_TARGET_MESSAGE_DELAY)

    async def request_profiles_list(self) -> None:
        try:
            await self._request({"tp": MSG_TYPE_PROFILES_LIST})
        except UpdateFailed:
            _LOGGER.debug("Failed to request profile list")

    async def select_profile(self, profile_id: str) -> None:
        await self._request({"tp": MSG_TYPE_PROFILES_SELECT, "id": profile_id})

    async def request_ota_settings(self) -> None:
        try:
            await self.send_message({"tp": MSG_TYPE_OTA_SETTINGS})
        except UpdateFailed:
            _LOGGER.debug("Failed to request OTA settings")

    async def request_history_list(self) -> list[dict[str, Any]]:
        response = await self._request({"tp": MSG_TYPE_HISTORY_LIST})
        history = response.get("history")
        if history is None or not isinstance(history, list):
            raise UpdateFailed("Invalid history list response")
        return history

    @property
    def http_base_url(self) -> str:
        """Return the HTTP base URL of the device."""
        protocol = "https" if self.use_ssl else "http"
        return f"{protocol}://{self.host}:{self.port}"

    async def async_fetch_history_index(self) -> list[dict[str, Any]]:
        """Return the shot history entries (without deleted ones).

        Current firmware serves a binary index over HTTP; only if that returns 404
        the legacy websocket list is tried. Any other problem raises and must
        abort the caller (no deletes after a read/parse error).
        """
        if self._session is None or self._session.closed:
            raise UpdateFailed("Not connected")
        url = f"{self.http_base_url}{HISTORY_INDEX_PATH}"
        data = await self._download_shot_index(url)
        if data is None:
            _LOGGER.debug("No shot index at %s, trying legacy history list", url)
            return await self.request_history_list()
        return parse_shot_index(data)

    async def _download_shot_index(self, url: str) -> bytes | None:
        """Download the shot index; return None on HTTP 404 (legacy firmware)."""
        try:
            timeout = aiohttp.ClientTimeout(total=WS_REQUEST_TIMEOUT)
            async with self._session.get(url, timeout=timeout, allow_redirects=False) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    raise UpdateFailed(f"Shot index request failed: HTTP {resp.status}")
                return await self._read_capped(resp)
        except UpdateFailed:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Failed to fetch shot index: {err or type(err).__name__}") from err

    @staticmethod
    async def _read_capped(resp: aiohttp.ClientResponse) -> bytes:
        """Read the response body, refusing more than HISTORY_INDEX_MAX_BYTES."""
        if (resp.content_length or 0) > HISTORY_INDEX_MAX_BYTES:
            raise UpdateFailed("Shot index is too large")
        data = bytearray()
        async for chunk in resp.content.iter_chunked(8192):
            data.extend(chunk)
            if len(data) > HISTORY_INDEX_MAX_BYTES:
                raise UpdateFailed("Shot index is too large")
        return bytes(data)

    async def delete_history_item(self, shot_id: int | str) -> None:
        await self._request({"tp": MSG_TYPE_HISTORY_DELETE, "id": str(shot_id)})

    async def get_history_notes(self, shot_id: int | str) -> dict[str, Any]:
        response = await self._request({"tp": MSG_TYPE_HISTORY_NOTES_GET, "id": str(int(shot_id))})
        notes = response.get("notes")
        if notes is None:
            return {}
        if not isinstance(notes, dict):
            raise UpdateFailed("Invalid notes format")
        return notes

    @property
    def profiles(self) -> dict[str, str]:
        return self._profiles

    @property
    def ota_settings(self) -> dict[str, Any]:
        return self._ota_settings

    @property
    def ota_phase(self) -> int | None:
        """Last evt:ota-progress phase of the current run (None when none was seen)."""
        return self._ota_phase

    @property
    def ota_percent(self) -> int | None:
        """Progress (0..100) of the current phase."""
        return self._ota_percent

    @property
    def ota_channel(self) -> str | None:
        """The update channel key ("latest" or "nightly"), None when unknown."""
        channel = self._ota_settings.get("channel")
        if isinstance(channel, str) and channel in OTA_CHANNELS:
            return channel
        return None

    @property
    def ota_updating(self) -> bool:
        """True while a firmware update runs (or was just requested)."""
        sent_at = self._ota_start_sent_at
        if sent_at is not None and time.monotonic() - sent_at < OTA_START_PENDING:
            return True
        phase = self._ota_phase
        if phase in (OTA_PHASE_DISPLAY, OTA_PHASE_DISPLAY_FS, OTA_PHASE_CONTROLLER):
            return True
        if phase == OTA_PHASE_FINISHED:
            return self._ota_ran
        return self._ota_settings.get("updating") is True

    async def start_ota(self, component: str) -> None:
        """Ask the firmware to install the display, the controller or both.

        The firmware sends no response: progress arrives as evt:ota-progress and the
        display always reboots after flashing. Nothing is checked about the machine state.
        """
        if component not in OTA_COMPONENTS:
            raise ValueError(f"Unknown update component: {component}")
        if self.ota_updating:
            raise UpdateFailed("A firmware update is already running")

        # Record before the await so a concurrent call and the watchdog see it
        previous = (self._ota_start_sent_at, self._ota_grace_until)
        now = time.monotonic()
        self._ota_start_sent_at = now
        self._ota_grace_until = now + OTA_WATCHDOG_GRACE
        try:
            await self.send_message({"tp": MSG_TYPE_OTA_START, "cp": component})
        except BaseException:
            # Includes cancellation: an install that was never sent must not count as running
            self._ota_start_sent_at, self._ota_grace_until = previous
            raise
        await self.request_ota_settings()

    async def check_for_updates(self) -> None:
        """Ask the firmware to look for a new release on the current channel.

        The firmware answers with the old data at once; the result of the check
        arrives later as another res:ota-settings broadcast.
        """
        if self.ota_updating:
            raise UpdateFailed("A firmware update is already running")
        channel = self.ota_channel
        if channel is None:
            raise UpdateFailed("Update channel unknown")
        await self.send_message({"tp": MSG_TYPE_OTA_SETTINGS, "update": True, "channel": channel})

    async def set_ota_channel(self, channel: str) -> None:
        """Switch the update channel (the firmware also starts a new check)."""
        if channel not in OTA_CHANNELS:
            raise ValueError(f"Unknown update channel: {channel}")
        if self.ota_updating:
            raise UpdateFailed("A firmware update is already running")
        await self.send_message({"tp": MSG_TYPE_OTA_SETTINGS, "update": True, "channel": channel})

    @property
    def available(self) -> bool:
        # Return whether the machine is available.
        return self._available
