# GaggiMate Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/GaggiMate/ha-integration.svg)](https://github.com/GaggiMate/ha-integration/releases)

Control and monitor your GaggiMate-equipped espresso machine directly from Home Assistant. This custom integration provides comprehensive control over your machine's temperature, brewing modes, profiles, and more through native Home Assistant entities.

## Table of Contents

- [Features](#features)
- [Supported Entities](#supported-entities)
  - [Capability detection](#capability-detection)
- [Installation](#installation)
- [Configuration](#configuration)
- [Services](#services)
- [Automation Examples](#automation-examples)
- [Example Dashboard](#example-dashboard)
- [Troubleshooting](#troubleshooting)
- [Development / Running Tests](#development--running-tests)
- [Support](#support)
- [Contributing](#contributing)

## Features

- 🌡️ **Real-time Monitoring** - Current and target temperatures and pressure, heater and pump power, puck resistance and flow
- ☕ **Mode Control** - Switch between Standby, Brew, Steam, Hot Water, and Grind modes (dual-boiler machines offer Standby, Brew and Grind; see [Troubleshooting](#troubleshooting))
- 🎮 **Machine Control** - Start/stop brewing and flush cycles, confirm brews that a warning blocked, grind, tare the scale, and switch the steam heater on dual-boiler machines
- 🧹 **Cleaning** - Start and mark backflush and descaling (the start buttons are disabled by default)
- 📊 **Profile Selection** - Select brewing profiles
- ⚖️ **Scale Integration** - Weight from a connected BLE scale or hardware scale, with scale battery and the active weight source
- 🔔 **Events** - Shot saved and shot finished events, and machine events, for automations
- 🩺 **Diagnostics** - Controller state and link health, warnings, and firmware update status
- 🔄 **Automatic Entity Setup** - Entities appear as the machine reports its capabilities (see [Capability detection](#capability-detection))
- 🏠 **Native HA Integration** - Uses standard Home Assistant entity types for seamless automation
- 🧹 **Shot History Maintenance** - Built-in service to trim stored shots and keep only the newest entries

## Supported Entities

Entity names are fixed English names, so entity IDs do not depend on the Home Assistant language. Entity IDs in the examples assume the device is named `GaggiMate`; the default device name includes the host, so check the actual IDs (see [Automation Examples](#automation-examples)). **Shown when** is the condition under which the entity is created (see [Capability detection](#capability-detection)). **Enabled by default: No** means the entity is created but hidden until you enable it in its entity settings.

### Capability detection

Entities with a condition in **Shown when** depend on what the machine reports: a capability flag (for example `db` for a dual-boiler machine, `hs` for a hardware scale, `cd` for dimming) or a firmware key (for example `sys` for the system state). The machine reports these once its controller link is up, which can be a few seconds after Home Assistant starts. Such an entity is therefore created shortly after startup rather than during setup. The integration never removes an entity once it has been created: if the machine later stops reporting the capability, the entity's state becomes unknown until the capability is reported again. After a restart, an entity whose capability has not arrived yet shows as unavailable until it does.

Entities marked **Always** do not depend on the machine (for example Current Temperature, Mode and Start Brew).

**Firmware requirements:**
- Firmware that does not send a key does not get the entities that depend on it. Nothing else changes.
- Brew Confirmation Pending, Confirm Brew and Cancel Brew Confirmation depend on the firmware's brew confirmation event. Without it the sensor stays off and the two buttons stay unavailable.
- Trim Shot History reads the shot index at `/api/history/index.bin` on the machine. Firmware that answers 404 for that file falls back to the old websocket shot list. See [Services](#services).

### Sensors

| Entity | Description | Device Class | Shown when | Enabled by default |
|--------|-------------|--------------|------------|--------------------|
| Current Temperature | Live temperature reading from the machine | Temperature | Always | Yes |
| Target Temperature | Desired temperature target | Temperature | Always | Yes |
| Current Pressure | Live pressure reading from the machine | Pressure | Always | Yes |
| Target Pressure | Desired pressure target | Pressure | Always | Yes |
| Pump Flow | Current pump flow rate | - | Always | Yes |
| Heater Power | Heater duty cycle in % (0 in standby) | - | `hp` reported | Yes |
| Pump Power | Pump power in % | - | `pw` reported | Yes |
| Puck Resistance | Puck resistance in s*sqrt(bar)/mL; unknown outside 0-100 | - | `cp` and `cd` true, and `pkr` reported | No |
| Puck Flow | Flow through the puck in mL/s | - | `cp` and `cd` true | Yes |
| Target Flow | Target flow in mL/s during a process; unknown when 0 | - | `cd` true | No |
| Water Pumped | Water pumped during the current or last process in mL; appears after the first brew or process and keeps its last value | - | `cp` and `cd` true, and `wp` reported | Yes |
| Steam Temperature | Steam boiler temperature | Temperature | `db` true (dual boiler) | Yes |
| Target Steam Temperature | Steam boiler temperature target; unknown when 0 (steam heater off) | Temperature | `db` true (dual boiler) | Yes |
| Mode | Active machine mode (Standby, Brew, Steam, Hot Water, Grind); attribute `mode_id`. Dual-boiler machines report Brew for Steam/Hot Water | - | Always | Yes |
| Status | Derived machine status (Idle, Brewing, Steaming, Grinding, Pouring Water); attribute `elapsed_seconds` | - | Always | Yes |
| Process Phase | Current process phase label from the machine | - | Always | Yes |
| Selected Profile | Currently active brewing profile | - | Always | Yes |
| Target Shot Volume | Target shot volume for the selected profile | Weight | Always | Yes |
| Shot Volume Progress | Current shot volume progress during extraction | Weight | Always | Yes |
| Scale Connection | BLE scale connection state (Connected/Disconnected) | - | Always | Yes |
| Current Weight | Live weight of the active scale source (BLE or hardware scale, or flow estimate); unknown when no source is active. Attribute `source` shows which one | Weight | Always | Yes |
| Tank level | Tank level estimate in % | - | Always | Yes |
| ToF Distance | Raw distance reading of the ToF tank sensor in mm (diagnostic) | - | Always | Yes |
| Water Tank Low | Firmware water warning state (Active/Inactive) with severity attribute (diagnostic) | - | Always | Yes |
| Temperature Not Stable | Firmware temperature warning state with severity attribute (diagnostic) | - | Always | Yes |
| Steam Switch Left On | Firmware steam switch warning state with severity attribute (diagnostic) | - | Always | Yes |
| Flush Recommended | Firmware flush warning state with severity attribute (diagnostic) | - | Always | Yes |
| Scale Not Connected | Firmware scale connection warning state with severity attribute (diagnostic) | - | Always | Yes |
| Scale Battery Low | Firmware scale battery warning state with severity attribute (diagnostic) | - | Always | Yes |
| Hardware Model | Controller hardware model information (diagnostic) | - | Always | Yes |
| Display Firmware Version | Current display firmware version (diagnostic) | - | Always | Yes |
| Controller Firmware Version | Current controller firmware version (diagnostic) | - | Always | Yes |
| Display Update Available | Display firmware update status (diagnostic) | - | Always | Yes |
| Controller Update Available | Controller firmware update status (diagnostic) | - | Always | Yes |
| Latest Software Version | Latest available firmware version (diagnostic) | - | Always | Yes |
| System State | Controller state: starting, waiting, ready, updating, autotuning, mismatch, error; attributes `message` and `error_code` | Enum | `sys` reported | Yes |
| Controller Link | Link to the controller: ble, uart or none (diagnostic) | Enum | `lnk` reported | Yes |
| Controller Link RSSI | BLE signal strength in dBm; unknown unless the link is BLE (diagnostic) | Signal strength | `lnk` reported | No |
| Controller Link Latency | BLE round-trip latency in ms; unknown until measured (diagnostic) | Duration | `lat` reported | No |
| Controller Link Retransmits | Communication frames resent since boot (diagnostic) | - | `rtx` reported | No |
| Scale Battery | Battery level of the connected BLE scale in %; unknown when no scale or level unknown (diagnostic) | Battery | `sbat` reported | Yes |
| Active Scale Source | Source of the weight reading: hardware, bluetooth, estimation or inactive (diagnostic) | Enum | `scaleSource` reported | Yes |
| Backflush Due | Backflush warning state (Active/Inactive) with severity attribute (diagnostic) | - | Firmware reports the backflush warning | Yes |
| Descaling Due | Descaling warning state (Active/Inactive) with severity attribute (diagnostic) | - | Firmware reports the descaling warning | Yes |

A change of the machine's update flag triggers a refresh of the OTA settings, so the Display/Controller Update Available sensors do not go stale.

### Binary Sensors

| Entity | Description | Device Class | Shown when | Enabled by default |
|--------|-------------|--------------|------------|--------------------|
| Brew Confirmation Pending | On while the machine waits for a brew start to be confirmed (see [Brew confirmation](#brew-confirmation)); attribute `warnings` lists the warnings reported by the machine as `key`, `severity`, `severity_level` | - | Always | Yes |
| Controller Error | On when the controller reports an error code or an error/mismatch state (diagnostic) | Problem | `sys` reported | Yes |
| Update Available | On when a display or controller firmware update is available (diagnostic) | Update | `up` reported | Yes |
| Tank Level Low | On when the tank sensor reports the tank as not OK; unknown without a reading | Problem | `htl` true and `tlo` reported | Yes |
| Steam Boiler Low | On when the steam boiler level sensor reports low water; unknown without a reading | Problem | `hsl` true and `sbl` reported | Yes |

**Tank and water level entities:** the Tank level sensor (%) is the level estimate. The Water Tank Low sensor (diagnostic) is the firmware warning: on machines with a ToF tank sensor, the firmware reports it active when the level is below 20 %, and its severity setting decides whether an active warning asks for confirmation before brewing. Tank Level Low is the raw state of the tank sensor (`htl`: the machine has a tank sensor, which is a tank switch or a ToF sensor on a dual-boiler machine). A tank switch alone produces no Water Tank Low warning, so Tank Level Low is the entity to use for it. Tank Level Low and Water Tank Low can only be compared where both exist. Steam Boiler Low is the raw steam level state and has no warning counterpart.

### Brew confirmation

A warning whose severity is set to *Error* on the machine does not block brewing outright: when you press **Start Brew** (or start from the display or web UI) in brew mode, the machine does not brew but asks for confirmation. Start Brew can therefore appear to do nothing while such a warning is active. Home Assistant shows the request as **Brew Confirmation Pending** (on, with a `warnings` attribute that lists the warnings reported by the machine). Answer it with **Confirm Brew** (starts the brew, ignoring the warnings) or **Cancel Brew Confirmation**; Start Brew itself never overrides the warning.

The pending state clears when the confirmation is cancelled (from any client), when a brew process starts, when the machine mode changes away from Brew, and when the connection to the machine is lost. Confirm Brew starts the brew only if the machine is ready.

Example: notify when a brew is waiting for confirmation:

```yaml
trigger:
  - platform: state
    entity_id: binary_sensor.gaggimate_brew_confirmation_pending
    to: "on"
action:
  - service: notify.mobile_app_phone
    data:
      message: "Brew blocked: {{ state_attr('binary_sensor.gaggimate_brew_confirmation_pending', 'warnings') | default([], true) | map(attribute='key') | join(', ') }}"
```

Example: confirm the brew automatically only when the blocking warnings are scale warnings (`scaleConnected`, `scaleBattery`). Any other warning, or an empty list, leaves the confirmation to you:

```yaml
alias: "GaggiMate: Confirm brew when only the scale is missing"
trigger:
  - platform: state
    entity_id: binary_sensor.gaggimate_brew_confirmation_pending
    to: "on"
condition:
  - condition: template
    value_template: >-
      {% set keys = state_attr('binary_sensor.gaggimate_brew_confirmation_pending', 'warnings') | default([], true) | map(attribute='key') | list %}
      {{ keys | length > 0 and keys | reject('in', ['scaleConnected', 'scaleBattery']) | list | length == 0 }}
action:
  - service: button.press
    target:
      entity_id: button.gaggimate_confirm_brew
mode: single
```

**Warning:** the confirmation is the machine's safety prompt for an error-level warning (for example an empty water tank). An automation that presses Confirm Brew blindly bypasses it; only automate it when you check the warnings first, as the example above does.

### Buttons

| Entity | Description | Shown when | Enabled by default |
|--------|-------------|------------|--------------------|
| Start Brew | Begin brewing operation. With an error-level warning active the machine asks for confirmation instead (see [Brew confirmation](#brew-confirmation)) | Always | Yes |
| Stop Brew | Stop active process (brew or steam) | Always | Yes |
| Flush | Trigger flush cycle | Always | Yes |
| Flush Stop | Release a hold-to-flush (no effect during a fixed-length flush) | Always | Yes |
| Clear Process | Clear the finished process state on the machine | Always | Yes |
| Confirm Brew | Start the brew although an error-level warning is active. Available only while Brew Confirmation Pending is on | Always (available only while pending) | Yes |
| Cancel Brew Confirmation | Dismiss the pending brew confirmation. Available only while Brew Confirmation Pending is on | Always (available only while pending) | Yes |
| Tare Scale | Tare the hardware scale. The Bluetooth scale is not tared by this button | `hs` true (hardware scale) | Yes |
| Start Backflush | Select the backflush profile and start it in brew mode (runs the pump and heater). Present whether or not a backflush is due. See [Cleaning](#cleaning) | Firmware reports the backflush warning | No |
| Mark Backflush Done | Record a backflush as done: resets the backflush schedule and shot count without running the cycle | Firmware reports the backflush warning | Yes |
| Start Descaling | Select the descaling profile and start it in brew mode (runs the pump and heater). Present whether or not a descaling is due. See [Cleaning](#cleaning) | Firmware reports the descaling warning | No |
| Mark Descaling Done | Record a descaling as done: resets the descaling schedule and shot count without running the cycle | Firmware reports the descaling warning | Yes |

### Switches

| Entity | Description | Shown when | Enabled by default |
|--------|-------------|------------|--------------------|
| Machine Active | Turn the machine on (sets brew mode) or off (sets standby). Both directions raise an error while the machine is not ready | Always | Yes |
| Steam Heater | Turn the steam boiler on/off. Dual-boiler machines only. Turning on while off, or off while on, is refused in standby; turning on while already on, or off while already off, is a silent no-op. The firmware only has a toggle, so a command is sent only when the state has to change; a change made at the same moment from the web UI or display can still be reversed | `db` true (dual boiler) | Yes |
| Grind | Start or stop the grinder (on = `req:grind:activate`, off = `req:grind:deactivate`); shows whether the grinder is running. Turning on raises an error while the machine is not ready or a brew is running (the firmware would drop the start silently, or clear the brew's scale source). Turning on while already on is a silent no-op. Turning off is sent only while a grind runs and no other process is active, because the firmware's stop also ends any running brew: while another process such as a brew is active, turning Grind off does nothing and raises no error, so stop that process first and then turn Grind off | `gact` reported | Yes |
| Grind By Weight | Grind target type: on = by weight, off = by time. Changes the grind target setting that the machine and its web UI share. Depending on the firmware, grinding by weight uses a Bluetooth scale only: with just a hardware scale the grind runs for the set time, even when this is on. The switch reflects the effective target: it shows off while no scale is available, even if the stored setting is by weight | `bta` is 1 (volumetric source) | Yes |

**Warning:** Grind starts the grinder motor. Check the grinder (hopper, portafilter, cup) before you automate it. Home Assistant refuses to start Grind while a brew runs, because the firmware's start would clear the running brew's scale source.

Example: turn the steam heater off after it has been on for 30 minutes. The condition skips the call in standby, where the integration refuses the change before sending anything:

```yaml
alias: "GaggiMate: Steam heater off after 30 minutes"
trigger:
  - platform: state
    entity_id: switch.gaggimate_steam_heater
    to: "on"
    for:
      minutes: 30
condition:
  - condition: template
    value_template: "{{ states('sensor.gaggimate_mode') != 'Standby' }}"
action:
  - service: switch.turn_off
    target:
      entity_id: switch.gaggimate_steam_heater
mode: single
```

### Numbers

| Entity | Description | Shown when | Enabled by default |
|--------|-------------|------------|--------------------|
| Target Temperature Setpoint | Set desired temperature (0–160 °C, step 1) | Always | Yes |
| Grind Target Duration | Grind time in seconds (5-300, step 1). Available only while the grind target type is time (Grind By Weight off, or no scale) | `gtd` reported | Yes |
| Grind Target Weight | Grind weight in grams (5-250, step 0.5). Available only while the grind target type is weight (Grind By Weight on and a scale available) | `bta` is 1 and `gtv` reported | Yes |

The grind target numbers move the machine's target with repeated raise/lower messages (one per second or 0.5 g, 50 ms apart; the firmware has no absolute setter), so a large change takes a few seconds and can race with another client (web UI, display) changing the same target. Setting the current value sends nothing; the sequence fails with an error if the target type flips or the target stops moving. Values off the step grid are rounded to the nearest step. Calls are serialised, and each waits briefly for the machine to report the new value before it returns.

### Selects

| Entity | Description | Shown when | Enabled by default |
|--------|-------------|------------|--------------------|
| Mode | Choose machine operating mode (dual-boiler machines offer Standby, Brew and Grind only) | Always | Yes |
| Profile | Choose brewing profile | Always | Yes |

### Cleaning

Start Backflush and Start Descaling select the machine's cleaning profile and start it in brew mode. The Start buttons are created only when the firmware reports the matching warning (backflush or descaling). The firmware reports these warnings whether or not a cleaning is due, so the buttons exist on those firmware versions even when nothing is due. They are **disabled by default** because they run the pump and heater. Mark Backflush Done and Mark Descaling Done are created under the same firmware-warning rule and are enabled by default.

**Warning:** these buttons actuate the machine.

- Start switches the machine to brew mode and wakes it from standby. An unattended automation therefore heats and pumps from cold standby.
- Start selects the cleaning profile persistently: the next normal brew uses that profile until another profile is selected. This also applies after a cancelled confirmation.
- Backflush needs a blind basket in the portafilter.
- Descaling needs descaler in the water tank and the manual steps of the descaling procedure. Check the machine's instructions first.
- Success is reported when the machine accepts the start, not when the cycle has run. A start can still wait for a brew confirmation: an error-level warning turns on **Brew Confirmation Pending** instead, and the cleaning continues only after **Confirm Brew**. Check that sensor after pressing Start.
- The schedule resets when the cleaning brew starts, even if the cycle is stopped early. Mark Done is for cleaning you did without the profile; it resets the schedule immediately, whether or not any cleaning was done.

Start Backflush and Start Descaling fail with an error while the machine is not ready or a brew is running, and when the machine cannot select the cleaning profile.

### Events

Two event entities report things the machine announces once. They have no state history: each event carries a type and attributes, and you trigger automations on them.

| Entity | Description | Shown when | Enabled by default |
|--------|-------------|------------|--------------------|
| Shot Events | Shot saved and shot finished events | Always | Yes |
| Machine Events | Scale disconnected and autotune events | Always | Yes |

| Entity | Event type | Attributes |
|---|---|---|
| Shot Events | `shot_saved` | `shot_id` (integer) |
| Shot Events | `shot_finished` | `max_pressure_bar`, `avg_flow_ml_s` (floats) |
| Machine Events | `scale_disconnected` | `name`, `address` |
| Machine Events | `autotune_result` | `pid` (string) |
| Machine Events | `autotune_failed` | none |

An attribute the machine did not send, or sent in an unexpected form, is `null`.

Events that the machine emits while Home Assistant is not connected are lost. They are not replayed after a reconnect, and the entities are unavailable while the machine is.

Example: notify when a shot finishes.

```yaml
alias: "Shot finished"
trigger:
  - platform: state
    entity_id: event.gaggimate_shot_events
condition:
  - condition: template
    value_template: "{{ trigger.from_state is not none and trigger.from_state.state not in ['unavailable', 'unknown'] }}"
  - condition: template
    value_template: "{{ trigger.to_state.attributes.event_type == 'shot_finished' }}"
action:
  - service: notify.notify
    data:
      message: >
        {% set p = trigger.to_state.attributes.max_pressure_bar %}
        {% set f = trigger.to_state.attributes.avg_flow_ml_s %}
        Shot done. Max pressure {{ p | round(1) if p is not none else 'n/a' }} bar,
        average flow {{ f | round(1) if f is not none else 'n/a' }} mL/s.
```

Example: notify when a shot is saved.

```yaml
alias: "Shot saved"
trigger:
  - platform: state
    entity_id: event.gaggimate_shot_events
condition:
  - condition: template
    value_template: "{{ trigger.from_state is not none and trigger.from_state.state not in ['unavailable', 'unknown'] }}"
  - condition: template
    value_template: "{{ trigger.to_state.attributes.event_type == 'shot_saved' }}"
action:
  - service: notify.notify
    data:
      message: "Shot saved (id {{ trigger.to_state.attributes.shot_id }})."
```

The trigger watches the entity's state (the time of the last event) and the condition picks the event type, so consecutive events of the same type each fire. The first condition skips the change when the entity is first created or returns from `unavailable` after a reconnect or restart: Home Assistant then restores the last event, which would otherwise notify again. The shot finished example prints `n/a` when the machine did not send a value. Adjust the entity id to match yours.

## Installation

### HACS (Recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=gaggimate&repository=ha-integration&category=integration)

Or add the repository manually:

1. Open HACS in your Home Assistant instance
2. Click on the three dots menu (⋮) in the top right corner
3. Select **Custom repositories**
4. Add this repository URL: `https://github.com/gaggimate/ha-integration`
5. Select **Integration** as the category
6. Click **Add**
7. Search for "GaggiMate" in HACS
8. Click **Install**
9. Restart Home Assistant

### Manual Installation

1. Download the latest release from the [releases page](https://github.com/gaggimate/ha-integration/releases)
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

The integration creates the entities the machine reports (see [Capability detection](#capability-detection)). Entities that are disabled by default can be enabled in their entity settings.

## Services

### `gaggimate.trim_shot_history`

Deletes the oldest shot logs on each connected GaggiMate so that no more than `max_shots` shots remain.
When `keep_annotated: true` is set, shots with notes, ratings, bean type, or grind settings are kept and do not count toward `max_shots`; `max_shots` then applies only to the remaining shots.

**Firmware requirement:** the service reads the shot index from `http://<host>:<port>/api/history/index.bin` (the same file the web UI uses). Firmware that answers 404 for that file gets the old websocket shot list instead. If the index cannot be read or has an unknown format/version, the service fails with an error and deletes nothing on that device (other devices are still trimmed). With `keep_annotated`, every shot is checked individually for notes (one request per shot), because the index flags are not reliable; shots that cannot be checked are kept and reported as an error. Shots are deleted one by one; if any device cannot be read or any deletion fails, the service reports an error after processing all devices.

Service call:

```yaml
service: gaggimate.trim_shot_history
data:
  max_shots: 5
  keep_annotated: true
```

Daily trim at midnight keeping the 5 newest shots that are not annotated (annotated shots are kept):

```yaml
alias: "GaggiMate: Daily trim shot history"
trigger:
  - platform: time
    at: "00:00:00"
action:
  - service: gaggimate.trim_shot_history
    data:
      max_shots: 5
      keep_annotated: true
```

## Automation Examples

The examples use entity IDs that assume the device is named `GaggiMate`. With the default setup the device name includes the host, so the IDs look like `binary_sensor.gaggimate_192_168_x_x_brew_confirmation_pending`. Check the actual entity IDs in **Settings** → **Devices & Services** → **Entities** and adjust the examples to match.

### Basic Automation: Morning Warm-Up

```yaml
alias: "Start Espresso Machine in the Morning"
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

**Goal:** Remind you that your espresso machine has finished preheating. This automation sends a notification once the boiler temperature has been stable above your target for a set period, so you know it's ready to brew.

```yaml
alias: "Alert When Espresso Ready"
trigger:
  - platform: numeric_state
    entity_id: sensor.gaggimate_current_temperature
    above: 86
    for:
      minutes: 10
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
alias: "GaggiMate Turbo Preheat"
icon: mdi:fire
description: "Bring the boiler and portafilter up to a stable temp faster. Boost setpoint to 120°C until profile temp reached, hold 30s, then restore profile temp"
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
    timeout: "00:02:00"
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

After steaming, the boiler is well above brew temperature. This script switches to Brew mode, reads the profile's target temperature, and repeatedly triggers flush cycles through the group head until the boiler cools to within 10°C of the brew target. Remove the portafilter and flush to the drip tray or a container.

```yaml
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

## Example Dashboard

An example Home Assistant dashboard is available in the [`examples/dashboard`](examples/dashboard/) folder. The dashboard aims to be a full GaggiMate control panel, allowing you to operate the machine and monitor your shot. See the [dashboard README](examples/dashboard/README.md) for setup instructions.

<img src="examples/dashboard/screenshots/dashboard.png" alt="GaggiMate Dashboard" width="25%">

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

**Problem**: An entity is missing
- Entities that depend on a capability or firmware key appear once the machine reports it (see [Capability detection](#capability-detection)). Wait a few seconds after startup, then reload the integration if it still does not appear
- Entities that are disabled by default (Puck Resistance, Target Flow, the controller link RSSI, latency and retransmit sensors, Start Backflush and Start Descaling) must be enabled in their entity settings

**Problem**: "GaggiMate is not ready" error from Machine Active, Mode, Start Brew, Confirm Brew, Grind (on), Start Backflush or Start Descaling
- These commands are rejected until the controller link is up and the machine reports state "ready" (e.g. while it is starting or waiting for the controller)
- Check the System State / controller connection and try again once the machine is ready

**Problem**: Mode select does not offer Steam / Hot Water, or an automation selecting them fails
- On dual-boiler machines the firmware maps Steam and Hot Water to Brew, so the Mode select only offers Standby, Brew and Grind; automations selecting Steam or Hot Water get an invalid-option error

**Problem**: Scale weight not showing
- Ensure your BLE scale is paired with the GaggiMate (not Home Assistant)
- Check the scale connection status sensor

## Development / Running Tests

The test suite uses [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component) (PHACC), pinned in `requirements_test.txt`. PHACC requires **Python 3.14** and does not support native Windows, so run the tests on Linux or WSL. CI runs them on Ubuntu with Python 3.14 (`.github/workflows/tests.yaml`).

**Windows (WSL + uv)**

1. Install WSL with a Linux distribution (from an elevated PowerShell: `wsl --install`, then reboot).
2. Inside the distribution, install [uv](https://docs.astral.sh/uv/getting-started/installation/) and let it provide Python 3.14:
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   uv python install 3.14
   ```
3. From the repository (on the WSL filesystem, e.g. under `~`, for speed):
   ```bash
   uv venv --python 3.14 .venv
   source .venv/bin/activate
   uv pip install -r requirements_test.txt
   pytest
   ```

**Linux / macOS**

```bash
uv venv --python 3.14 .venv && source .venv/bin/activate
uv pip install -r requirements_test.txt
pytest
```

The tests run against an in-process fake GaggiMate websocket server and do not contact a real machine. Configuration is in `pytest.ini`.

## Support

- **Documentation**: [GaggiMate Docs](https://docs.gaggimate.eu/)
- **Issues**: [GitHub Issues](https://github.com/gaggimate/ha-integration/issues)
- **Main GaggiMate Project**: [jniebuhr/gaggimate](https://github.com/jniebuhr/gaggimate)
- **Shop**: [shop.gaggimate.eu](https://shop.gaggimate.eu/)

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request. For major changes, please open an issue first to discuss what you would like to change.
