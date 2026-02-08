"""WebSocket coordinator for GaggiMate integration."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant, callback
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
            update_interval=None,  # We get push updates via WebSocket
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

    @property
    def ws_url(self) -> str:
        """Return WebSocket URL."""
        protocol = "wss" if self.use_ssl else "ws"
        return f"{protocol}://{self.host}:{self.port}/ws"

    async def async_start(self) -> None:
        """Start the WebSocket connection."""
        if self._session is None:
            self._session = aiohttp.ClientSession()

        await self._connect()

        # Start availability checker
        if self._availability_check_task is None:
            self._availability_check_task = asyncio.create_task(self._check_availability())

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator."""
        if self._availability_check_task:
            self._availability_check_task.cancel()
            self._availability_check_task = None

        if self._listen_task:
            self._listen_task.cancel()
            self._listen_task = None

        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None

        if self._ws and not self._ws.closed:
            await self._ws.close()
            self._ws = None

        if self._session:
            await self._session.close()
            self._session = None

    async def _connect(self) -> None:
        """Connect to WebSocket."""
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

            # Start listening for messages
            if self._listen_task:
                self._listen_task.cancel()
            self._listen_task = asyncio.create_task(self._listen())

            # Prime cached data in background to avoid blocking setup
            self.hass.async_create_task(self.request_ota_settings())
            self.hass.async_create_task(self.request_profiles_list())

        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("Failed to connect to GaggiMate: %s", err)
            await self._schedule_reconnect()
            raise UpdateFailed(f"Failed to connect: {err}") from err

    async def _listen(self) -> None:
        """Listen for WebSocket messages."""
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
            _LOGGER.debug("Listen task cancelled")
            raise
        except Exception as err:
            _LOGGER.error("Error in WebSocket listen loop: %s", err)
        finally:
            await self._schedule_reconnect()

    async def _handle_message(self, data: str) -> None:
        """Handle incoming WebSocket message."""
        try:
            message = json.loads(data)
            msg_type = message.get("tp")

            if msg_type == MSG_TYPE_STATUS:
                self._last_status_time = datetime.now()
                # Update coordinator data with status message
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
            _LOGGER.error("Failed to decode WebSocket message: %s", err)

    async def _schedule_reconnect(self) -> None:
        """Schedule a reconnection attempt."""
        if self._reconnect_task and not self._reconnect_task.done():
            return

        delay_index = min(self._reconnect_attempt, len(WS_RECONNECT_DELAYS) - 1)
        delay = WS_RECONNECT_DELAYS[delay_index]

        _LOGGER.info("Scheduling reconnect in %s seconds (attempt %s)", delay, self._reconnect_attempt + 1)

        self._reconnect_task = asyncio.create_task(self._reconnect_after_delay(delay))

    async def _reconnect_after_delay(self, delay: int) -> None:
        """Reconnect after delay."""
        try:
            await asyncio.sleep(delay)
            self._reconnect_attempt += 1
            await self._connect()
        except asyncio.CancelledError:
            _LOGGER.debug("Reconnect task cancelled")
            raise
        except Exception as err:
            _LOGGER.error("Error during reconnect: %s", err)

    async def _check_availability(self) -> None:
        """Periodically check if device is available based on last status time."""
        while True:
            try:
                await asyncio.sleep(1)

                if self._last_status_time is None:
                    continue

                time_since_status = (datetime.now() - self._last_status_time).total_seconds()

                if time_since_status > WS_UNAVAILABLE_TIMEOUT:
                    _LOGGER.warning(
                        "No status update received for %s seconds, reconnecting WebSocket",
                        round(time_since_status, 1),
                    )
                    self.async_set_updated_data(None)
                    self._last_status_time = None
                    if self._ws and not self._ws.closed:
                        await self._ws.close()

            except asyncio.CancelledError:
                _LOGGER.debug("Availability check task cancelled")
                raise
            except Exception as err:
                _LOGGER.error("Error checking availability: %s", err)

    async def send_message(self, message: dict[str, Any]) -> None:
        """Send a message to the WebSocket."""
        if self._ws is None or self._ws.closed:
            raise UpdateFailed("WebSocket not connected")

        try:
            await self._ws.send_json(message)
            _LOGGER.debug("Sent message: %s", message)
        except Exception as err:
            _LOGGER.error("Failed to send message: %s", err)
            raise UpdateFailed(f"Failed to send message: {err}") from err

    async def _request(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send a request message and await a response with matching rid."""
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
        """Set machine mode."""
        await self.send_message({"tp": MSG_TYPE_MODE_CHANGE, "mode": mode})

    async def set_temperature(self, temperature: float) -> None:
        """Set target temperature."""
        if self.data is None:
            raise UpdateFailed("No status data available to adjust temperature")

        current_target = self.data.get("tt")
        if current_target is None:
            raise UpdateFailed("Target temperature is unavailable")

        delta = int(round(temperature - float(current_target)))
        if delta == 0:
            return

        msg_type = MSG_TYPE_TEMP_RAISE if delta > 0 else MSG_TYPE_TEMP_LOWER
        steps = abs(delta)

        for _ in range(steps):
            await self.send_message({"tp": msg_type})
            await asyncio.sleep(0.05)

    async def start_brew(self) -> None:
        """Start brewing."""
        await self.send_message({"tp": MSG_TYPE_PROCESS_ACTIVATE})

    async def stop_brew(self) -> None:
        """Stop brewing."""
        await self.send_message({"tp": MSG_TYPE_PROCESS_DEACTIVATE})

    async def start_steam(self) -> None:
        """Start steam process."""
        await self.set_mode(MachineMode.STEAM)
        await asyncio.sleep(0.1)
        await self.send_message({"tp": MSG_TYPE_PROCESS_ACTIVATE})

    async def start_flush(self) -> None:
        """Start a flush cycle."""
        await self._request({"tp": MSG_TYPE_FLUSH_START})

    async def request_profiles_list(self) -> None:
        """Request profile list from device."""
        try:
            await self._request({"tp": MSG_TYPE_PROFILES_LIST})
        except UpdateFailed as err:
            _LOGGER.debug("Failed to request profiles list: %s", err)

    async def select_profile(self, profile_id: str) -> None:
        """Select a profile by ID."""
        await self._request({"tp": MSG_TYPE_PROFILES_SELECT, "id": profile_id})

    async def request_ota_settings(self) -> None:
        """Request OTA settings info."""
        try:
            await self.send_message({"tp": MSG_TYPE_OTA_SETTINGS})
        except UpdateFailed as err:
            _LOGGER.debug("Failed to request OTA settings: %s", err)

    @property
    def profiles(self) -> dict[str, str]:
        """Return profile label->id mapping."""
        return self._profiles

    @property
    def ota_settings(self) -> dict[str, Any]:
        """Return OTA settings payload."""
        return self._ota_settings
