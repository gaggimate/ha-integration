"""Shot history helpers: binary index parsing and the trim_shot_history logic."""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError

if TYPE_CHECKING:
    from .coordinator import GaggiMateCoordinator

_LOGGER = logging.getLogger(__name__)

# Layout of /api/history/index.bin (firmware shot_log_format.h, little endian, packed).
# Header 32 B: magic, version, entrySize, entryCount, nextId, 16 reserved bytes.
# Entry 128 B: id, timestamp, duration, volume, rating, flags, profileId[32],
# profileName[48], avgTemp, maxPressure, avgFlow, 26 reserved bytes.
INDEX_MAGIC = 0x58444953  # "SIDX"
INDEX_VERSION = 1
INDEX_HEADER_FMT = "<IHHII16x"
INDEX_ENTRY_FMT = "<IIIHBB32s48sHHH26x"
INDEX_HEADER_SIZE = struct.calcsize(INDEX_HEADER_FMT)  # 32
INDEX_ENTRY_SIZE = struct.calcsize(INDEX_ENTRY_FMT)  # 128

FLAG_COMPLETED = 0x01
FLAG_DELETED = 0x02
FLAG_HAS_NOTES = 0x04

_ANNOTATION_FIELDS = ("rating", "beanType", "grindSetting", "notes")


class ShotIndexError(HomeAssistantError):
    """The shot index could not be read or is not in a format we understand."""


def parse_shot_index(data: bytes) -> list[dict[str, Any]]:
    """Parse index.bin into a list of entries (deleted entries are skipped).

    Raises ShotIndexError on anything unexpected (short file, bad magic, unknown
    version or entry size, truncation). The result must never be trusted for
    deletions after an error, so callers must not catch and continue.
    """
    if len(data) < INDEX_HEADER_SIZE:
        raise ShotIndexError(f"Shot index too small ({len(data)} bytes)")

    magic, version, entry_size, count, _next_id = struct.unpack_from(INDEX_HEADER_FMT, data, 0)
    if magic != INDEX_MAGIC:
        raise ShotIndexError(f"Shot index has invalid magic 0x{magic:08x}")
    if version != INDEX_VERSION:
        raise ShotIndexError(f"Unsupported shot index version {version} (expected {INDEX_VERSION})")
    if entry_size != INDEX_ENTRY_SIZE:
        raise ShotIndexError(f"Unsupported shot index entry size {entry_size} (expected {INDEX_ENTRY_SIZE})")

    expected = INDEX_HEADER_SIZE + count * INDEX_ENTRY_SIZE
    if len(data) < expected:
        raise ShotIndexError(f"Shot index truncated: {len(data)} bytes, expected {expected}")

    entries: dict[int, dict[str, Any]] = {}
    for i in range(count):
        (
            shot_id,
            timestamp,
            duration,
            _volume,
            rating,
            flags,
            _profile_id,
            _profile_name,
            _avg_temp,
            _max_pressure,
            _avg_flow,
        ) = struct.unpack_from(INDEX_ENTRY_FMT, data, INDEX_HEADER_SIZE + i * INDEX_ENTRY_SIZE)
        if flags & FLAG_DELETED:
            entries.pop(shot_id, None)
            continue
        entries[shot_id] = {
            "id": shot_id,
            "timestamp": timestamp,
            "duration": duration,
            "rating": rating,
            "flags": flags,
            # Informational only: the firmware sets the notes flag unreliably (e.g. not for
            # notes without a rating), so annotation is always decided via notes:get
            "maybe_annotated": bool(flags & FLAG_HAS_NOTES) or rating > 0,
        }
    return list(entries.values())


# Shot ids are monotonic and a shot recorded with an unset RTC has a tiny timestamp, so it
# sorts oldest (same ordering as the original service).
def _sort_key(item: dict[str, Any]) -> tuple[int, int]:
    def _int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return _int(item.get("timestamp")), _int(item.get("id"))


def _has_annotation(notes: Any) -> bool:
    if not isinstance(notes, dict) or not notes:
        return False
    for key in _ANNOTATION_FIELDS:
        if notes.get(key) in (None, "", 0, 0.0, False):
            continue
        return True
    return False


async def _trim_device(
    coordinator: GaggiMateCoordinator, max_shots: int, keep_annotated: bool
) -> list[str]:
    """Trim one device. Returns failure descriptions; raises if the history is unreadable.

    Reading and parsing happen completely before the first delete, so a read or
    parse error can never be followed by a delete.
    """
    failures: list[str] = []
    history = await coordinator.async_fetch_history_index()
    sorted_history = sorted(history, key=_sort_key)

    if keep_annotated:
        candidates: list[dict[str, Any]] = []
        annotated_count = 0
        unchecked = 0
        for item in sorted_history:
            shot_id = item.get("id")
            if shot_id is None:
                continue  # cannot be deleted anyway
            try:
                notes = await coordinator.get_history_notes(shot_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Cannot determine annotation status for shot %s on %s: %s",
                    shot_id,
                    coordinator.host,
                    err,
                )
                annotated_count += 1  # unknown: keep it
                unchecked += 1
                continue
            if _has_annotation(notes):
                annotated_count += 1
            else:
                candidates.append(item)
            await asyncio.sleep(0)

        if unchecked:
            failures.append(f"{coordinator.host}: {unchecked} shots could not be checked for notes and were kept")
        if annotated_count:
            _LOGGER.info("Preserved %s annotated shots on %s", annotated_count, coordinator.host)
    else:
        candidates = [item for item in sorted_history if item.get("id") is not None]

    if len(candidates) <= max_shots:
        _LOGGER.info(
            "Shot history trim skipped for %s: %s entries <= max_shots=%s",
            coordinator.host,
            len(candidates),
            max_shots,
        )
        return failures

    to_delete = candidates[: len(candidates) - max_shots]
    deleted = 0
    for idx, item in enumerate(to_delete, start=1):
        shot_id = item["id"]
        try:
            await coordinator.delete_history_item(shot_id)
            deleted += 1
        except Exception as err:  # noqa: BLE001 - keep going, report at the end
            failures.append(f"{coordinator.host}: shot {shot_id}: {err}")
        # Yield to event loop between deletions and avoid hammering device
        await asyncio.sleep(0.1 if idx % 10 == 0 else 0)

    msg = "Trimmed shot history for %s, kept %s newest, deleted %s older entries" % (
        coordinator.host,
        max_shots,
        deleted,
    )
    if failures:
        _LOGGER.warning("%s; failed deletions: %s", msg, "; ".join(failures))
    else:
        _LOGGER.info(msg)
    return failures


async def async_trim_shot_history(
    coordinators: list[GaggiMateCoordinator], max_shots: int, keep_annotated: bool
) -> None:
    """Trim every device independently; raise at the end if any device had a problem."""
    if not isinstance(max_shots, int) or max_shots <= 0:
        raise ValueError("max_shots must be a positive integer")
    if not coordinators:
        raise HomeAssistantError("No GaggiMate devices are configured")

    problems: list[str] = []
    for coordinator in coordinators:
        try:
            problems.extend(await _trim_device(coordinator, max_shots, keep_annotated))
        except Exception as err:  # noqa: BLE001 - one device must not abort the others
            _LOGGER.error("Shot history trim failed for %s: %s", coordinator.host, err)
            problems.append(f"{coordinator.host}: {err}")

    if problems:
        raise HomeAssistantError("Shot history trim problems: " + "; ".join(problems))
