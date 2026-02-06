# GaggiMate Home Assistant Integration

A minimal custom integration for controlling and monitoring a GaggiMate‑equipped espresso machine from Home Assistant.

## Entities

**Sensors**
- Current temperature
- Target temperature
- Current mode (Standby, Brew, Steam, Hot Water, Grind)
- Selected profile
- Hardware model (diagnostic)
- Display firmware version (diagnostic)
- Controller firmware version (diagnostic)
- Latest firmware version (diagnostic)
- Display update available (diagnostic)
- Controller update available (diagnostic)
- Scale connection status (diagnostic)


**Controls / Services**
- Machine active switch
- Mode select
- Profile select
- Target temperature setpoint (number)
- Start/Stop brew buttons
- Start steam button
- Flush button

## Install via HACS

1. HACS → Integrations → ⋮ → Custom repositories
2. Add `https://github.com/jaapp/ha_gaggimate` as category **Integration**
3. Install “GaggiMate” and restart Home Assistant
