"""WebSocket coordinator for GaggiMate integration."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
import time
from typing import Any
from contextlib import suppress

import aiohttp
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    MSG_TYPE_FLUSH_START,
    MSG_TYPE_MODE_CHANGE,
    MSG_TYPE_OTA_PROGRESS,
    OTA_STATUS_TIMEOUT,
    OTA_FINISHED_STATUS_TIMEOUT,
    OTA_CHECK_TIMEOUT,
    MSG_TYPE_OTA_START,
    MSG_TYPE_OTA_SETTINGS,
    MSG_TYPE_PROCESS_ACTIVATE,
    MSG_TYPE_PROCESS_DEACTIVATE,
    MSG_TYPE_PROFILES_LIST,
    MSG_TYPE_PROFILES_SELECT,
    MSG_TYPE_STATUS,
    MSG_TYPE_TEMP_LOWER,
    MSG_TYPE_TEMP_RAISE,
    MachineMode,
    OTA_PHASE_CONTROLLER_FW,
    OTA_PHASE_DISPLAY_FS,
    OTA_PHASE_DISPLAY_FW,
    OTA_PHASE_FAILED,
    OTA_PHASE_FINISHED,
    OTA_PHASE_IDLE,
    OTA_REBOOT_GRACE,
    OTA_FINISHED_IDLE_DELAY,
    OTA_STATE_FAILED,
    OTA_STATE_FINISHED,
    OTA_STATE_IDLE,
    OTA_STATE_REBOOTING,
    OTA_STATE_UPDATING_CONTROLLER,
    OTA_STATE_UPDATING_DISPLAY,
    MSG_TYPE_HISTORY_DELETE,
    MSG_TYPE_HISTORY_LIST,
    MSG_TYPE_HISTORY_NOTES_GET,
    WS_CONNECT_TIMEOUT,
    WS_RECONNECT_DELAYS,
    WS_REQUEST_TIMEOUT,
    WS_UNAVAILABLE_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


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

        # Firmware update progress (evt:ota-progress)
        self._ota_state: str = OTA_STATE_IDLE
        self._ota_component: str | None = None  # "display" or "controller"
        self._ota_progress: int | None = None
        self._ota_timer: CALLBACK_TYPE | None = None
        self._profiles_refresh_pending = False
        # Ask the firmware for one update check after a flash: set when the machine restarts after a
        # flash, used by the first firmware answer after it is back (see _maybe_check_after_flash).
        self._check_after_flash = False
        self._check_deadline: float | None = None
        self._flash_started_here = False   # the running flash was started by Install in Home Assistant

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

        _LOGGER.warning("GaggiMate _connect() called")
        if self._session is None:
            self._session = aiohttp.ClientSession()

        await self._connect()

        # Start availability checker once
        if self._availability_check_task is None:
            self._availability_check_task = asyncio.create_task(self._check_availability())

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator cleanly."""
        self._shutting_down = True
        self._cancel_ota_timer()

        if self._availability_check_task:
            self._availability_check_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._availability_check_task
            self._availability_check_task = None

        if self._listen_task:
            self._listen_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._listen_task
            self._listen_task = None

        if self._reconnect_task:
            self._reconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None

        if self._ws and not self._ws.closed:
            await self._ws.close()
            self._ws = None

        if self._session:
            await self._session.close()
            self._session = None

    async def _connect(self) -> None:
        """Connect to WebSocket, ensuring only one connection attempt at a time."""
        _LOGGER.warning(">>> GAGGIMATE CONNECT ENTRY <<<")
        _LOGGER.warning("ENTER _CONNECT BEFORE LOCK")
        async with self._connect_lock:
            _LOGGER.warning("ENTERED CONNECT LOCK")
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

                self._ws = await self._session.ws_connect(
                    self.ws_url,
                    timeout=aiohttp.ClientTimeout(total=WS_CONNECT_TIMEOUT),
                    heartbeat=30,
                )

                _LOGGER.info("Successfully connected to GaggiMate")
                self._reconnect_attempt = 0

                # Start listener
                self._listen_task = asyncio.create_task(self._listen())

                # Prime data
                self.hass.async_create_task(self.request_ota_settings())
                self.hass.async_create_task(self.request_profiles_list())

            except Exception as err:
                _LOGGER.error("Failed to connect to GaggiMate: %s", err)

                # DO NOT trigger reconnect from inside locked context
                # just let caller handle it
                raise UpdateFailed(f"Failed to connect: {err}") from err

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
            # The connection dropped while an update was running or had just
            # finished: the machine restarts itself after flashing.
            if self._ota_state in (
                OTA_STATE_UPDATING_CONTROLLER,
                OTA_STATE_UPDATING_DISPLAY,
                OTA_STATE_FINISHED,
            ):
                self._set_ota_state(OTA_STATE_REBOOTING, self._ota_component, None)
                # The socket is gone: the stale timestamp must not make the watchdog fire
                # (and close the next socket) while the machine restarts.
                self._last_status_time = None
            if self._available:
                self._available = False
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
            msg_type = message.get("tp")

            if msg_type == MSG_TYPE_STATUS:
                self._reconnect_attempt = 0
                self._last_status_time = time.monotonic()
                
                if not self._available:
                    _LOGGER.info("GaggiMate became available")
                    self._available = True
                    if self._ota_state == OTA_STATE_REBOOTING:
                        # Back after the restart that followed a firmware update.
                        self._set_ota_state(OTA_STATE_IDLE, None, None)
                        if self._check_after_flash:
                            self._check_deadline = time.monotonic() + OTA_CHECK_TIMEOUT
                    self.async_update_listeners()
                # Firmware sends partial status frames: fast telemetry every tick and the
                # slow-changing state only when it changes (plus a full snapshot on connect).
                # Merge onto the last known status; a key sent as null clears it.
                self.async_set_updated_data({**(self.data or {}), **message})
                return

            if msg_type == MSG_TYPE_OTA_PROGRESS:
                self._handle_ota_progress(message)
                return

            if msg_type == "res:ota-settings":
                self._ota_settings = self._merge_ota_settings(message)
                self.async_update_listeners()
                await self._maybe_check_after_flash(message)
                return

            if msg_type == "res:profiles:list":
                profiles = message.get("profiles")
                if not isinstance(profiles, list):
                    # e.g. an error reply ("Update in progress"): keep what we know
                    return
                new_profiles: dict[str, str] = {}
                for profile in profiles:
                    label = profile.get("label")
                    pid = profile.get("id")
                    if label and pid:
                        new_profiles[label] = pid
                self._profiles = new_profiles
                self.async_update_listeners()
                return

            rid = message.get("rid")
            if rid and rid in self._pending_requests:
                future = self._pending_requests.pop(rid)
                if not future.done():
                    future.set_result(message)

        except json.JSONDecodeError as err:
            _LOGGER.error("Invalid WebSocket JSON: %s", err)

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

                _LOGGER.warning(
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
                    _LOGGER.error("Reconnect failed: %s", err)

        except asyncio.CancelledError:
            raise
        finally:
            self._reconnect_task = None

    def _status_timeout(self) -> float:
        """Seconds without a status frame before the machine counts as gone."""
        if self._ota_state in (OTA_STATE_UPDATING_CONTROLLER, OTA_STATE_UPDATING_DISPLAY):
            return OTA_STATUS_TIMEOUT
        if self._ota_state == OTA_STATE_FINISHED:
            return OTA_FINISHED_STATUS_TIMEOUT
        return WS_UNAVAILABLE_TIMEOUT

    async def _check_availability(self) -> None:
        """Monitor status updates and reconnect if stale."""
        while not self._shutting_down:
            try:
                await asyncio.sleep(1)

                if self._last_status_time is None:
                    continue

                elapsed = time.monotonic() - self._last_status_time

                if elapsed > self._status_timeout():
                    _LOGGER.warning(
                        "No status update for %.1f seconds - marking unavailable",
                        elapsed,
                    )
                
                    self._last_status_time = None
                
                    if self._available:
                        self._available = False
                        self.async_set_updated_data({})
                        self.async_update_listeners()
                    
                    # Closing WS causes listener to trigger reconnect
                    if self._ws and not self._ws.closed:
                        await self._ws.close()

            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.error("Availability check error: %s", err)

    def _merge_ota_settings(self, new: dict[str, Any]) -> dict[str, Any]:
        """Keep the installed versions when the firmware answers with blanks.

        Just after a restart the firmware has not yet reconnected the
        controller (blank controllerVersion) nor run its update check (blank
        latestVersion, flags false). A blank installed version means "not known
        yet", so carry the previous one over. A blank latestVersion means the
        firmware has not checked for updates: the latest version and the update
        flags are UNKNOWN (not "no update"), so they are passed on as received
        and the entities treat a blank latestVersion as unknown.
        """
        old = self._ota_settings
        merged = dict(new)
        for key in ("displayVersion", "controllerVersion"):
            if not new.get(key) and old.get(key):
                merged[key] = old[key]
        return merged

    async def _maybe_check_after_flash(self, answer: dict[str, Any]) -> None:
        """Ask the firmware for ONE update check after a flash that Install started, once the machine is back.

        Each flash restarts the machine and the firmware then knows nothing about updates for about
        30 minutes. The integration changed the firmware's state, so it brings it up to date: only
        after a completed flash that Install started (never after a flash started elsewhere, a plain
        reboot or a reconnect), once, on the first answer after the restart, with the channel the
        machine reports (nothing is sent if it reports none). No retry, no rate limit.
        """
        if not self._check_after_flash:
            return
        if self._check_deadline is not None and time.monotonic() > self._check_deadline:
            self._check_after_flash = False   # the answer came too late to be "right after the restart"
            self._check_deadline = None
            return
        self._check_after_flash = False
        self._check_deadline = None
        if answer.get("latestVersion"):
            return                            # the firmware already knows
        channel = answer.get("channel")
        if not channel:
            return
        try:
            await self.send_message({"tp": MSG_TYPE_OTA_SETTINGS, "update": True, "channel": channel})
        except UpdateFailed:
            _LOGGER.debug("Failed to request the update check after the flash")

    def _cancel_ota_timer(self) -> None:
        if self._ota_timer is not None:
            self._ota_timer()
            self._ota_timer = None

    @callback
    def _set_ota_state(
        self, state: str, component: str | None, progress: int | None
    ) -> None:
        """Change the firmware update state and notify the entities."""
        self._cancel_ota_timer()
        self._ota_state = state
        self._ota_component = component
        self._ota_progress = progress

        if state == OTA_STATE_IDLE and self._profiles_refresh_pending:
            self._profiles_refresh_pending = False
            self.hass.async_create_task(self.request_profiles_list())

        if state in (OTA_STATE_IDLE, OTA_STATE_FAILED):
            self._flash_started_here = False

        if state == OTA_STATE_REBOOTING:
            # Only a flash started by this integration gets the update check afterwards.
            self._check_after_flash = self._flash_started_here
            self._check_deadline = None
            self._ota_timer = async_call_later(
                self.hass, OTA_REBOOT_GRACE, self._ota_timer_fired
            )
        elif state == OTA_STATE_FINISHED:
            # If nothing needed flashing the machine does not restart.
            self._ota_timer = async_call_later(
                self.hass, OTA_FINISHED_IDLE_DELAY, self._ota_timer_fired
            )
        self.async_update_listeners()

    @callback
    def _ota_timer_fired(self, _now: Any) -> None:
        """Give up waiting for a restart (rebooting) or the end of a no-op update (finished)."""
        self._ota_timer = None
        if self._ota_state == OTA_STATE_REBOOTING:
            self._check_after_flash = False   # the machine never came back: no check
            self._check_deadline = None
        if self._ota_state in (OTA_STATE_REBOOTING, OTA_STATE_FINISHED):
            self._set_ota_state(OTA_STATE_IDLE, None, None)

    @callback
    def _handle_ota_progress(self, message: dict[str, Any]) -> None:
        """Handle evt:ota-progress: {"phase": 0-5, "progress": 0-100}."""
        if self._last_status_time is not None:
            self._last_status_time = time.monotonic()  # alive, though busy flashing
        phase = message.get("phase")
        raw_progress = message.get("progress")
        progress = (
            max(0, min(100, int(raw_progress)))
            if isinstance(raw_progress, (int, float))
            else None
        )
        if phase in (OTA_PHASE_DISPLAY_FW, OTA_PHASE_DISPLAY_FS):
            self._set_ota_state(OTA_STATE_UPDATING_DISPLAY, "display", progress)
        elif phase == OTA_PHASE_CONTROLLER_FW:
            self._set_ota_state(OTA_STATE_UPDATING_CONTROLLER, "controller", progress)
        elif phase == OTA_PHASE_FINISHED:
            self._set_ota_state(OTA_STATE_FINISHED, self._ota_component, 100)
        elif phase == OTA_PHASE_FAILED:
            self._set_ota_state(OTA_STATE_FAILED, self._ota_component, None)
        elif phase == OTA_PHASE_IDLE:
            self._set_ota_state(OTA_STATE_IDLE, None, None)
        else:
            _LOGGER.debug("Unknown OTA phase: %s", message)

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

    def _flash_io_blocked(self) -> bool:
        """True while the firmware flashes: newer firmware refuses profile and history requests then.

        (Per the firmware source it answers req:profiles:* and req:history* with an "Update in progress"
        error while it updates, so they are not sent.)
        """
        return self._ota_state in (
            OTA_STATE_UPDATING_CONTROLLER,
            OTA_STATE_UPDATING_DISPLAY,
            OTA_STATE_FINISHED,
        )

    async def _request(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send a request and await the matching response."""
        tp = str(message.get("tp", ""))
        if self._flash_io_blocked() and (
            tp.startswith("req:profiles:") or tp.startswith("req:history")
        ):
            raise UpdateFailed("A firmware update is in progress")
        rid = str(uuid.uuid4())
        message["rid"] = rid
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_requests[rid] = future

        try:
            await self.send_message(message)
            return await asyncio.wait_for(future, timeout=WS_REQUEST_TIMEOUT)
        except asyncio.TimeoutError as err:
            self._pending_requests.pop(rid, None)
            raise UpdateFailed("Timed out waiting for response") from err

    async def set_mode(self, mode: int) -> None:
        await self.send_message({"tp": MSG_TYPE_MODE_CHANGE, "mode": mode})

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

    async def start_brew(self) -> None:
        await self.send_message({"tp": MSG_TYPE_PROCESS_ACTIVATE})

    async def stop_brew(self) -> None:
        await self.send_message({"tp": MSG_TYPE_PROCESS_DEACTIVATE})

    async def start_flush(self) -> None:
        await self._request({"tp": MSG_TYPE_FLUSH_START})

    async def request_profiles_list(self) -> None:
        if self._flash_io_blocked():
            # Ask again once the update is over (see _set_ota_state).
            self._profiles_refresh_pending = True
            return
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

    async def start_ota(self, component: str) -> None:
        """Ask the firmware to flash one component ("display" or "controller").

        The firmware sends no reply; progress arrives as evt:ota-progress.
        """
        if component not in ("display", "controller"):
            raise ValueError(f"Unknown firmware component: {component}")
        await self.send_message({"tp": MSG_TYPE_OTA_START, "cp": component})
        self._flash_started_here = True

    async def request_history_list(self) -> list[dict[str, Any]]:
        response = await self._request({"tp": MSG_TYPE_HISTORY_LIST})
        history = response.get("history")
        if history is None or not isinstance(history, list):
            raise UpdateFailed("Invalid history list response")
        return history

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
    def ota_state(self) -> str:
        """Firmware update state (one of OTA_STATES)."""
        return self._ota_state

    @property
    def ota_component(self) -> str | None:
        """Component being updated ("display" or "controller"), if any."""
        return self._ota_component

    @property
    def ota_progress(self) -> int | None:
        """Progress of the current flashing step in percent, if known."""
        return self._ota_progress

    @property
    def ota_busy(self) -> bool:
        """True while a firmware update is running or the machine is restarting after one."""
        return self._ota_state in (
            OTA_STATE_UPDATING_CONTROLLER,
            OTA_STATE_UPDATING_DISPLAY,
            OTA_STATE_FINISHED,
            OTA_STATE_REBOOTING,
        )

    @property
    def available(self) -> bool:
        # Return whether the machine is available.
        return self._available
