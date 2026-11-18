# Test fixtures

All files in `frames/` are **synthetic**: they are written by hand from the key names in the
GaggiMate firmware (`WebSocketHandler.cpp`, `publishState()` / `publishTelemetry()` and the
`evt:*` / `res:*` handlers), not recorded from a real machine.

* `single_boiler_idle`, `dual_boiler_idle`, `dual_boiler_brewing`, `waiting_controller`,
  `tof_tank`, `scale_connected`, `warnings_full`: `{"state": <slow evt:status frame>,
  "telemetry": <fast evt:status frame>}`. The fake server sends `state` then `telemetry` when a
  client connects.
* `events.json`: non-status `evt:*` frames by name.
* `profiles_list.json`, `ota_settings.json`: `{"response": <frame>}` answers to the matching requests.

To replace synthetic frames with real ones, run a recording against a real machine:

    python scripts/record_frames.py <host> [--port 80] [--name my_machine] [--seconds 5]

The recorder writes `frames/<name>.json` in the same `state` / `telemetry` format (without the
`synthetic` flag). The recorder needs `aiohttp` only.
