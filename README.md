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
| Current Pressure | Live pressure reading from the machine | Pressure |
| Target Pressure | Desired pressure target | Pressure |
| Pump Flow | Current pump flow rate | - |
| Mode | Active machine mode (Standby, Brew, Steam, Hot Water, Grind) | - |
| Status | Derived machine status (Idle, Brewing, Steaming, Grinding, Pouring Water) | - |
| Process Phase | Current process phase label from the machine (e.g. infusion, brew, finished) | - |
| Selected Profile | Currently active brewing profile | - |
| Target Shot Weight | Target shot volume for the selected profile | Weight |
| Shot Weight Progress | Current shot volume progress during extraction | Weight |
| Scale Connection | BLE scale connection state (Connected/Disconnected) | - |
| Current Weight | Live weight reading from scale | Weight |
| Hardware Model | Controller hardware model information (diagnostic) | - |
| Display Firmware Version | Current display firmware version (diagnostic) | - |
| Controller Firmware Version | Current controller firmware version (diagnostic) | - |
| Display Update Available | Display firmware update status (diagnostic) | - |
| Controller Update Available | Controller firmware update status (diagnostic) | - |
| Latest Software Version | Latest available firmware version (diagnostic) | - |

### Controls
| Entity | Type | Description |
|--------|------|-------------|
| Machine Active | Switch | Turn the machine on/off |
| Mode | Select | Choose machine operating mode |
| Profile | Select | Choose brewing profile |
| Target Temperature Setpoint | Number | Set desired temperature (0–160 °C) |
| Start Brew | Button | Begin brewing operation |
| Stop Brew | Button | Stop active process (brew or steam) |
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

### Turbo Preheat: Faster Boiler & Portafilter Warm-Up

Immediately raises the target temperature to 120°C, waits for the boiler to reach the original profile temperature, then holds at the boosted setpoint for an additional 30 seconds before restoring the profile temperature. This pushes extra heat into the boiler and portafilter so everything reaches brewing temperature faster.

```yaml
script:
  gaggimate_turbo_preheat:
    alias: "GaggiMate Turbo Preheat"
    icon: mdi:fire
    description: "Bring the boiler and portafilter up to a _stable_ temp faster. Boost setpoint to 120°C until profile temp reached, hold 30s, then restore profile temp"
    sequence:
      - service: select.select_option
        target:
          entity_id: select.gaggimate_mode
        data:
          option: "Brew"
      - wait_template: "{{ states('sensor.gaggimate_target_temperature') | float > 60 }}"
        timeout: "00:00:15"
        continue_on_timeout: false
      - variables:
          original_temp: "{{ states('sensor.gaggimate_target_temperature') | float }}"
      - service: number.set_value
        target:
          entity_id: number.gaggimate_target_temperature_setpoint
        data:
          value: 120
      - wait_template: "{{ states('sensor.gaggimate_current_temperature') | float >= original_temp }}"
        timeout: "00:05:00"
        continue_on_timeout: true
      - delay:
          seconds: 30
      - service: number.set_value
        target:
          entity_id: number.gaggimate_target_temperature_setpoint
        data:
          value: "{{ original_temp }}"
    mode: single
```

### Cool Down Machine

Switches to Hot Water mode with a 25°C target and dispenses water through the steam wand until the boiler temperature drops below 30°C, then restores the original settings. Make sure to open the steam valve and place a container underneath the wand before running this script.

If the pressure rises above 5 bar for 10 seconds after the initial release (indicating the steam wand has been closed), the script aborts, restores the original temperature, and switches to Standby.

```yaml
script:
  gaggimate_cooldown:
    alias: "GaggiMate Cool Down Machine"
    icon: mdi:snowflake
    description: "Cool the machine down by dispensing hot water until temperature is below 30°C. Aborts if steam wand is closed (pressure > 5 bar for 10s)."
    sequence:
      - service: select.select_option
        target:
          entity_id: select.gaggimate_mode
        data:
          option: "Hot Water"
      - wait_template: "{{ states('sensor.gaggimate_mode') == 'Hot Water' }}"
        timeout: "00:00:10"
        continue_on_timeout: false
      - variables:
          original_water_temp: "{{ states('sensor.gaggimate_target_temperature') | float }}"
      - service: number.set_value
        target:
          entity_id: number.gaggimate_target_temperature_setpoint
        data:
          value: 25
      - service: button.press
        target:
          entity_id: button.gaggimate_start_brew
      - delay:
          seconds: 5
      - wait_template: >-
          {{ states('sensor.gaggimate_current_temperature') | float < 30 or
             states('sensor.gaggimate_current_pressure') | float > 5 }}
        timeout: "00:10:00"
        continue_on_timeout: true
      - if:
          - condition: numeric_state
            entity_id: sensor.gaggimate_current_pressure
            above: 5
        then:
          - delay:
              seconds: 10
          - if:
              - condition: numeric_state
                entity_id: sensor.gaggimate_current_pressure
                above: 5
            then:
              - service: button.press
                target:
                  entity_id: button.gaggimate_stop_brew
              - service: number.set_value
                target:
                  entity_id: number.gaggimate_target_temperature_setpoint
                data:
                  value: "{{ original_water_temp }}"
              - service: select.select_option
                target:
                  entity_id: select.gaggimate_mode
                data:
                  option: "Standby"
              - stop: "Aborted: steam wand appears closed (pressure > 5 bar)"
      - service: button.press
        target:
          entity_id: button.gaggimate_stop_brew
      - service: number.set_value
        target:
          entity_id: number.gaggimate_target_temperature_setpoint
        data:
          value: "{{ original_water_temp }}"
      - service: select.select_option
        target:
          entity_id: select.gaggimate_mode
        data:
          option: "Standby"
    mode: single
```

### Cooldown to Brew Temp

After steaming, the boiler is well above brew temperature. This script switches to Brew mode, reads the profile's target temperature, and repeatedly triggers flush cycles through the group head until the boiler cools to within 10°C of the brew target. No steam wand or container needed — just leave the portafilter in or use a blind basket.

```yaml
script:
  gaggimate_cooling_flush:
    alias: "GaggiMate Cooldown to Brew Temp"
    icon: mdi:coffee-to-go
    description: "Cool the boiler from steam temp back to brew temp by repeatedly flushing through the group head until brew temperature is reached."
    sequence:
      - service: select.select_option
        target:
          entity_id: select.gaggimate_mode
        data:
          option: "Brew"
      - wait_template: "{{ states('sensor.gaggimate_mode') == 'Brew' }}"
        timeout: "00:00:10"
        continue_on_timeout: false
      - wait_template: "{{ states('sensor.gaggimate_target_temperature') | float > 60 }}"
        timeout: "00:00:15"
        continue_on_timeout: false
      - variables:
          target_brew_temp: "{{ states('sensor.gaggimate_target_temperature') | float }}"
      - repeat:
          sequence:
            - service: button.press
              target:
                entity_id: button.gaggimate_flush
            - delay:
                seconds: 3
            - wait_template: "{{ states('sensor.gaggimate_status') == 'Idle' }}"
              timeout: "00:00:30"
              continue_on_timeout: true
          until:
            - condition: template
              value_template: >-
                {{ states('sensor.gaggimate_current_temperature') | float <= target_brew_temp + 10
                   or repeat.index >= 10 }}
    mode: single
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
