"""Builder for synthetic ``/api/history/index.bin`` files (layout from the firmware source).

Header 32 B ``<IHHII16x`` (magic "SIDX", version, entrySize, entryCount, nextId);
entries 128 B ``<IIIHBB32s48sHHH26x``.
"""
from __future__ import annotations

import struct

MAGIC = 0x58444953
HEADER_FMT = "<IHHII16x"
ENTRY_FMT = "<IIIHBB32s48sHHH26x"
FLAG_COMPLETED = 0x01
FLAG_DELETED = 0x02
FLAG_HAS_NOTES = 0x04


def build_entry(
    shot_id: int,
    timestamp: int,
    *,
    duration: int = 30000,
    volume: int = 36,
    rating: int = 0,
    flags: int = FLAG_COMPLETED,
    profile_id: str = "default",
    profile_name: str = "Default",
    avg_temp: int = 930,
    max_pressure: int = 90,
    avg_flow: int = 12,
) -> bytes:
    return struct.pack(
        ENTRY_FMT,
        shot_id,
        timestamp,
        duration,
        volume,
        rating,
        flags,
        profile_id.encode()[:32],
        profile_name.encode()[:48],
        avg_temp,
        max_pressure,
        avg_flow,
    )


def build_index(entries: list[bytes], *, version: int = 1, next_id: int | None = None) -> bytes:
    entry_size = struct.calcsize(ENTRY_FMT)
    header = struct.pack(HEADER_FMT, MAGIC, version, entry_size, len(entries), next_id or len(entries) + 1)
    return header + b"".join(entries)
