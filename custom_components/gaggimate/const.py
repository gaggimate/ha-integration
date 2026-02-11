"""Constants for the GaggiMate integration."""
from __future__ import annotations

from enum import IntEnum

DOMAIN = "gaggimate"

# Default configuration values
DEFAULT_PORT = 80
DEFAULT_SCAN_INTERVAL = 1  # seconds

# WebSocket configuration
WS_CONNECT_TIMEOUT = 10  # seconds
WS_RECONNECT_DELAYS = [1, 2, 4, 8, 16, 30]  # seconds
WS_REQUEST_TIMEOUT = 30  # seconds
WS_UNAVAILABLE_TIMEOUT = 10  # seconds - mark unavailable if no status for 5 seconds

# Message types
MSG_TYPE_STATUS = "evt:status"
MSG_TYPE_MODE_CHANGE = "req:change-mode"
MSG_TYPE_PROCESS_ACTIVATE = "req:process:activate"
MSG_TYPE_PROCESS_DEACTIVATE = "req:process:deactivate"
MSG_TYPE_PROCESS_CLEAR = "req:process:clear"
MSG_TYPE_TEMP_RAISE = "req:raise-temp"
MSG_TYPE_TEMP_LOWER = "req:lower-temp"
MSG_TYPE_FLUSH_START = "req:flush:start"
MSG_TYPE_PROFILES_LIST = "req:profiles:list"
MSG_TYPE_PROFILES_SELECT = "req:profiles:select"
MSG_TYPE_OTA_SETTINGS = "req:ota-settings"

# Machine modes
class MachineMode(IntEnum):
    """Machine mode enumeration matching GaggiMate firmware."""
    STANDBY = 0
    BREW = 1
    STEAM = 2
    WATER = 3
    GRIND = 4


MODE_NAMES = {
    MachineMode.STANDBY: "Standby",
    MachineMode.BREW: "Brew",
    MachineMode.STEAM: "Steam",
    MachineMode.WATER: "Hot Water",
    MachineMode.GRIND: "Grind",
}

MODE_ICONS = {
    MachineMode.STANDBY: "mdi:power-standby",
    MachineMode.BREW: "mdi:coffee",
    MachineMode.STEAM: "mdi:cloud",
    MachineMode.WATER: "mdi:water",
    MachineMode.GRIND: "mdi:grain",
}

# Entity unique ID suffixes
UNIQUE_ID_CURRENT_TEMP = "current_temperature"
UNIQUE_ID_TARGET_TEMP = "target_temperature"
UNIQUE_ID_MODE = "mode"
UNIQUE_ID_POWER = "power"
UNIQUE_ID_MODE_SELECT = "mode_select"
UNIQUE_ID_BREW_START = "brew_start"
UNIQUE_ID_BREW_STOP = "brew_stop"
UNIQUE_ID_TARGET_TEMP_SETPOINT = "target_temperature_setpoint"
UNIQUE_ID_PROFILE_SELECT = "profile_select"
UNIQUE_ID_SELECTED_PROFILE = "selected_profile"
UNIQUE_ID_FLUSH = "flush"
UNIQUE_ID_STEAM_START = "steam_start"
UNIQUE_ID_HW_MODEL = "hardware_model"
UNIQUE_ID_SW_DISPLAY = "software_display_version"
UNIQUE_ID_SW_CONTROLLER = "software_controller_version"
UNIQUE_ID_UPDATE_DISPLAY = "display_software_update"
UNIQUE_ID_UPDATE_CONTROLLER = "controller_software_update"
UNIQUE_ID_LATEST_VERSION = "latest_software_version"
UNIQUE_ID_SCALE_CONNECTED = "scale_connection_status"
UNIQUE_ID_CURRENT_WEIGHT = "current_weight"
UNIQUE_ID_CURRENT_PRESSURE = "current_pressure"
UNIQUE_ID_TARGET_PRESSURE = "target_pressure"
UNIQUE_ID_PUMP_FLOW = "pump_flow"
UNIQUE_ID_TARGET_VOLUME = "target_shot_volume"
UNIQUE_ID_SHOT_VOLUME_PROGRESS = "shot_volume_progress"
UNIQUE_ID_STATUS = "status"
UNIQUE_ID_PROCESS_PHASE = "process_phase"
