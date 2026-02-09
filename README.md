# GaggiMate Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/GaggiMate/ha-integration.svg)](https://github.com/GaggiMate/ha-integration/releases)

Control and monitor your GaggiMate-equipped espresso machine directly from Home Assistant. This custom integration provides comprehensive control over your machine's temperature, brewing modes, profiles, and more through native Home Assistant entities.

## Features

- 🌡️ **Real-time Temperature Monitoring** - Track current and target temperatures
- ☕ **Mode Control** - Switch between Standby, Brew, Steam, Hot Water, and Grind modes
- 🎮 **Full Machine Control** - Start/stop brewing, steaming, and flushing operations
- 📊 **Profile Selection** - Select brewing profiles
- ⚖️ **Scale Integration** - Monitor weight from connected BLE scales
- 🔄 **Firmware Updates** - Track firmware versions and available updates
- 🏠 **Native HA Integration** - Uses standard Home Assistant entity types for seamless automation

## Supported Entities

### Sensors
| Entity | Description | Device Class |
|--------|-------------|--------------|
| Current Temperature | Live temperature reading from the machine | Temperature |
| Target Temperature | Desired temperature target | Temperature |
| Current Mode | Active machine mode (Standby, Brew, Steam, Hot Water, Grind) | - |
| Selected Profile | Currently active brewing profile | - |
| Hardware Model | Controller hardware model information (diagnostic) | - |
| Display Firmware Version | Current display firmware version (diagnostic) | - |
| Controller Firmware Version | Current controller firmware version (diagnostic) | - |
| Latest Firmware Version | Available firmware version (diagnostic) | - |
| Display Update Available | Display firmware update status (diagnostic) | - |
| Controller Update Available | Controller firmware update status (diagnostic) | - |
| Scale Connection Status | BLE scale connection state | - |
| Current Weight | Live weight reading from scale | Weight |

### Controls
| Entity | Type | Description |
|--------|------|-------------|
| Machine Active | Switch | Turn the machine on/off |
| Mode Select | Select | Choose machine operating mode |
| Profile Select | Select | Choose brewing profile |
| Target Temperature | Number | Set desired temperature (adjustable range) |
| Start Brew | Button | Begin brewing operation |
| Stop Brew | Button | End brewing operation |
| Start Steam | Button | Begin steaming operation |
| Flush | Button | Trigger flush cycle |

## Installation

### HACS (Recommended)

1. Open HACS in your Home Assistant instance
2. Click on the three dots menu (⋮) in the top right corner
3. Select **Custom repositories**
4. Add this repository URL: `https://github.com/DevNullGamer/ha-integration`
5. Select **Integration** as the category
6. Click **Add**
7. Search for "GaggiMate" in HACS
8. Click **Install**
9. Restart Home Assistant

### Manual Installation

1. Download the latest release from the [releases page](https://github.com/DevNullGamer/ha-integration/releases)
2. Extract the `custom_components/gaggimate` folder
3. Copy it to your Home Assistant `custom_components` directory:
   ```
   <config_directory>/custom_components/gaggimate/
   ```
4. Restart Home Assistant

## Configuration

### Adding the Integration

1. Navigate to **Settings** → **Devices & Services**
2. Click **+ Add Integration**
3. Search for "GaggiMate"
4. Enter your GaggiMate machine's details:
   - **Host/IP Address**: The IP address or hostname of your GaggiMate controller
   - **Port**: WebSocket port (default: 80)
5. Click **Submit**

The integration will automatically discover all available entities and create them in Home Assistant.

## Automations Examples

### Basic Automation: Morning Warm-Up

```yaml
automation:
  - alias: "Start Espresso Machine in the Morning"
    trigger:
      - platform: time
        at: "07:00:00"
    condition:
      - condition: time
        weekday:
          - mon
          - tue
          - wed
          - thu
          - fri
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.gaggimate_machine_active
      - service: select.select_option
        target:
          entity_id: select.gaggimate_mode
        data:
          option: "Brew"
```

### Temperature-Based Alert

```yaml
automation:
  - alias: "Alert When Espresso Ready"
    trigger:
      - platform: numeric_state
        entity_id: sensor.gaggimate_current_temperature
        above: 86
        for:
          hours: 0
          minutes: 10
          seconds: 0
    condition:
      - condition: state
        entity_id: select.gaggimate_mode
        state: "Brew"
    action:
      - service: notify.mobile_app
        data:
          message: "☕ GaggiMate is ready!"
```

## Troubleshooting

### Connection Issues

**Problem**: Integration can't connect to the machine
- Verify the IP address/hostname is correct
- Ensure the machine is powered on and connected to your network
- Check that there are no firewall rules blocking the connection
- Verify your Home Assistant instance can reach the machine's network

**Problem**: Integration keeps disconnecting
- Consider setting a static IP for your GaggiMate
- Check for WiFi signal strength issues

### Entity Issues

**Problem**: Entities not updating
- Check the integration logs: Settings → System → Logs
- Try reloading the integration: Settings → Devices & Services → GaggiMate → ⋮ → Reload
- Verify the WebSocket connection is stable

**Problem**: Scale weight not showing
- Ensure your BLE scale is paired with the GaggiMate (not Home Assistant)
- Check the scale connection status sensor

## Support

- **Documentation**: [GaggiMate Docs](https://docs.gaggimate.eu/)
- **Issues**: [GitHub Issues](https://github.com/DevNullGamer/ha-integration/issues)
- **Main GaggiMate Project**: [jniebuhr/gaggimate](https://github.com/jniebuhr/gaggimate)
- **Shop**: [shop.gaggimate.eu](https://shop.gaggimate.eu/)

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request. For major changes, please open an issue first to discuss what you would like to change.
