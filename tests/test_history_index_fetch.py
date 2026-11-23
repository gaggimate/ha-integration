"""Characterization tests for ``GaggiMateCoordinator.async_fetch_history_index`` using a mocked aiohttp session.

End-to-end behaviour against the fake server lives in test_trim_history.py; these pin the request
details and the exception mapping.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.gaggimate.const import HISTORY_INDEX_PATH
from custom_components.gaggimate.coordinator import GaggiMateCoordinator
from custom_components.gaggimate.history import ShotIndexError

from .index_bin import build_entry, build_index

MODULE = "custom_components.gaggimate.coordinator"


class _Content:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class _Resp:
    def __init__(self, status: int = 200, content_length: int | None = None, chunks: list[bytes] | None = None) -> None:
        self.status = status
        self.content_length = content_length
        self.content = _Content(chunks or [])


class _Ctx:
    def __init__(self, resp: _Resp | None = None, error: BaseException | None = None) -> None:
        self._resp = resp
        self._error = error

    async def __aenter__(self) -> _Resp:
        if self._error is not None:
            raise self._error
        assert self._resp is not None
        return self._resp

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _coordinator(hass: HomeAssistant, ctx: _Ctx | None = None) -> tuple[GaggiMateCoordinator, MagicMock]:
    coordinator = GaggiMateCoordinator(hass, "192.0.2.7", 8080)
    session = MagicMock()
    session.closed = False
    session.get = MagicMock(return_value=ctx)
    coordinator._session = session
    return coordinator, session


def _good_index() -> bytes:
    return build_index([build_entry(i, 1000 + i) for i in range(1, 4)])


async def test_success_parses_and_request_shape(hass: HomeAssistant) -> None:
    data = _good_index()
    coordinator, session = _coordinator(hass, _Ctx(_Resp(200, len(data), [data[:10], data[10:]])))
    result = await coordinator.async_fetch_history_index()
    assert sorted(str(e["id"]) for e in result) == ["1", "2", "3"]
    session.get.assert_called_once()
    args, kwargs = session.get.call_args
    assert args == (f"http://192.0.2.7:8080{HISTORY_INDEX_PATH}",)
    assert kwargs["allow_redirects"] is False
    assert isinstance(kwargs["timeout"], aiohttp.ClientTimeout)


async def test_use_ssl_uses_https(hass: HomeAssistant) -> None:
    data = _good_index()
    coordinator, session = _coordinator(hass, _Ctx(_Resp(200, len(data), [data])))
    coordinator.use_ssl = True
    await coordinator.async_fetch_history_index()
    assert session.get.call_args.args[0].startswith("https://192.0.2.7:8080/")


async def test_no_session_raises_not_connected(hass: HomeAssistant) -> None:
    coordinator = GaggiMateCoordinator(hass, "192.0.2.7")
    with pytest.raises(UpdateFailed, match="^Not connected$"):
        await coordinator.async_fetch_history_index()


async def test_closed_session_raises_not_connected(hass: HomeAssistant) -> None:
    coordinator, session = _coordinator(hass)
    session.closed = True
    with pytest.raises(UpdateFailed, match="^Not connected$"):
        await coordinator.async_fetch_history_index()
    session.get.assert_not_called()


async def test_declared_content_length_above_cap_raises(hass: HomeAssistant) -> None:
    coordinator, _ = _coordinator(hass, _Ctx(_Resp(200, 101, [b"x"])))
    with patch(f"{MODULE}.HISTORY_INDEX_MAX_BYTES", 100), patch(f"{MODULE}.parse_shot_index") as parse:
        with pytest.raises(UpdateFailed, match="^Shot index is too large$"):
            await coordinator.async_fetch_history_index()
    parse.assert_not_called()


async def test_declared_content_length_at_cap_is_accepted(hass: HomeAssistant) -> None:
    coordinator, _ = _coordinator(hass, _Ctx(_Resp(200, 100, [b"y" * 100])))
    with patch(f"{MODULE}.HISTORY_INDEX_MAX_BYTES", 100), patch(f"{MODULE}.parse_shot_index", return_value=[]) as parse:
        assert await coordinator.async_fetch_history_index() == []
    parse.assert_called_once_with(b"y" * 100)


async def test_streamed_body_above_cap_without_content_length_raises(hass: HomeAssistant) -> None:
    coordinator, _ = _coordinator(hass, _Ctx(_Resp(200, None, [b"a" * 60, b"b" * 41])))
    with patch(f"{MODULE}.HISTORY_INDEX_MAX_BYTES", 100), patch(f"{MODULE}.parse_shot_index") as parse:
        with pytest.raises(UpdateFailed, match="^Shot index is too large$"):
            await coordinator.async_fetch_history_index()
    parse.assert_not_called()


async def test_timeout_mapped_to_update_failed(hass: HomeAssistant) -> None:
    coordinator, _ = _coordinator(hass, _Ctx(error=TimeoutError()))
    # Quirk pinned as-is: exceptions are always truthy, so the type-name fallback never applies and an
    # empty-message error yields a trailing empty detail.
    with pytest.raises(UpdateFailed, match="^Failed to fetch shot index: $") as exc:
        await coordinator.async_fetch_history_index()
    assert isinstance(exc.value.__cause__, TimeoutError)


async def test_asyncio_timeout_with_message_mapped(hass: HomeAssistant) -> None:
    coordinator, _ = _coordinator(hass, _Ctx(error=asyncio.TimeoutError("too slow")))
    with pytest.raises(UpdateFailed, match="^Failed to fetch shot index: too slow$"):
        await coordinator.async_fetch_history_index()


async def test_client_error_mapped_to_update_failed(hass: HomeAssistant) -> None:
    err = aiohttp.ClientConnectionError("connection reset")
    coordinator, _ = _coordinator(hass, _Ctx(error=err))
    with pytest.raises(UpdateFailed, match="^Failed to fetch shot index: connection reset$") as exc:
        await coordinator.async_fetch_history_index()
    assert exc.value.__cause__ is err


async def test_client_error_without_message_has_empty_detail(hass: HomeAssistant) -> None:
    coordinator, _ = _coordinator(hass, _Ctx(error=aiohttp.ClientError()))
    with pytest.raises(UpdateFailed, match="^Failed to fetch shot index: $"):
        await coordinator.async_fetch_history_index()


async def test_404_falls_back_to_legacy_list_without_reading_body(hass: HomeAssistant) -> None:
    legacy = [{"id": "a", "timestamp": 1}]
    coordinator, _ = _coordinator(hass, _Ctx(_Resp(404, 10_000_000, [b"never read"])))
    coordinator.request_history_list = AsyncMock(return_value=legacy)  # type: ignore[method-assign]
    with patch(f"{MODULE}.parse_shot_index") as parse:
        assert await coordinator.async_fetch_history_index() == legacy
    coordinator.request_history_list.assert_awaited_once_with()
    parse.assert_not_called()


async def test_404_legacy_failure_propagates(hass: HomeAssistant) -> None:
    coordinator, _ = _coordinator(hass, _Ctx(_Resp(404)))
    coordinator.request_history_list = AsyncMock(side_effect=UpdateFailed("Invalid history list response"))  # type: ignore[method-assign]
    with pytest.raises(UpdateFailed, match="^Invalid history list response$"):
        await coordinator.async_fetch_history_index()


@pytest.mark.parametrize("status", [301, 302, 307, 403, 500, 503])
async def test_non_200_not_followed_and_no_fallback(hass: HomeAssistant, status: int) -> None:
    coordinator, session = _coordinator(hass, _Ctx(_Resp(status, 5, [b"hello"])))
    coordinator.request_history_list = AsyncMock()  # type: ignore[method-assign]
    with pytest.raises(UpdateFailed, match=f"^Shot index request failed: HTTP {status}$"):
        await coordinator.async_fetch_history_index()
    assert session.get.call_args.kwargs["allow_redirects"] is False
    coordinator.request_history_list.assert_not_called()


async def test_shot_index_error_propagates_unwrapped(hass: HomeAssistant) -> None:
    coordinator, _ = _coordinator(hass, _Ctx(_Resp(200, 4, [b"junk"])))
    err = ShotIndexError("Shot index is corrupt")
    with patch(f"{MODULE}.parse_shot_index", side_effect=err):
        with pytest.raises(ShotIndexError) as exc:
            await coordinator.async_fetch_history_index()
    assert exc.value is err
    assert not isinstance(exc.value, UpdateFailed)


async def test_real_corrupt_body_raises_shot_index_error(hass: HomeAssistant) -> None:
    coordinator, _ = _coordinator(hass, _Ctx(_Resp(200, 3, [b"\x00\x01\x02"])))
    with pytest.raises(ShotIndexError):
        await coordinator.async_fetch_history_index()
