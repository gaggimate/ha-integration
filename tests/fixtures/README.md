# Test fixtures

All files in `frames/` are **synthetic**: they are written by hand from the key names in the
GaggiMate firmware (`WebSocketHandler.cpp`, `publishState()` / `publishTelemetry()` and the
`evt:*` / `res:*` handlers), not recorded from a real machine.

* `single_boiler_idle`, `dual_boiler_idle`, `dual_boiler_brewing`, `waiting_controller`,
  `tof_tank`, `scale_connected`, `warnings_full`: `{"state": <slow evt:status frame>,
  "telemetry": <fast evt:status frame>}`. The fake server sends `state` then `telemetry` when a
  client connects.
* Tank and steam-level fixtures, all synthetic (each has a `description` naming the base fixture):
  `single_boiler_tof` (single boiler with a ToF tank: `htl` false, `tlo` null, `wl` 64),
  `tank_switch` (`htl` true, `tlo` true, `tofc` false, `wl` 100),
  `steam_level` (dual boiler with `hsl` true, `sbl` false, `htl` false, `tlo` null).
  `tof_tank` is unchanged: it is kept as found (`htl` true with `tlo` null) and is not a realistic machine.
* `events.json`: non-status `evt:*` frames by name.
* `profiles_list.json`, `ota_settings.json`: `{"response": <frame>}` answers to the matching requests.

To replace synthetic frames with real ones, run a recording against a real machine:

    python scripts/record_frames.py <host> [--port 80] [--name my_machine] [--seconds 5]

The recorder writes `frames/<name>.json` in the same `state` / `telemetry` format (without the
`synthetic` flag). The recorder needs `aiohttp` only.
