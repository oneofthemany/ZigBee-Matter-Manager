"""
Swarm Intelligence — the capability vocabulary.

The rule engine speaks in raw attribute names, operators and literals. Nothing
in it knows that `occupancy` is a thing that can *become true*, that a lux
reading is what "dark" means, or that a bulb and a radar in the same room are an
obvious pair. That knowledge lives here, as data.

Every device — Zigbee, Matter, Nuki, a presence user, a virtual house sensor —
is reduced to the same shape: a list of **offers**. An offer is one thing the
device can contribute to a rule, in one of three roles:

    trigger    an edge worth waking a rule on      ("someone is detected")
    condition  a state worth testing               ("the room is dark")
    action     a command worth sending             ("turn on")

Because every device is described in the same vocabulary, any trigger or
condition composes with any action anywhere on the network. The pairing between
two devices is therefore *derived*, not enumerated: there is no list of
supported combinations to keep up to date, and a device type nobody anticipated
still wires to everything the moment its capabilities resolve.

Offers compile down to the rule dict `AutomationEngine.add_rule()` already
accepts, so this layer adds vocabulary without adding a second execution path.

Read-only. Nothing here mutates device or rule state.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, Optional, Sequence, Tuple

logger = logging.getLogger("modules.swarm.capabilities")

# Roles
TRIGGER = "trigger"
CONDITION = "condition"
ACTION = "action"

# Scope decides how far a blueprint may reach when filling a slot with an offer.
# Room-scoped offers are only interchangeable with others in the same chamber;
# house-scoped ones (a person, the weather, the tariff) apply anywhere.
SCOPE_ROOM = "room"
SCOPE_HOUSE = "house"

# Ambient-light thresholds shared with the NL parser, so "dark" means the same
# number whichever way a rule is authored.
from modules.nl_automations import BRIGHT_LUX, DARK_LUX


# Parameters
#
# A threshold a user should be able to tune on the suggestion card rather than
# have baked into the offer. Referenced from an offer's `value` as
# {"param": "<id>"}; the default is what a one-tap "create" uses.

PARAMS: Dict[str, Dict[str, Any]] = {
    "dark_lux":       {"label": "Dark below",        "type": "int",   "default": DARK_LUX,   "unit": "lx",  "min": 0,    "max": 2000},
    "bright_lux":     {"label": "Bright above",      "type": "int",   "default": BRIGHT_LUX, "unit": "lx",  "min": 0,    "max": 20000},
    "cold_c":         {"label": "Cold below",        "type": "float", "default": 18.0,       "unit": "°C",  "min": 0,    "max": 30},
    "warm_c":         {"label": "Warm above",        "type": "float", "default": 24.0,       "unit": "°C",  "min": 10,   "max": 40},
    "humid_pct":      {"label": "Humid above",       "type": "int",   "default": 65,         "unit": "%",   "min": 0,    "max": 100},
    "dry_pct":        {"label": "Dry below",         "type": "int",   "default": 35,         "unit": "%",   "min": 0,    "max": 100},
    "co2_ppm":        {"label": "CO₂ above",         "type": "int",   "default": 1000,       "unit": "ppm", "min": 400,  "max": 5000},
    "pm25_ugm3":      {"label": "PM2.5 above",       "type": "int",   "default": 35,         "unit": "µg/m³", "min": 0,  "max": 500},
    "voc_index":      {"label": "VOC above",         "type": "int",   "default": 250,        "unit": "",    "min": 0,    "max": 500},
    "battery_pct":    {"label": "Battery below",     "type": "int",   "default": 20,         "unit": "%",   "min": 1,    "max": 100},
    "power_on_w":     {"label": "Drawing above",     "type": "float", "default": 5.0,        "unit": "W",   "min": 0,    "max": 4000},
    "power_idle_w":   {"label": "Idle below",        "type": "float", "default": 2.0,        "unit": "W",   "min": 0,    "max": 4000},
    "brightness_pct": {"label": "Brightness",        "type": "int",   "default": 80,         "unit": "%",   "min": 1,    "max": 100},
    "color_temp_k":   {"label": "Colour temp",       "type": "int",   "default": 2700,       "unit": "K",   "min": 2000, "max": 6500},
    "position_pct":   {"label": "Position",          "type": "int",   "default": 50,         "unit": "%",   "min": 0,    "max": 100},
    "setpoint_c":     {"label": "Set to",            "type": "float", "default": 21.0,       "unit": "°C",  "min": 5,    "max": 30},
    "demand_pct":     {"label": "Demand above",      "type": "int",   "default": 10,         "unit": "%",   "min": 0,    "max": 100},
    "soil_dry_pct":   {"label": "Soil dry below",    "type": "int",   "default": 30,         "unit": "%",   "min": 0,    "max": 100},
    "clear_hold_s":   {"label": "Wait before off",   "type": "int",   "default": 120,        "unit": "s",   "min": 0,    "max": 3600},
    "open_hold_s":    {"label": "Open for",          "type": "int",   "default": 300,        "unit": "s",   "min": 0,    "max": 7200},
    "near_home_min":  {"label": "Minutes from home", "type": "int",   "default": 15,         "unit": "min", "min": 1,    "max": 120},
    "dear_rate_p":    {"label": "Rate above",        "type": "float", "default": 25.0,       "unit": "p/kWh", "min": 0,  "max": 200},
    "cheap_rate_p":   {"label": "Rate below",        "type": "float", "default": 12.0,       "unit": "p/kWh", "min": 0,  "max": 200},
    # A colour is [hue 0-360, saturation 0-100], which is what the device layer
    # takes. The named choices exist so a card can offer swatches and a
    # sentence can say "set the lamp to red" rather than "to [0, 100]".
    "alert_colour":   {"label": "Colour", "type": "colour", "default": [0, 100],
                       "choices": {"red": [0, 100], "amber": [40, 100],
                                   "green": [120, 100], "blue": [220, 100],
                                   "purple": [280, 100], "warm white": [30, 25]}},
    "calm_colour":    {"label": "Colour", "type": "colour", "default": [120, 100],
                       "choices": {"red": [0, 100], "amber": [40, 100],
                                   "green": [120, 100], "blue": [220, 100],
                                   "purple": [280, 100], "warm white": [30, 25]}},
}


def param(pid: str) -> Dict[str, Any]:
    """Reference a tunable parameter from inside an offer's value."""
    return {"param": pid}


def param_display(pid: Optional[str], value: Any) -> str:
    """How a parameter's value should read in a sentence.

    A colour is carried as [hue, saturation] because that is what the device
    takes, but "set the lamp to [0, 100]" is not a sentence. Where the
    parameter names its choices, the matching name is used.
    """
    spec = PARAMS.get(pid or "") or {}
    for name, choice in (spec.get("choices") or {}).items():
        if choice == value:
            return name
    unit = spec.get("unit") or ""
    return f"{value}{unit}"


def resolve_param(value: Any, overrides: Optional[Dict[str, Any]] = None) -> Any:
    """Collapse a {"param": id} marker to a concrete value.

    Anything that is not a marker passes through untouched, so callers can run
    every offer value through this without checking first.
    """
    if not isinstance(value, dict) or "param" not in value:
        return value
    pid = value["param"]
    if overrides and pid in overrides:
        return overrides[pid]
    spec = PARAMS.get(pid)
    return spec.get("default") if spec else None


# Capability vocabulary
#
# Each entry describes one semantic capability and everything it can contribute
# to a rule. Fields:
#
#   label      human name for the capability itself
#   kind       sensor | actuator | virtual — drives grouping in the UI
#   tags       free-form affinities blueprints filter on (e.g. "lighting")
#   scope      room | house — how far this offer reaches (default room)
#   attrs      candidate state keys, best first; the first one present on the
#              device backs every trigger and condition in the entry
#   triggers   offers usable as a rule's source edge
#   conditions offers usable as a prerequisite or a secondary check
#   actions    offers usable as a sequence step
#
# An offer carries:
#   id         stable within the capability; "<cap>:<id>" is globally stable
#   label      sentence fragment, "{device}" and "{room}" substituted
#   operator   engine operator (triggers/conditions)
#   value      literal, or param() marker
#   command    engine command (actions)
#   value_from param id supplying the command's argument (actions)
#   step       non-command step type, for media/notify offers
#   sustain    suggested hold in seconds before the edge counts
#   attrs      overrides the capability-level attrs for this offer alone
#   weight     tiebreak only: which offer leads when two pairings score equally.
#              Never affects confidence — "turn on" simply reads before "toggle".
#   polarity   +1 activating, -1 deactivating, absent where the reading is not
#              unambiguous. Pairing a matching polarity ranks far above pairing
#              opposites, which is what stops "someone is detected -> turn the
#              light off" reading as strongly as the rule anyone actually wants.
#              Left absent on safety sensors: a leak should be free to switch
#              something off as readily as on.

CAPABILITIES: Dict[str, Dict[str, Any]] = {

    # Sensing — occupancy and openings

    "presence": {
        "label": "Presence",
        "kind": "sensor",
        "tags": ["occupancy", "security"],
        "attrs": ["occupancy", "presence", "motion", "occupied", "presence_state"],
        "triggers": [
            {"id": "detected", "label": "someone is detected in {room}",
             "operator": "eq", "value": True, "weight": 2, "polarity": 1},
            {"id": "cleared", "label": "{room} becomes empty",
             "operator": "eq", "value": False, "sustain": param("clear_hold_s"), "weight": 1, "polarity": -1},
        ],
        "conditions": [
            {"id": "occupied", "label": "{room} is occupied", "operator": "eq", "value": True, "polarity": 1},
            {"id": "empty", "label": "{room} is empty", "operator": "eq", "value": False, "polarity": -1},
        ],
        "actions": [],
    },

    "contact": {
        "label": "Contact",
        "kind": "sensor",
        "tags": ["openings", "security"],
        "attrs": ["contact", "is_open", "opening", "door", "window", "is_closed"],
        # Polarity is resolved per device — see _contact_values(). The literals
        # here are placeholders replaced at resolve time.
        "triggers": [
            {"id": "opened", "label": "{device} is opened", "operator": "eq", "value": "$open", "weight": 2, "polarity": 1},
            {"id": "closed", "label": "{device} is closed", "operator": "eq", "value": "$closed", "weight": 1, "polarity": -1},
        ],
        "conditions": [
            {"id": "is_open", "label": "{device} is open", "operator": "eq", "value": "$open", "polarity": 1},
            {"id": "is_closed", "label": "{device} is closed", "operator": "eq", "value": "$closed", "polarity": -1},
        ],
        "actions": [],
    },

    "vibration": {
        "label": "Vibration",
        "kind": "sensor",
        "tags": ["security"],
        "attrs": ["vibration", "tilt", "drop"],
        "triggers": [{"id": "detected", "label": "{device} detects vibration",
                      "operator": "eq", "value": True}],
        "conditions": [],
        "actions": [],
    },

    "tamper": {
        "label": "Tamper",
        "kind": "sensor",
        "tags": ["security", "diagnostic"],
        "attrs": ["tamper"],
        "triggers": [{"id": "tampered", "label": "{device} is tampered with",
                      "operator": "eq", "value": True}],
        "conditions": [],
        "actions": [],
    },

    # Sensing — environment

    "illuminance": {
        "label": "Illuminance",
        "kind": "sensor",
        "tags": ["light", "environment"],
        "attrs": ["illuminance_lux", "illuminance", "lux", "light_level", "illumination"],
        "triggers": [
            {"id": "got_dark", "label": "{room} gets dark", "operator": "lt", "value": param("dark_lux"), "weight": 1, "polarity": 1},
            {"id": "got_bright", "label": "{room} gets bright", "operator": "gt", "value": param("bright_lux"), "polarity": -1},
        ],
        "conditions": [
            {"id": "is_dark", "label": "{room} is dark", "operator": "lt", "value": param("dark_lux"), "polarity": 1},
            {"id": "is_bright", "label": "{room} is bright", "operator": "gt", "value": param("bright_lux"), "polarity": -1},
        ],
        "actions": [],
    },

    "temperature": {
        "label": "Temperature",
        "kind": "sensor",
        "tags": ["climate", "environment"],
        "attrs": ["temperature", "local_temperature", "device_temperature"],
        "triggers": [
            {"id": "got_cold", "label": "{room} drops below {value}", "operator": "lt", "value": param("cold_c"), "polarity": 1},
            {"id": "got_warm", "label": "{room} rises above {value}", "operator": "gt", "value": param("warm_c"), "polarity": -1},
        ],
        "conditions": [
            {"id": "is_cold", "label": "{room} is below {value}", "operator": "lt", "value": param("cold_c")},
            {"id": "is_warm", "label": "{room} is above {value}", "operator": "gt", "value": param("warm_c")},
        ],
        "actions": [],
    },

    "humidity": {
        "label": "Humidity",
        "kind": "sensor",
        "tags": ["climate", "environment"],
        "attrs": ["humidity"],
        "triggers": [
            {"id": "got_humid", "label": "{room} humidity rises above {value}", "operator": "gt", "value": param("humid_pct")},
            {"id": "got_dry", "label": "{room} humidity drops below {value}", "operator": "lt", "value": param("dry_pct")},
        ],
        "conditions": [
            {"id": "is_humid", "label": "{room} is humid", "operator": "gt", "value": param("humid_pct")},
            {"id": "is_dry", "label": "{room} is dry", "operator": "lt", "value": param("dry_pct")},
        ],
        "actions": [],
    },

    "pressure": {
        "label": "Pressure",
        "kind": "sensor",
        "tags": ["environment"],
        "attrs": ["pressure"],
        "triggers": [], "conditions": [], "actions": [],
    },

    "co2": {
        "label": "CO₂",
        "kind": "sensor",
        "tags": ["air", "environment"],
        "attrs": ["co2"],
        "triggers": [{"id": "high", "label": "{room} CO₂ rises above {value}",
                      "operator": "gt", "value": param("co2_ppm")}],
        "conditions": [{"id": "is_high", "label": "{room} CO₂ is above {value}",
                        "operator": "gt", "value": param("co2_ppm")}],
        "actions": [],
    },

    "air_quality": {
        "label": "Air quality",
        "kind": "sensor",
        "tags": ["air", "environment"],
        "attrs": ["pm25", "voc", "formaldehyde", "air_quality"],
        "triggers": [{"id": "poor", "label": "{room} air quality worsens",
                      "operator": "gt", "value": param("pm25_ugm3")}],
        "conditions": [{"id": "is_poor", "label": "{room} air quality is poor",
                        "operator": "gt", "value": param("pm25_ugm3")}],
        "actions": [],
    },

    "soil_moisture": {
        "label": "Soil moisture",
        "kind": "sensor",
        "tags": ["environment"],
        "attrs": ["soil_moisture"],
        "triggers": [{"id": "dry", "label": "{device} soil dries out",
                      "operator": "lt", "value": param("soil_dry_pct")}],
        "conditions": [{"id": "is_dry", "label": "{device} soil is dry",
                        "operator": "lt", "value": param("soil_dry_pct")}],
        "actions": [],
    },

    # Sensing — safety

    "water_leak": {
        "label": "Water leak",
        "kind": "sensor",
        "tags": ["safety", "alarm"],
        "attrs": ["water_leak", "leak", "moisture"],
        "triggers": [{"id": "detected", "label": "{device} detects water",
                      "operator": "eq", "value": True}],
        "conditions": [{"id": "is_wet", "label": "{device} is wet",
                        "operator": "eq", "value": True}],
        "actions": [],
    },

    "smoke": {
        "label": "Smoke",
        "kind": "sensor",
        "tags": ["safety", "alarm"],
        "attrs": ["smoke"],
        "triggers": [{"id": "detected", "label": "{device} detects smoke",
                      "operator": "eq", "value": True}],
        "conditions": [{"id": "is_alarming", "label": "{device} is alarming",
                        "operator": "eq", "value": True}],
        "actions": [],
    },

    "co_detector": {
        "label": "Carbon monoxide",
        "kind": "sensor",
        "tags": ["safety", "alarm"],
        "attrs": ["co_detected", "carbon_monoxide"],
        "triggers": [{"id": "detected", "label": "{device} detects carbon monoxide",
                      "operator": "eq", "value": True}],
        "conditions": [],
        "actions": [],
    },

    "gas": {
        "label": "Gas",
        "kind": "sensor",
        "tags": ["safety", "alarm"],
        "attrs": ["gas", "gas_detected"],
        "triggers": [{"id": "detected", "label": "{device} detects gas",
                      "operator": "eq", "value": True}],
        "conditions": [],
        "actions": [],
    },

    # Sensing — power and health

    "power": {
        "label": "Power draw",
        "kind": "sensor",
        "tags": ["energy"],
        "attrs": ["power", "active_power", "apparent_power"],
        "triggers": [
            {"id": "started", "label": "{device} starts drawing power",
             "operator": "gt", "value": param("power_on_w"), "polarity": 1},
            {"id": "finished", "label": "{device} finishes",
             "operator": "lt", "value": param("power_idle_w"), "sustain": 120, "polarity": -1},
        ],
        "conditions": [
            {"id": "is_running", "label": "{device} is drawing power",
             "operator": "gt", "value": param("power_on_w")},
            {"id": "is_idle", "label": "{device} is idle",
             "operator": "lt", "value": param("power_idle_w")},
        ],
        "actions": [],
    },

    "energy": {
        "label": "Energy",
        "kind": "sensor",
        "tags": ["energy"],
        "attrs": ["energy", "daily_energy", "monthly_energy"],
        "triggers": [], "conditions": [], "actions": [],
    },

    "battery": {
        "label": "Battery",
        "kind": "sensor",
        "tags": ["diagnostic", "maintenance"],
        # Two shapes in the wild: a percentage from the power-configuration
        # cluster, and a bare low flag from the IAS Zone status bitfield. An
        # IAS contact or motion sensor reports only the flag, so a
        # percentage-only capability left the devices most in need of a
        # low-battery warning without one.
        "attrs": ["battery", "battery_percentage_remaining", "battery_low"],
        "triggers": [
            {"id": "low", "label": "{device} battery falls below {value}",
             "attrs": ["battery", "battery_percentage_remaining"],
             "operator": "lt", "value": param("battery_pct")},
            {"id": "low_flag", "label": "{device} reports a low battery",
             "attrs": ["battery_low"], "operator": "eq", "value": True},
        ],
        "conditions": [
            {"id": "is_low", "label": "{device} battery is low",
             "attrs": ["battery", "battery_percentage_remaining"],
             "operator": "lt", "value": param("battery_pct")},
            {"id": "is_low_flag", "label": "{device} battery is low",
             "attrs": ["battery_low"], "operator": "eq", "value": True},
        ],
        "actions": [],
    },

    "availability": {
        "label": "Availability",
        "kind": "sensor",
        "tags": ["diagnostic", "maintenance"],
        "attrs": ["available"],
        "triggers": [
            {"id": "went_offline", "label": "{device} goes offline", "operator": "eq", "value": False},
            {"id": "came_online", "label": "{device} comes back online", "operator": "eq", "value": True},
        ],
        "conditions": [{"id": "is_online", "label": "{device} is online",
                        "operator": "eq", "value": True}],
        "actions": [],
    },

    # Controllers

    "button": {
        "label": "Button",
        "kind": "sensor",
        "tags": ["controller"],
        "attrs": ["action", "click", "button_action", "event", "scene"],
        # Values come from the device's own value_options — a button offers one
        # trigger per press type it actually reports, expanded at resolve time.
        "triggers": [{"id": "pressed", "label": "{device} is pressed",
                      "operator": "eq", "value": "$action", "expand": "value_options"}],
        "conditions": [],
        "actions": [],
    },

    "rotary": {
        "label": "Rotary",
        "kind": "sensor",
        "tags": ["controller"],
        "attrs": ["rotary", "rotation", "step"],
        "triggers": [{"id": "turned", "label": "{device} is turned",
                      "operator": "neq", "value": None}],
        "conditions": [],
        "actions": [],
    },

    # Actuation

    "on_off": {
        "label": "On / off",
        "kind": "actuator",
        "tags": ["switchable"],
        # No `state_1` here: the resolver matches an endpoint-suffixed form of
        # any candidate, and listing one explicitly would short-circuit that as
        # an exact match — collapsing a dual-gang socket to a single outlet.
        # `state_l1` stays: its suffix is not numeric, so nothing infers it.
        "attrs": ["state", "state_l1", "on", "on_off"],
        "triggers": [
            {"id": "turned_on", "label": "{device} is turned on", "operator": "eq", "value": True, "polarity": 1},
            {"id": "turned_off", "label": "{device} is turned off", "operator": "eq", "value": False, "polarity": -1},
        ],
        "conditions": [
            {"id": "is_on", "label": "{device} is on", "operator": "eq", "value": True},
            {"id": "is_off", "label": "{device} is off", "operator": "eq", "value": False},
        ],
        "actions": [
            {"id": "turn_on", "label": "turn on {device}", "command": "on", "weight": 2, "polarity": 1},
            {"id": "turn_off", "label": "turn off {device}", "command": "off", "weight": 1, "polarity": -1},
            {"id": "toggle", "label": "toggle {device}", "command": "toggle"},
        ],
    },

    "brightness": {
        "label": "Brightness",
        "kind": "actuator",
        "tags": ["lighting", "switchable"],
        "attrs": ["brightness", "level"],
        "triggers": [],
        "conditions": [
            {"id": "is_dim", "label": "{device} is dim", "operator": "lt", "value": param("brightness_pct")},
        ],
        "actions": [
            {"id": "set_brightness", "label": "set {device} to {value}",
             "command": "brightness", "value_from": "brightness_pct", "polarity": 1},
        ],
    },

    "color_temp": {
        "label": "Colour temperature",
        "kind": "actuator",
        "tags": ["lighting"],
        "attrs": ["color_temp", "color_temp_kelvin"],
        "triggers": [], "conditions": [],
        "actions": [
            {"id": "set_color_temp", "label": "set {device} to {value}",
             "command": "color_temp", "value_from": "color_temp_k"},
        ],
    },

    "color": {
        "label": "Colour",
        "kind": "actuator",
        # Colour temperature lives in the same ColorControl cluster, and no
        # legacy vocabulary names it separately — so without this a colour bulb
        # could be given a hue but never a warmth. Declaring it is safe: the
        # action is gated on the device actually having a color_temp command.
        "implies": ["color_temp"],
        "tags": ["lighting", "signalling"],
        "attrs": ["color_mode", "hue", "saturation"],
        "triggers": [],
        "conditions": [],
        # A light that can hold a colour can say something a light that only
        # switches cannot: a colour is a notification nobody has to read.
        "actions": [
            {"id": "set_color", "label": "turn {device} {value}",
             "command": "hs_color", "value_from": "alert_colour", "weight": 1,
             "polarity": 1},
        ],
    },

    "cover": {
        "label": "Cover",
        "kind": "actuator",
        "tags": ["openings", "shading"],
        "attrs": ["position", "current_position_lift_percentage", "cover_position"],
        "triggers": [
            {"id": "opened", "label": "{device} opens", "operator": "gt", "value": 0},
            {"id": "closed", "label": "{device} closes", "operator": "lte", "value": 0},
        ],
        "conditions": [
            {"id": "is_open", "label": "{device} is open", "operator": "gt", "value": 0},
            {"id": "is_closed", "label": "{device} is closed", "operator": "lte", "value": 0},
        ],
        "actions": [
            {"id": "open", "label": "open {device}", "command": "open", "weight": 1, "polarity": 1},
            {"id": "close", "label": "close {device}", "command": "close", "weight": 1, "polarity": -1},
            {"id": "stop", "label": "stop {device}", "command": "stop"},
            {"id": "set_position", "label": "set {device} to {value}",
             "command": "position", "value_from": "position_pct"},
        ],
    },

    "thermostat": {
        "label": "Thermostat",
        "kind": "actuator",
        "tags": ["climate", "heating"],
        "attrs": ["local_temperature", "temperature"],
        "triggers": [],
        "conditions": [
            {"id": "below_target", "label": "{room} is below {value}",
             "operator": "lt", "value": param("setpoint_c")},
            {"id": "calling_for_heat", "label": "{room} is calling for heat",
             "attrs": ["pi_heating_demand", "running_state"],
             "operator": "gt", "value": param("demand_pct")},
        ],
        "actions": [
            {"id": "set_setpoint", "label": "set {device} to {value}",
             "command": "temperature", "value_from": "setpoint_c"},
        ],
    },

    "fan": {
        "label": "Fan",
        "kind": "actuator",
        "tags": ["climate"],
        "attrs": ["fan_mode", "fan_speed"],
        "triggers": [], "conditions": [], "actions": [],
    },

    "lock": {
        "label": "Lock",
        "kind": "actuator",
        "tags": ["security", "access"],
        "attrs": ["locked", "lock_state"],
        "triggers": [
            {"id": "locked", "label": "{device} locks", "operator": "eq", "value": True, "polarity": -1},
            {"id": "unlocked", "label": "{device} unlocks", "operator": "eq", "value": False, "polarity": 1},
        ],
        "conditions": [
            {"id": "is_locked", "label": "{device} is locked", "operator": "eq", "value": True},
            {"id": "is_unlocked", "label": "{device} is unlocked", "operator": "eq", "value": False},
        ],
        "actions": [
            {"id": "lock", "label": "lock {device}", "command": "lock", "weight": 1, "polarity": -1},
            {"id": "unlock", "label": "unlock {device}", "command": "unlock", "weight": 1, "polarity": 1},
        ],
    },

    # House-scope virtual inputs

    "person": {
        "label": "Person",
        "kind": "virtual",
        "scope": SCOPE_HOUSE,
        "tags": ["presence", "people"],
        "implies": ["notify"],
        "excludes": ["presence", "availability"],
        "attrs": ["place"],
        "triggers": [
            {"id": "arrived_home", "label": "{device} arrives home",
             "type": "zone", "event": "enter", "place": "home", "weight": 2, "polarity": 1},
            {"id": "left_home", "label": "{device} leaves home",
             "type": "zone", "event": "leave", "place": "home", "weight": 1, "polarity": -1},
            {"id": "arrived_anywhere", "label": "{device} arrives somewhere",
             "type": "zone", "event": "enter", "place": "any"},
        ],
        "conditions": [
            {"id": "is_home", "label": "{device} is home",
             "attrs": ["presence"], "operator": "eq", "value": "home"},
            {"id": "is_away", "label": "{device} is away",
             "attrs": ["presence"], "operator": "eq", "value": "away"},
        ],
        "actions": [],
    },

    "household": {
        "label": "Household",
        "kind": "virtual",
        "scope": SCOPE_HOUSE,
        "excludes": ["availability"],
        "tags": ["presence", "people"],
        "attrs": ["anyone_home"],
        "triggers": [
            {"id": "first_home", "label": "the first person gets home",
             "operator": "gte", "value": 1, "polarity": 1},
            {"id": "last_left", "label": "everybody leaves",
             "operator": "lt", "value": 1, "polarity": -1},
        ],
        "conditions": [
            {"id": "anyone_home", "label": "somebody is home", "operator": "gte", "value": 1},
            {"id": "nobody_home", "label": "nobody is home", "operator": "lt", "value": 1},
            {"id": "everyone_home", "label": "everybody is home",
             "attrs": ["everyone_home"], "operator": "gte", "value": 1},
        ],
        "actions": [],
    },

    "weather": {
        "label": "Weather",
        "kind": "virtual",
        "scope": SCOPE_HOUSE,
        "excludes": ["availability"],
        "sniffable": False,
        "tags": ["climate", "environment", "outdoor"],
        "attrs": ["temperature"],
        "triggers": [
            {"id": "got_cold_out", "label": "it drops below {value} outside",
             "operator": "lt", "value": param("cold_c"), "polarity": 1},
            {"id": "got_warm_out", "label": "it rises above {value} outside",
             "operator": "gt", "value": param("warm_c"), "polarity": -1},
            {"id": "got_dark_out", "label": "daylight fades",
             "attrs": ["is_daylight"], "operator": "eq", "value": 0, "polarity": 1},
        ],
        "conditions": [
            {"id": "cold_out", "label": "it is below {value} outside",
             "operator": "lt", "value": param("cold_c"), "polarity": 1},
            {"id": "warm_out", "label": "it is above {value} outside",
             "operator": "gt", "value": param("warm_c"), "polarity": -1},
            {"id": "is_daylight", "label": "it is daylight",
             "attrs": ["is_daylight"], "operator": "eq", "value": 1},
            {"id": "is_dark_out", "label": "it is dark outside",
             "attrs": ["is_daylight"], "operator": "eq", "value": 0},
        ],
        "actions": [],
    },

    "house": {
        "label": "House",
        "kind": "virtual",
        "scope": SCOPE_HOUSE,
        "excludes": ["availability"],
        "sniffable": False,
        "tags": ["climate", "heating"],
        "attrs": ["indoor_avg_temp"],
        # The computed flags are the point of this capability. The engine
        # compares an attribute to a literal, so a comparison between two live
        # readings — inside against outside, travel time against warm-up time —
        # is made by the module that owns the model and published as a flag.
        "triggers": [
            {"id": "preheat_due", "label": "somebody is due home within the warm-up time",
             "attrs": ["preheat_now_for_arrival"], "operator": "eq", "value": 1,
             "weight": 2, "polarity": 1},
            {"id": "cooler_outside", "label": "it becomes cooler outside than in",
             "attrs": ["outdoor_cooler_than_indoor"], "operator": "eq", "value": 1,
             "polarity": 1},
            {"id": "got_cold_inside", "label": "the house drops below {value}",
             "operator": "lt", "value": param("cold_c"), "polarity": 1},
            {"id": "got_warm_inside", "label": "the house rises above {value}",
             "operator": "gt", "value": param("warm_c"), "polarity": -1},
        ],
        "conditions": [
            {"id": "cold_inside", "label": "the house is below {value}",
             "operator": "lt", "value": param("cold_c"), "polarity": 1},
            {"id": "warm_inside", "label": "the house is above {value}",
             "operator": "gt", "value": param("warm_c"), "polarity": -1},
            {"id": "cooler_outside", "label": "it is cooler outside than in",
             "attrs": ["outdoor_cooler_than_indoor"], "operator": "eq", "value": 1},
            {"id": "warmer_outside", "label": "it is warmer outside than in",
             "attrs": ["outdoor_cooler_than_indoor"], "operator": "eq", "value": 0},
        ],
        "actions": [],
    },
    "tariff": {
        "label": "Tariff",
        "kind": "virtual",
        "scope": SCOPE_HOUSE,
        "excludes": ["availability"],
        "sniffable": False,
        "tags": ["energy"],
        # Two ways a tariff is cheap. An agile tariff has a *cheapest window*,
        # which is relative and moves daily; a fixed or two-rate tariff has only
        # a price, and comparing it against a number is the only question
        # available. Modelling just the window left a household with a working
        # unit rate and no agile window unable to say anything about
        # electricity at all.
        "attrs": ["is_off_peak", "unit_rate"],
        "triggers": [
            {"id": "off_peak_started", "label": "cheap-rate electricity starts",
             "attrs": ["is_off_peak"],
             "operator": "eq", "value": 1, "weight": 2, "polarity": 1},
            {"id": "off_peak_ended", "label": "cheap-rate electricity ends",
             "attrs": ["is_off_peak"], "operator": "eq", "value": 0, "polarity": -1},
            {"id": "got_cheap", "label": "electricity drops below {value}",
             "attrs": ["unit_rate"], "operator": "lt",
             "value": param("cheap_rate_p"), "weight": 1, "polarity": 1},
            {"id": "got_dear", "label": "electricity rises above {value}",
             "attrs": ["unit_rate"], "operator": "gt",
             "value": param("dear_rate_p"), "polarity": -1},
        ],
        "conditions": [
            {"id": "is_off_peak", "label": "electricity is at the cheap rate",
             "attrs": ["is_off_peak"], "operator": "eq", "value": 1, "polarity": 1},
            {"id": "is_peak", "label": "electricity is at the expensive rate",
             "attrs": ["is_off_peak"], "operator": "eq", "value": 0, "polarity": -1},
            {"id": "is_cheap", "label": "electricity is below {value}",
             "attrs": ["unit_rate"], "operator": "lt",
             "value": param("cheap_rate_p"), "polarity": 1},
            {"id": "is_dear", "label": "electricity is above {value}",
             "attrs": ["unit_rate"], "operator": "gt",
             "value": param("dear_rate_p"), "polarity": -1},
        ],
        "actions": [],
    },

    "notify": {
        "label": "Notify",
        "kind": "virtual",
        "scope": SCOPE_HOUSE,
        "tags": ["messaging"],
        "attrs": [],
        "triggers": [], "conditions": [],
        "actions": [
            {"id": "message", "label": "message {device}", "step": "request"},
        ],
    },
}


# Legacy capability vocabularies → canonical ids
#
# Three vocabularies already exist and all three stay: DeviceCapabilities
# derives from Zigbee clusters, device_profiles.DEVICE_TYPES from the profile
# schema, and Matter definitions from the commissioning scan. They disagree on
# names for the same idea ("motion_sensor" / "motion", "power_monitoring" /
# "power"), so everything is folded here rather than at each call site.
#
# A value of None means the name carries no semantics worth an offer — it is a
# grouping label or a transport marker, and the real capabilities sit alongside.

LEGACY_CAPABILITY_ALIASES: Dict[str, Optional[str]] = {
    # DeviceCapabilities (Zigbee clusters)
    "occupancy_sensing": "presence",
    "motion_sensor": "presence",
    "presence_sensor": "presence",
    "radar_sensor": "presence",
    "contact_sensor": "contact",
    "illuminance_sensor": "illuminance",
    "temperature_sensor": "temperature",
    "humidity_sensor": "humidity",
    "pressure_sensor": "pressure",
    "power_monitoring": "power",
    "metering": "energy",
    "level_control": "brightness",
    "color_control": "color",
    "window_covering": "cover",
    "fan_control": "fan",
    "switch": "on_off",
    "light": "on_off",
    "environmental_sensor": None,
    "hvac": None,
    "ias_zone": None,
    "tuya": None,
    "matter": None,
    "multi_endpoint": None,
    "multi_switch": None,
    # device_profiles.DEVICE_TYPES
    "motion": "presence",
    "door_lock": "lock",
    # Names that already match a canonical id map to themselves implicitly.
}


def canonical_capability(name: str) -> Optional[str]:
    """Fold any of the legacy vocabularies onto a canonical capability id."""
    if name in CAPABILITIES:
        return name
    if name in LEGACY_CAPABILITY_ALIASES:
        return LEGACY_CAPABILITY_ALIASES[name]
    return None


# Device classes
#
# What the device *is*, kept separate from what it can do: two devices may both
# offer on_off while only one is a light, and a blueprint that wants "a light"
# must be able to say so.

DEVICE_CLASS_RULES: Sequence[Tuple[str, Callable[[set], bool]]] = (
    # Identity first. These capabilities are exclusive and unambiguous — only a
    # person has `person` — and they must outrank the readings they carry: the
    # weather reports temperature and humidity, which would otherwise make it a
    # climate sensor.
    ("person",           lambda c: "person" in c),
    ("household",        lambda c: "household" in c),
    ("weather",          lambda c: "weather" in c),
    ("house",            lambda c: "house" in c),
    ("tariff",           lambda c: "tariff" in c),

    # Then what a device unambiguously *is*, by a capability nothing else has.
    ("lock",             lambda c: "lock" in c),
    ("cover",            lambda c: "cover" in c),
    ("thermostat",       lambda c: "thermostat" in c),

    # Sensing that cannot be faked, before any switchable class. A contact or
    # occupancy reading is hard evidence; an on_off or brightness capability may
    # be an artefact of a binding cluster the device never honours.
    ("contact_sensor",   lambda c: "contact" in c),
    ("leak_sensor",      lambda c: "water_leak" in c),
    ("smoke_sensor",     lambda c: "smoke" in c),
    ("presence_sensor",  lambda c: "presence" in c),
    ("button",           lambda c: "button" in c or "rotary" in c),

    # Switchable things. A plug outranks a light: a metering socket often
    # advertises a level cluster it does not use, while a dimmable bulb almost
    # never reports power.
    ("plug",             lambda c: "on_off" in c and ("power" in c or "energy" in c)),
    ("color_light",      lambda c: "color" in c and "on_off" in c),
    ("light",            lambda c: "brightness" in c and "on_off" in c),
    ("switch",           lambda c: "on_off" in c),

    # Measurement, most specific first.
    ("air_sensor",       lambda c: bool({"co2", "air_quality"} & c)),
    ("climate_sensor",   lambda c: bool({"temperature", "humidity"} & c)),
    ("light_sensor",     lambda c: "illuminance" in c),
)


def classify(capabilities: Iterable[str]) -> str:
    """Best single label for a device, most specific rule first."""
    caps = set(capabilities)
    for label, test in DEVICE_CLASS_RULES:
        if test(caps):
            return label
    return "generic"
