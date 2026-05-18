"""WebSocket coordinator for GaggiMate integration."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any
from contextlib import suppress

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    MSG_TYPE_FLUSH_START,
    MSG_TYPE_MODE_CHANGE,
    MSG_TYPE_OTA_SETTINGS,
    MSG_TYPE_PROCESS_ACTIVATE,
    MSG_TYPE_PROCESS_DEACTIVATE,
    MSG_TYPE_PROFILES_LIST,
    MSG_TYPE_PROFILES_SELECT,
    MSG_TYPE_STATUS,
    MSG_TYPE_TEMP_LOWER,
    MSG_TYPE_TEMP_RAISE,
    MachineMode,
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
        self._last_status_time: datetime | None = None
        self._availability_check_task: asyncio.Task | None = None
        self._listen_task: asyncio.Task | None = None
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._profiles: dict[str, str] = {}
        self._ota_settings: dict[str, Any] = {}

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
                raise UpdateFailed(f"Failed to connect: {err}") from errr

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
                self._last_status_time = datetime.now()
                self.async_set_updated_data(message)
                return

            if msg_type == "res:ota-settings":
                self._ota_settings = message
                self.async_update_listeners()
                return

            if msg_type == "res:profiles:list":
                profiles = message.get("profiles", [])
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

    async def _check_availability(self) -> None:
        """Monitor status updates and reconnect if stale."""
        while not self._shutting_down:
            try:
                await asyncio.sleep(1)

                if self._last_status_time is None:
                    continue

                elapsed = (datetime.now() - self._last_status_time).total_seconds()

                if elapsed > WS_UNAVAILABLE_TIMEOUT:
                    _LOGGER.warning("No status update for %.1f seconds — closing WebSocket", elapsed)
                    self._last_status_time = None

                    # Closing WS causes listener to trigger reconnect
                    if self._ws and not self._ws.closed:
                        await self._ws.close()

            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.error("Availability check error: %s", err)

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
