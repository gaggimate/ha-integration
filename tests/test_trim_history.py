"""Step 3 tests: shot index parsing and the trim_shot_history service."""
from __future__ import annotations

import struct

import pytest
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gaggimate.const import DOMAIN, HISTORY_INDEX_MAX_BYTES
from custom_components.gaggimate.history import (
    ShotIndexError,
    async_trim_shot_history,
    parse_shot_index,
)

from .conftest import SetupIntegration, wait_until
from .fake_server import FakeGaggiMate
from .index_bin import FLAG_COMPLETED, FLAG_DELETED, FLAG_HAS_NOTES, build_entry, build_index


def _entries(n: int, **kw):
    """n entries with ids 1..n, ascending timestamps."""
    return [build_entry(i, 1000 + i, **kw) for i in range(1, n + 1)]


def _deleted_ids(server: FakeGaggiMate) -> list[str]:
    return [r["id"] for r in server.requests_of("req:history:delete")]


async def _trim(hass: HomeAssistant, max_shots: int, keep_annotated: bool | None = None) -> None:
    data: dict = {"max_shots": max_shots}
    if keep_annotated is not None:
        data["keep_annotated"] = keep_annotated
    await hass.services.async_call(DOMAIN, "trim_shot_history", data, blocking=True)


# -- parse_shot_index -------------------------------------------------------------------


def test_parse_empty() -> None:
    assert parse_shot_index(build_index([])) == []


def test_parse_basic_fields() -> None:
    out = parse_shot_index(build_index([build_entry(5, 777, duration=1234, rating=4, flags=FLAG_COMPLETED)]))
    assert len(out) == 1
    e = out[0]
    assert (e["id"], e["timestamp"], e["duration"], e["rating"], e["flags"]) == (5, 777, 1234, 4, FLAG_COMPLETED)
    assert e["maybe_annotated"] is True


def test_parse_skips_deleted_flag() -> None:
    data = build_index(
        [build_entry(1, 10), build_entry(2, 20, flags=FLAG_COMPLETED | FLAG_DELETED), build_entry(3, 30)]
    )
    assert [e["id"] for e in parse_shot_index(data)] == [1, 3]


def test_parse_deleted_marker_removes_earlier_duplicate() -> None:
    data = build_index([build_entry(1, 10), build_entry(1, 10, flags=FLAG_DELETED)])
    assert parse_shot_index(data) == []


def test_parse_duplicate_ids_latest_wins() -> None:
    data = build_index([build_entry(1, 10, duration=1), build_entry(2, 20), build_entry(1, 10, duration=99)])
    out = parse_shot_index(data)
    assert sorted(e["id"] for e in out) == [1, 2]
    assert next(e for e in out if e["id"] == 1)["duration"] == 99


def test_parse_maybe_annotated_informational() -> None:
    out = parse_shot_index(
        build_index(
            [
                build_entry(1, 10, flags=0),
                build_entry(2, 20, flags=FLAG_HAS_NOTES),
                build_entry(3, 30, flags=0, rating=3),
            ]
        )
    )
    assert [e["maybe_annotated"] for e in out] == [False, True, True]


def test_parse_trailing_data_tolerated() -> None:
    assert len(parse_shot_index(build_index(_entries(2)) + b"\x00" * 10)) == 2


@pytest.mark.parametrize(
    "data,match",
    [
        (b"", "too small"),
        (b"\x00" * 31, "too small"),
        (b"\x00" * 160, "magic"),
    ],
)
def test_parse_corrupt_basic(data: bytes, match: str) -> None:
    with pytest.raises(ShotIndexError, match=match):
        parse_shot_index(data)


def test_parse_wrong_version() -> None:
    with pytest.raises(ShotIndexError, match="version"):
        parse_shot_index(build_index(_entries(1), version=2))


def test_parse_wrong_entry_size() -> None:
    data = bytearray(build_index(_entries(1)))
    struct.pack_into("<H", data, 6, 64)
    with pytest.raises(ShotIndexError, match="entry size"):
        parse_shot_index(bytes(data))


def test_parse_truncated() -> None:
    with pytest.raises(ShotIndexError, match="truncated"):
        parse_shot_index(build_index(_entries(3))[:-1])


def test_parse_count_larger_than_data() -> None:
    data = bytearray(build_index(_entries(2)))
    struct.pack_into("<I", data, 8, 50)
    with pytest.raises(ShotIndexError, match="truncated"):
        parse_shot_index(bytes(data))


# -- service end-to-end -----------------------------------------------------------------


async def test_empty_index(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    assert hass.services.has_service(DOMAIN, "trim_shot_history")
    fake_gaggimate.index_bin = build_index([])
    await _trim(hass, 3)
    assert fake_gaggimate.index_requests == 1
    assert _deleted_ids(fake_gaggimate) == []


async def test_single_entry_noop(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.index_bin = build_index(_entries(1))
    await _trim(hass, 1)
    assert _deleted_ids(fake_gaggimate) == []


async def test_max_shots_ge_count_noop(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.index_bin = build_index(_entries(5))
    await _trim(hass, 5)
    await _trim(hass, 100)
    assert _deleted_ids(fake_gaggimate) == []


async def test_deletes_oldest_with_string_ids_mixed_deleted(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    entries = [
        build_entry(1, 100),
        build_entry(2, 200, flags=FLAG_COMPLETED | FLAG_DELETED),
        build_entry(3, 300),
        build_entry(4, 400),
        build_entry(5, 500),
    ]
    fake_gaggimate.index_bin = build_index(entries)
    await _trim(hass, 2)
    ids = _deleted_ids(fake_gaggimate)
    assert ids == ["1", "3"]
    assert all(isinstance(i, str) for i in ids)
    assert fake_gaggimate.requests_of("req:history:notes:get") == []


async def test_oldest_by_timestamp_not_file_order(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.index_bin = build_index([build_entry(1, 900), build_entry(2, 100), build_entry(3, 500)])
    await _trim(hass, 1)
    assert _deleted_ids(fake_gaggimate) == ["2", "3"]


async def test_keep_annotated_false_ignores_notes(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.index_bin = build_index(_entries(3, rating=5, flags=FLAG_COMPLETED | FLAG_HAS_NOTES))
    fake_gaggimate.notes = {"1": {"notes": "great"}}
    await _trim(hass, 1)
    assert _deleted_ids(fake_gaggimate) == ["1", "2"]
    assert fake_gaggimate.requests_of("req:history:notes:get") == []


async def test_keep_annotated_true_checks_every_entry(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    entries = [
        build_entry(1, 100, flags=0, rating=0),  # notes only, flags unreliable -> keep
        build_entry(2, 200, flags=FLAG_HAS_NOTES),  # flag set but empty notes -> deletable
        build_entry(3, 300, flags=FLAG_COMPLETED),
        build_entry(4, 400, flags=FLAG_COMPLETED),
        build_entry(5, 500, flags=FLAG_COMPLETED),
    ]
    fake_gaggimate.index_bin = build_index(entries)
    fake_gaggimate.notes = {"1": {"notes": "sour", "rating": 0}, "2": {}}
    await _trim(hass, 2, True)
    assert sorted(r["id"] for r in fake_gaggimate.requests_of("req:history:notes:get")) == ["1", "2", "3", "4", "5"]
    # candidates (non-annotated) 2,3,4,5 -> keep newest 2 -> delete 2,3; shot 1 kept
    assert _deleted_ids(fake_gaggimate) == ["2", "3"]


async def test_keep_annotated_zero_value_fields_not_annotation(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.index_bin = build_index(_entries(3))
    fake_gaggimate.notes = {"1": {"rating": 0, "notes": "", "beanType": "", "grindSetting": ""}}
    await _trim(hass, 1, True)
    assert _deleted_ids(fake_gaggimate) == ["1", "2"]


async def test_keep_annotated_flag_zero_rating_zero_notes_kept(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.index_bin = build_index([build_entry(1, 100, flags=0, rating=0), build_entry(2, 200)])
    fake_gaggimate.notes = {"1": {"notes": "keep me"}}
    await _trim(hass, 1, True)
    assert _deleted_ids(fake_gaggimate) == []


async def test_keep_annotated_flag_zero_notes_kept_among_many(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    entries = [build_entry(1, 100, flags=0, rating=0)] + [build_entry(i, 100 * i) for i in (2, 3, 4)]
    fake_gaggimate.index_bin = build_index(entries)
    fake_gaggimate.notes = {"1": {"notes": "keep me"}}
    await _trim(hass, 1, True)
    assert "1" not in _deleted_ids(fake_gaggimate)
    assert _deleted_ids(fake_gaggimate) == ["2", "3"]


async def test_keep_annotated_other_annotation_fields(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.index_bin = build_index(_entries(4))
    fake_gaggimate.notes = {"1": {"beanType": "Ethiopia"}, "2": {"grindSetting": "12"}, "3": {"rating": 4}}
    await _trim(hass, 1, True)
    assert _deleted_ids(fake_gaggimate) == []


async def test_notes_failure_keeps_shot_and_reports(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.index_bin = build_index(_entries(4))
    fake_gaggimate.notes_errors = {"1": "boom"}
    with pytest.raises(HomeAssistantError, match="could not be checked"):
        await _trim(hass, 1, True)
    # shot 1 kept; candidates 2,3,4 -> delete 2,3
    assert _deleted_ids(fake_gaggimate) == ["2", "3"]


# -- corrupt / failure modes -------------------------------------------------------------


def _corrupt_cases() -> dict[str, bytes]:
    good = build_index(_entries(5))
    wrong_size = bytearray(good)
    struct.pack_into("<H", wrong_size, 6, 100)
    big_count = bytearray(good)
    struct.pack_into("<I", big_count, 8, 999)
    bad_magic = bytearray(good)
    bad_magic[0] ^= 0xFF
    return {
        "bad_magic": bytes(bad_magic),
        "truncated": good[:-50],
        "short": good[:10],
        "wrong_version": build_index(_entries(5), version=9),
        "wrong_entry_size": bytes(wrong_size),
        "count_too_big": bytes(big_count),
    }


@pytest.mark.parametrize("case", list(_corrupt_cases()))
async def test_corrupt_index_aborts_without_delete(
    case: str, hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.index_bin = _corrupt_cases()[case]
    with pytest.raises(HomeAssistantError, match="(?i)shot index"):
        await _trim(hass, 1)
    assert _deleted_ids(fake_gaggimate) == []
    assert fake_gaggimate.requests_of("req:history:list") == []


async def test_404_falls_back_to_legacy_list(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.index_bin = None
    fake_gaggimate.legacy_history = [
        {"id": "20240101-0001", "timestamp": 100},
        {"id": "20240101-0002", "timestamp": 200},
        {"id": "20240101-0003", "timestamp": 300},
    ]
    await _trim(hass, 1)
    assert len(fake_gaggimate.requests_of("req:history:list")) == 1
    assert _deleted_ids(fake_gaggimate) == ["20240101-0001", "20240101-0002"]


async def test_404_legacy_without_history_key_aborts(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.index_bin = None
    fake_gaggimate.legacy_history = None
    with pytest.raises(HomeAssistantError):
        await _trim(hass, 1)
    assert _deleted_ids(fake_gaggimate) == []


@pytest.mark.parametrize("status", [500, 503])
async def test_http_error_aborts_without_delete(
    status: int, hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.index_bin = build_index(_entries(5))
    fake_gaggimate.index_status = status
    with pytest.raises(HomeAssistantError, match=str(status)):
        await _trim(hass, 1)
    assert _deleted_ids(fake_gaggimate) == []
    assert fake_gaggimate.requests_of("req:history:list") == []


async def test_redirect_not_followed(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.index_bin = build_index(_entries(5))
    fake_gaggimate.index_redirect = True
    with pytest.raises(HomeAssistantError, match="302"):
        await _trim(hass, 1)
    assert fake_gaggimate.redirect_followed == 0
    assert _deleted_ids(fake_gaggimate) == []


async def test_oversized_body_aborts(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    good = build_index(_entries(5))
    fake_gaggimate.index_bin = good + b"\x00" * (HISTORY_INDEX_MAX_BYTES + 1)
    with pytest.raises(HomeAssistantError, match="(?i)too large"):
        await _trim(hass, 1)
    assert _deleted_ids(fake_gaggimate) == []


async def test_connection_refused_aborts(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    coordinator = await setup_integration()
    fake_gaggimate.index_bin = build_index(_entries(5))
    coordinator.port = 1  # nothing listens here
    with pytest.raises(HomeAssistantError):
        await _trim(hass, 1)
    assert _deleted_ids(fake_gaggimate) == []


async def test_per_shot_delete_error_continues_and_reports(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.index_bin = build_index(_entries(5))
    fake_gaggimate.history_delete_errors = {"2": "Delete failed"}
    with pytest.raises(HomeAssistantError, match="shot 2") as exc:
        await _trim(hass, 1)
    assert "Delete failed" in str(exc.value)
    assert _deleted_ids(fake_gaggimate) == ["1", "2", "3", "4"]


# -- multiple devices / no devices ------------------------------------------------------


async def test_two_entries_one_failing_other_still_trimmed(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    second = FakeGaggiMate()
    await second.start()
    try:
        entry2 = MockConfigEntry(
            domain=DOMAIN,
            title="GaggiMate Two",
            unique_id="second",
            data={CONF_HOST: second.host, CONF_PORT: second.port},
        )
        entry2.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry2.entry_id)
        coord2 = hass.data[DOMAIN][entry2.entry_id]
        await wait_until(lambda: coord2.available)

        fake_gaggimate.index_bin = _corrupt_cases()["bad_magic"]
        second.index_bin = build_index(_entries(4))
        with pytest.raises(HomeAssistantError, match="(?i)shot index"):
            await _trim(hass, 2)
        assert _deleted_ids(fake_gaggimate) == []
        assert _deleted_ids(second) == ["1", "2"]
        await hass.config_entries.async_unload(entry2.entry_id)
        await hass.async_block_till_done()
    finally:
        await second.stop()


async def test_no_coordinators_raises() -> None:
    with pytest.raises(HomeAssistantError, match="No GaggiMate"):
        await async_trim_shot_history([], 5, False)


async def test_invalid_max_shots_rejected(
    hass: HomeAssistant, fake_gaggimate: FakeGaggiMate, setup_integration: SetupIntegration
) -> None:
    await setup_integration()
    fake_gaggimate.index_bin = build_index(_entries(3))
    with pytest.raises(Exception):
        await _trim(hass, 0)
    assert _deleted_ids(fake_gaggimate) == []
