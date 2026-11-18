"""In-process fake GaggiMate websocket server for tests.

Replays frames from ``tests/fixtures/frames/*.json`` and records every request
the integration sends. It only ever listens on 127.0.0.1 (random port).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

FRAMES_DIR = Path(__file__).parent / "fixtures" / "frames"


def load_json(name: str) -> dict[str, Any]:
    """Load a fixture file (without the .json suffix)."""
    return json.loads((FRAMES_DIR / f"{name}.json").read_text(encoding="utf-8"))


class FakeGaggiMate:
    """Fake firmware websocket endpoint.

    * on connect: sends the full slow-state frame, then a telemetry frame
      (what the firmware does for a new client);
    * while ``keepalive`` is true: sends a bare ``{"tp": "evt:status"}`` every
      ``keepalive_interval`` seconds so the integration's availability timer is
      satisfied without overwriting values pushed by a test;
    * records every received JSON message in ``requests``;
    * answers the request types that have a ``rid`` response in the firmware.
    """

    def __init__(self, frames: str = "single_boiler_idle") -> None:
        data = load_json(frames)
        self.state: dict[str, Any] = data["state"]
        self.telemetry: dict[str, Any] = data["telemetry"]
        self.profiles: list[dict[str, Any]] = load_json("profiles_list")["response"]["profiles"]
        self.ota_settings: dict[str, Any] = load_json("ota_settings")["response"]
        self.keepalive = True
        # When set, `req:profiles:list` is answered with this error (and no `profiles`).
        self.profiles_list_error: str | None = None
        self.keepalive_interval = 0.2
        self.send_initial = True
        # Legacy `req:history:list` answer; None emulates current firmware (no `history` key).
        self.legacy_history: list[dict[str, Any]] | None = None
        self.history_delete_errors: dict[str, str] = {}
        self.notes: dict[str, dict[str, Any]] = {}
        self.index_bin: bytes | None = None  # served at /api/history/index.bin, 404 if None
        # Non-200 status for the index (e.g. 500/503); index_redirect answers 302 to /elsewhere.
        self.index_status: int | None = None
        self.index_redirect = False
        self.index_requests = 0
        self.redirect_followed = 0
        # shot id (str) -> error text for req:history:notes:get
        self.notes_errors: dict[str, str] = {}
        self.requests: list[dict[str, Any]] = []
        self.connections: list[web.WebSocketResponse] = []
        self.connect_count = 0
        self._server: TestServer | None = None
        self._keepalive_task: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/ws", self._ws_handler)
        app.router.add_get("/api/history/index.bin", self._index_handler)
        app.router.add_get("/elsewhere", self._elsewhere_handler)
        self._server = TestServer(app, host="127.0.0.1")
        await self._server.start_server()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def stop(self) -> None:
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            self._keepalive_task = None
        await self.drop_connections()
        if self._server:
            await self._server.close()
            self._server = None

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.port

    # -- server side handlers ---------------------------------------------
    async def _elsewhere_handler(self, request: web.Request) -> web.Response:
        self.redirect_followed += 1
        return web.Response(body=self.index_bin or b"", content_type="application/octet-stream")

    async def _index_handler(self, request: web.Request) -> web.Response:
        self.index_requests += 1
        if self.index_redirect:
            raise web.HTTPFound("/elsewhere")
        if self.index_status is not None:
            return web.Response(status=self.index_status)
        if self.index_bin is None:
            return web.Response(status=404)
        return web.Response(body=self.index_bin, content_type="application/octet-stream")

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(self.keepalive_interval)
            if self.keepalive:
                await self.push({"tp": "evt:status"})

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections.append(ws)
        self.connect_count += 1
        if self.send_initial:
            await ws.send_json(self.state)
            await ws.send_json(self.telemetry)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await self._handle_request(ws, json.loads(msg.data))
            elif msg.type == WSMsgType.ERROR:
                break
        if ws in self.connections:
            self.connections.remove(ws)
        return ws

    async def _respond(self, ws: web.WebSocketResponse, request: dict, tp: str, **fields: Any) -> None:
        resp = {"tp": tp, "rid": request.get("rid"), **fields}
        await ws.send_json(resp)

    async def _handle_request(self, ws: web.WebSocketResponse, req: dict[str, Any]) -> None:
        self.requests.append(req)
        tp = req.get("tp", "")
        if tp == "req:profiles:list":
            if self.profiles_list_error:
                await self._respond(ws, req, "res:profiles:list", error=self.profiles_list_error)
            else:
                await self._respond(ws, req, "res:profiles:list", profiles=self.profiles)
        elif tp == "req:ota-settings":
            await ws.send_json(self.ota_settings)
        elif tp == "req:profiles:select":
            if any(p["id"] == req.get("id") for p in self.profiles):
                await self._respond(ws, req, "res:profiles:select")
            else:
                await self._respond(ws, req, "res:profiles:select", error="Profile not found")
        elif tp in ("req:flush:start", "req:flush:stop"):
            await self._respond(ws, req, "res:" + tp[4:], success=True)
        elif tp == "req:history:list":
            if self.legacy_history is None:
                await self._respond(ws, req, "res:history:list")
            else:
                await self._respond(ws, req, "res:history:list", history=self.legacy_history)
        elif tp == "req:history:delete":
            err = self.history_delete_errors.get(str(req.get("id")))
            if err:
                await self._respond(ws, req, "res:history:delete", error=err)
            else:
                await self._respond(ws, req, "res:history:delete")
        elif tp == "req:history:notes:get":
            nerr = self.notes_errors.get(str(req.get("id")))
            if nerr:
                await self._respond(ws, req, "res:history:notes:get", error=nerr)
            else:
                await self._respond(ws, req, "res:history:notes:get", notes=self.notes.get(str(req.get("id")), {}))
        # everything else is fire-and-forget in the firmware: no reply

    # -- test helpers ------------------------------------------------------
    async def push(self, frame: dict[str, Any]) -> None:
        """Broadcast a frame to every connected client."""
        for ws in list(self.connections):
            if not ws.closed:
                await ws.send_json(frame)

    async def push_raw(self, text: str) -> None:
        """Broadcast a raw text frame (e.g. invalid JSON)."""
        for ws in list(self.connections):
            if not ws.closed:
                await ws.send_str(text)

    async def push_state(self, **changes: Any) -> None:
        """Send a slow-state frame with ``changes`` applied (and remember it for reconnects)."""
        self.state = {**self.state, **changes}
        await self.push(self.state)

    async def push_telemetry(self, **changes: Any) -> None:
        self.telemetry = {**self.telemetry, **changes}
        await self.push(self.telemetry)

    async def drop_connections(self) -> None:
        """Close every client connection from the server side."""
        for ws in list(self.connections):
            await ws.close()
        self.connections.clear()

    def requests_of(self, tp: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r.get("tp") == tp]

    def clear_requests(self) -> None:
        self.requests.clear()

    async def wait_for_request(self, tp: str, count: int = 1, timeout: float = 5.0) -> list[dict[str, Any]]:
        """Wait until at least ``count`` requests of type ``tp`` were received."""
        async with asyncio.timeout(timeout):
            while len(self.requests_of(tp)) < count:
                await asyncio.sleep(0.01)
        return self.requests_of(tp)
