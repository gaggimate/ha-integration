#!/usr/bin/env python3
"""Record real GaggiMate websocket frames into a test fixture.

Usage:
    python scripts/record_frames.py <host> [--port 80] [--name NAME] [--seconds 5]
                                    [--out-dir tests/fixtures/frames]

Connects to ws://<host>:<port>/ws, collects ``evt:status`` frames for a few seconds, and
writes ``<out-dir>/<name>.json`` as ``{"state": ..., "telemetry": ...}``. ``state`` is the
slow-state frame (has ``sys``/``warn``), ``telemetry`` is the newest fast frame (has ``ct``).
Read-only: it only listens and never sends commands.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiohttp


async def record(host: str, port: int, seconds: float) -> tuple[dict, dict]:
    state: dict | None = None
    telemetry: dict | None = None
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://{host}:{port}/ws", timeout=aiohttp.ClientTimeout(total=10)) as ws:
            try:
                async with asyncio.timeout(seconds):
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        frame = json.loads(msg.data)
                        if frame.get("tp") != "evt:status":
                            continue
                        if "sys" in frame or "warn" in frame:
                            state = frame
                        elif "ct" in frame:
                            telemetry = frame
            except TimeoutError:
                pass
    if state is None or telemetry is None:
        raise SystemExit(
            f"Did not see both a state and a telemetry frame in {seconds}s "
            f"(state={'yes' if state else 'no'}, telemetry={'yes' if telemetry else 'no'}); try --seconds 15"
        )
    return state, telemetry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--name", default="recorded")
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "frames",
    )
    args = parser.parse_args()

    state, telemetry = asyncio.run(record(args.host, args.port, args.seconds))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    target = args.out_dir / f"{args.name}.json"
    target.write_text(json.dumps({"state": state, "telemetry": telemetry}, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
