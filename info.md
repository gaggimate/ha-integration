# GaggiMate Integration

Control and monitor your GaggiMate smart espresso machine upgrade system from Home Assistant.

## Quick Start

1. Install via HACS or manually
2. Restart Home Assistant
3. Add the integration via Settings → Devices & Services
4. Enter your GaggiMate's hostname or IP address

## Features

- Real-time temperature, pressure, power and flow monitoring
- Mode selection (Standby, Brew, Steam, Hot Water, Grind; dual-boiler machines offer Standby, Brew and Grind)
- Start/Stop brew, flush and grind controls, and the steam heater on dual-boiler machines
- Confirm or cancel brews that a warning blocked
- Backflush and descaling status, start and mark-done controls
- Scale weight from a BLE scale or hardware scale
- Shot and machine events for automations
- Controller state, link health and firmware update status
- Firmware update check, Stable or Nightly channel, and display and/or controller install
- Entities appear as the machine reports its capabilities
- Automatic WebSocket reconnection
- Service to trim stored shot history

See the [README](https://github.com/gaggimate/ha-integration#readme) for the full entity list and the firmware requirements.

## Support

For issues and feature requests, visit the [GitHub repository](https://github.com/gaggimate/ha-integration/issues).
