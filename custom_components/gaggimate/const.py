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
GRIND_TARGET_MESSAGE_DELAY = 0.05  # seconds between raise/lower-grind-target messages
GRIND_TARGET_TIMEOUT = 60  # seconds - overall cap for one set_grind_target call (worst case ~25 s)
GRIND_TARGET_SETTLE = 1.5  # seconds - wait for the reported target to catch up (frames come every ~0.5 s) before the next call
GRIND_TARGET_STALL = 3.0  # seconds - stop stepping when the reported target has not moved for this long
# Firmware limits of the grind target (BREW_MIN/MAX_DURATION_MS, BREW_MIN/MAX_VOLUMETRIC)
GRIND_DURATION_MIN_S = 5
GRIND_DURATION_MAX_S = 300
GRIND_WEIGHT_MIN_G = 5.0
GRIND_WEIGHT_MAX_G = 250.0
STEAM_TOGGLE_SETTLE = 3.0  # seconds - a sent steam toggle counts as applied until `sa` confirms it
HISTORY_INDEX_PATH = "/api/history/index.bin"
HISTORY_INDEX_MAX_BYTES = 2 * 1024 * 1024  # size cap for the shot index download
WS_UNAVAILABLE_TIMEOUT = 15  # seconds - mark unavailable if no status for 15 seconds
OTA_REFRESH_DELAY = 2  # seconds - debounce for re-reading OTA settings after `up` changes

# Message types
MSG_TYPE_STATUS = "evt:status"
MSG_TYPE_EVENT_PREFIX = "evt:"
MSG_TYPE_MODE_CHANGE = "req:change-mode"
MSG_TYPE_PROCESS_ACTIVATE = "req:process:activate"
MSG_TYPE_PROCESS_DEACTIVATE = "req:process:deactivate"
MSG_TYPE_PROCESS_CLEAR = "req:process:clear"
MSG_TYPE_BREW_CONFIRM_CANCEL = "req:brew:confirm:cancel"
# Events: an error-level warning blocked a brew start / the confirmation was dismissed
MSG_TYPE_EVENT_BREW_CONFIRM = "evt:brew:confirm"
MSG_TYPE_EVENT_BREW_CONFIRM_CANCEL = "evt:brew:confirm:cancel"
MSG_TYPE_TEMP_RAISE = "req:raise-temp"
MSG_TYPE_TEMP_LOWER = "req:lower-temp"
MSG_TYPE_FLUSH_START = "req:flush:start"
MSG_TYPE_FLUSH_STOP = "req:flush:stop"
MSG_TYPE_SCALE_TARE = "req:scale:tare"
MSG_TYPE_STEAM_TOGGLE = "req:steam:toggle"
MSG_TYPE_GRIND_ACTIVATE = "req:grind:activate"
MSG_TYPE_GRIND_DEACTIVATE = "req:grind:deactivate"
MSG_TYPE_CHANGE_GRIND_TARGET = "req:change-grind-target"
MSG_TYPE_GRIND_TARGET_RAISE = "req:raise-grind-target"
MSG_TYPE_GRIND_TARGET_LOWER = "req:lower-grind-target"
MSG_TYPE_PROFILES_LIST = "req:profiles:list"
MSG_TYPE_PROFILES_SELECT = "req:profiles:select"
MSG_TYPE_OTA_SETTINGS = "req:ota-settings"
MSG_TYPE_OTA_SETTINGS_RESPONSE = "res:ota-settings"
MSG_TYPE_PROFILES_LIST_RESPONSE = "res:profiles:list"
MSG_TYPE_HISTORY_LIST = "req:history:list"
MSG_TYPE_HISTORY_DELETE = "req:history:delete"
MSG_TYPE_HISTORY_NOTES_GET = "req:history:notes:get"
# Cleaning: the firmware answers the requests with res:cleaning:<task>:<action> {success}
MSG_TYPE_CLEANING_BACKFLUSH_START = "req:cleaning:backflush:start"
MSG_TYPE_CLEANING_DESCALING_START = "req:cleaning:descaling:start"
MSG_TYPE_CLEANING_BACKFLUSH_COMPLETE = "req:cleaning:backflush:complete"
MSG_TYPE_CLEANING_DESCALING_COMPLETE = "req:cleaning:descaling:complete"
CLEANING_START_MESSAGES = {
    "backflush": MSG_TYPE_CLEANING_BACKFLUSH_START,
    "descaling": MSG_TYPE_CLEANING_DESCALING_START,
}
CLEANING_COMPLETE_MESSAGES = {
    "backflush": MSG_TYPE_CLEANING_BACKFLUSH_COMPLETE,
    "descaling": MSG_TYPE_CLEANING_DESCALING_COMPLETE,
}

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
UNIQUE_ID_WATER_LEVEL = "water_level"
UNIQUE_ID_TOF_DISTANCE = "tof_distance"
UNIQUE_ID_WARNING_WATER = "warning_water"
UNIQUE_ID_WARNING_TEMPERATURE = "warning_temperature"
UNIQUE_ID_WARNING_SWITCH = "warning_switch"
UNIQUE_ID_WARNING_FLUSH = "warning_flush"
UNIQUE_ID_WARNING_SCALE_CONNECTED = "warning_scale_connected"
UNIQUE_ID_WARNING_SCALE_BATTERY = "warning_scale_battery"
UNIQUE_ID_SYSTEM_STATE = "system_state"
UNIQUE_ID_CONTROLLER_ERROR = "controller_error"
UNIQUE_ID_LINK_TYPE = "link_type"
UNIQUE_ID_LINK_RSSI = "link_rssi"
UNIQUE_ID_LINK_LATENCY = "link_latency"
UNIQUE_ID_LINK_RETRANSMITS = "link_retransmits"
UNIQUE_ID_UPDATE_AVAILABLE = "update_available"
UNIQUE_ID_SCALE_BATTERY = "scale_battery"
UNIQUE_ID_SCALE_SOURCE = "scale_source"
UNIQUE_ID_WARNING_BACKFLUSH = "warning_backflush"
UNIQUE_ID_WARNING_DESCALING = "warning_descaling"
UNIQUE_ID_HEATER_POWER = "heater_power"
UNIQUE_ID_PUMP_POWER = "pump_power"
UNIQUE_ID_PUCK_RESISTANCE = "puck_resistance"
UNIQUE_ID_PUCK_FLOW = "puck_flow"
UNIQUE_ID_TARGET_FLOW = "target_flow"
UNIQUE_ID_WATER_PUMPED = "water_pumped"
UNIQUE_ID_STEAM_TEMPERATURE = "steam_temperature"
UNIQUE_ID_TARGET_STEAM_TEMPERATURE = "target_steam_temperature"
UNIQUE_ID_TANK_LEVEL_LOW = "tank_level_low"
UNIQUE_ID_STEAM_BOILER_LOW = "steam_boiler_low"
UNIQUE_ID_TARE_SCALE = "tare_scale"
UNIQUE_ID_FLUSH_STOP = "flush_stop"
UNIQUE_ID_PROCESS_CLEAR = "process_clear"
UNIQUE_ID_STEAM_HEATER = "steam_heater"
UNIQUE_ID_BREW_CONFIRM_PENDING = "brew_confirm_pending"
UNIQUE_ID_BREW_CONFIRM = "brew_confirm"
UNIQUE_ID_BREW_CONFIRM_CANCEL = "brew_confirm_cancel"
UNIQUE_ID_GRIND = "grind"
UNIQUE_ID_GRIND_BY_WEIGHT = "grind_by_weight"
UNIQUE_ID_GRIND_TARGET_DURATION = "grind_target_duration"
UNIQUE_ID_GRIND_TARGET_WEIGHT = "grind_target_weight"
UNIQUE_ID_BACKFLUSH_START = "backflush_start"
UNIQUE_ID_BACKFLUSH_COMPLETE = "backflush_complete"
UNIQUE_ID_DESCALING_START = "descaling_start"
UNIQUE_ID_DESCALING_COMPLETE = "descaling_complete"
UNIQUE_ID_SHOT_EVENTS = "shot_events"
UNIQUE_ID_MACHINE_EVENTS = "machine_events"

# Services
ATTR_MAX_SHOTS = "max_shots"
ATTR_KEEP_ANNOTATED = "keep_annotated"
SERVICE_TRIM_SHOT_HISTORY = "trim_shot_history"
DATA_SERVICES = f"{DOMAIN}_services"
