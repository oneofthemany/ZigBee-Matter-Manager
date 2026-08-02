"""
Aqara/Xiaomi specific handlers.
Handles: Buttons, Cubes, Vibration sensors (MultistateInput)
"""
import logging
import struct
from typing import Any, Dict, List
import asyncio
import zigpy.types as t

from .base import ClusterHandler, register_handler

# ZCL data-type IDs for raw writes that bypass cluster schema lookup
_ZCL_DATATYPE_ID = {
    t.Bool:     0x10,
    t.uint8_t:  0x20,
    t.uint16_t: 0x21,
    t.uint32_t: 0x23,
    t.int8s:    0x28,
    t.int16s:   0x29,
    t.int32s:   0x2B,
}

logger = logging.getLogger("handlers.aqara")


# XIAOMI STRUCTURED ATTRIBUTE PARSER
def parse_xiaomi_struct(data: bytes) -> dict:
    """
    Parse Xiaomi/Aqara structured attribute data (typically attribute 0x00DF or 0x00F7).

    Format: Each sub-attribute is encoded as:
      - 1 byte: sub-attribute ID
      - 1 byte: data type (0x10=bool, 0x20=uint8, 0x21=uint16, 0x23=uint32, 0x28=int8, etc)
      - N bytes: value (length depends on type)

    Returns dict of {sub_attr_id: value}
    """
    result = {}
    pos = 0

    while pos < len(data):
        if pos + 2 > len(data):
            break

        sub_attr_id = data[pos]
        data_type = data[pos + 1]
        pos += 2

        try:
            if data_type == 0x10:  # Bool
                if pos + 1 > len(data): break
                value = bool(data[pos])
                pos += 1
            elif data_type == 0x20:  # uint8
                if pos + 1 > len(data): break
                value = data[pos]
                pos += 1
            elif data_type == 0x21:  # uint16 LE
                if pos + 2 > len(data): break
                value = struct.unpack('<H', data[pos:pos+2])[0]
                pos += 2
            elif data_type == 0x23:  # uint32 LE
                if pos + 4 > len(data): break
                value = struct.unpack('<I', data[pos:pos+4])[0]
                pos += 4
            elif data_type == 0x25:  # uint48 LE
                if pos + 6 > len(data): break
                value = int.from_bytes(data[pos:pos+6], 'little')
                pos += 6
            elif data_type == 0x28:  # int8 LE
                if pos + 1 > len(data): break
                value = struct.unpack('<b', data[pos:pos+1])[0]
                pos += 1
            elif data_type == 0x29:  # int16 LE
                if pos + 2 > len(data): break
                value = struct.unpack('<h', data[pos:pos+2])[0]
                pos += 2
            elif data_type == 0x2B:  # int32 LE
                if pos + 4 > len(data): break
                value = struct.unpack('<i', data[pos:pos+4])[0]
                pos += 4
            elif data_type == 0x39:  # float
                if pos + 4 > len(data): break
                value = struct.unpack('<f', data[pos:pos+4])[0]
                pos += 4
            elif data_type in (0x41, 0x42):  # octet/char string
                if pos + 1 > len(data): break
                str_len = data[pos]
                pos += 1
                if pos + str_len > len(data): break
                value = data[pos:pos+str_len]
                if data_type == 0x42:
                    value = value.decode('utf-8', errors='ignore')
                pos += str_len
            else:
                logger.debug(f"Unknown Xiaomi data type: 0x{data_type:02X}")
                break

            result[sub_attr_id] = value
        except Exception as e:
            logger.error(f"Error parsing Xiaomi struct at pos {pos}: {e}")
            break

    return result


# Map sub-attribute IDs to meaningful names and converters
XIAOMI_ATTR_MAP = {
    0x01: ("battery_voltage_mV", lambda v: v),  # mV
    0x03: ("device_temperature", lambda v: v),  # Celsius
    0x04: ("power", lambda v: v / 10.0),  # 0.1W units
    0x05: ("voltage", lambda v: v / 10.0),  # 0.1V units
    0x06: ("current", lambda v: v / 1000.0),  # mA to A
    0x07: ("consumption", lambda v: v),  # Energy
    0x08: ("power_factor", lambda v: v),
    0x09: ("frequency", lambda v: v / 10.0),  # 0.1 Hz
    0x64: ("switch_state", lambda v: bool(v)),
    0x65: ("switch_state_ep2", lambda v: bool(v)),  # dual-gang if present
    0x6E: ("switch_state_ep3", lambda v: bool(v)),  # triple-gang if present
    0x95: ("power_consumption", lambda v: v),
    0x96: ("voltage_96", lambda v: v),
    0x97: ("current_97", lambda v: v),
    0x98: ("power_98", lambda v: v),
    0x9A: ("energy", lambda v: v),
    0x9B: ("indicator_mode", lambda v: v),
    0x0152: ("trigger_indicator", lambda v: bool(v)),
    0x6F: ("startup_on_off", lambda v: v),  # power-on behaviour
}


# MULTISTATE INPUT CLUSTER (0x0012)
# Used by: Aqara Buttons, Cube, Vibration Sensor
@register_handler(0x0012)
class MultistateInputHandler(ClusterHandler):
    """
    Handles Multistate Input cluster (0x0012).
    Used by Aqara buttons to report clicks (single, double, hold, etc).
    """
    CLUSTER_ID = 0x0012
    REPORT_CONFIG = [
        ("present_value", 0, 3600, 1),  # Report immediately on change
    ]

    ATTR_PRESENT_VALUE = 0x0055

    # Aqara Button mappings (approximate, varies by model)
    # For WXKG11LM / WXKG12LM etc.
    ACTION_MAP = {
        0: "hold",
        1: "single",
        2: "double",
        3: "triple",
        4: "quadruple",
        16: "hold",
        17: "release",
        18: "shake",
        255: "release"
    }

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if attrid == self.ATTR_PRESENT_VALUE:
            if hasattr(value, 'value'): value = value.value

            # Lookup action name
            action_name = self.ACTION_MAP.get(value, f"action_{value}")

            logger.info(f"[{self.device.ieee}] Aqara Button Action: {action_name} (val={value})")

            # Update state with the last action
            # We use 'action' state which is standard for buttons in HA
            self.device.update_state({
                "action": action_name,
                "multistate_value": value
            })

            # Emit an event so we can trigger automations
            self.device.emit_event("button_press", {
                "action": action_name,
                "value": value
            })

    def get_attr_name(self, attrid: int) -> str:
        if attrid == self.ATTR_PRESENT_VALUE:
            return "action_value"
        return super().get_attr_name(attrid)

    def get_discovery_configs(self) -> list:
        """Generate HA discovery for the button action."""
        return [
            {
                "component": "sensor",
                "object_id": "action",
                "config": {
                    "name": "Action",
                    "icon": "mdi:gesture-tap-button",
                    "value_template": "{{ value_json.action }}"
                }
            }
        ]


# ANALOG INPUT CLUSTER (0x000C)
# Used by: Aqara Cube (Rotation) or Vibration Sensor
@register_handler(0x000C)
class AqaraAnalogInputHandler(ClusterHandler):
    CLUSTER_ID = 0x000C
    ATTR_PRESENT_VALUE = 0x0055

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if attrid == self.ATTR_PRESENT_VALUE:
            if hasattr(value, 'value'): value = value.value
            self.device.update_state({"analog_input": value})
            logger.debug(f"[{self.device.ieee}] Aqara Analog: {value}")


# AQARA MANUFACTURER SPECIFIC CLUSTER (0xFCC0)
# Used by: Aqara TRV E1, Thermostats, Motion Sensors, Switches, etc.
@register_handler(0xFCC0)
class AqaraManufacturerCluster(ClusterHandler):
    """
    Handles Aqara/Xiaomi manufacturer-specific cluster (0xFCC0).

    This cluster is manufacturer code 0x115F (LUMI/Aqara).
    It provides proprietary attributes for various Aqara devices:
    - Thermostats/TRVs: Window detection, child lock, valve calibration
    - Motion sensors: Detection interval, sensitivity, trigger indicator
    - Switches: Decoupled mode, power outage memory, indicator light
    - Temperature/Humidity sensors: Display unit, measurement interval

    Based on ZHA's XiaomiAqaraE1Cluster and OppleCluster patterns.
    """
    CLUSTER_ID = 0xFCC0
    MANUFACTURER_CODE = 0x115F  # LUMI/Aqara manufacturer code

    # Common Attributes (multiple device types)
    ATTR_MODE = 0x0009                  # uint8 - Device mode
    ATTR_POWER_OUTAGE_MEM = 0x0201      # Bool - Power outage memory

    # Switch/Relay Attributes
    ATTR_OPERATION_MODE = 0x0200        # uint8 - 0=Decoupled, 1=Coupled
    ATTR_SWITCH_MODE = 0x0004           # uint8 - 1=Fast, 2=Multi
    ATTR_SWITCH_TYPE = 0x000A           # uint8 - 1=Toggle, 2=Momentary
    ATTR_INDICATOR_LIGHT = 0x00F0       # uint8 - 0=Normal, 1=Reverse

    # Motion Sensor Attributes
    ATTR_DETECTION_INTERVAL = 0x0102    # uint8 - Seconds between detections
    ATTR_MOTION_SENSITIVITY = 0x010C    # uint8 - 1=Low, 2=Medium, 3=High
    ATTR_TRIGGER_INDICATOR = 0x0152     # uint8 - 0=Off, 1=On

    # Thermostat/TRV Attributes (E1: lumi.airrtc.agl001)
    ATTR_MOTOR_CALIBRATION = 0x0270     # 624 decimal - Write 1 to start calibration
    ATTR_SYSTEM_MODE = 0x0271           # uint8 - System mode
    ATTR_PRESET = 0x0272                # uint8 - Preset mode
    ATTR_WINDOW_DETECTION = 0x0273      # 627 decimal - Window detection
    ATTR_VALVE_DETECTION = 0x0274       # 628 decimal - Valve detection
    ATTR_VALVE_ALARM = 0x0275           # uint8 - Valve error status
    ATTR_SCHEDULE_SETTINGS = 0x0276     # Data - Schedule programming
    ATTR_CHILD_LOCK = 0x0277            # 631 decimal - Child lock
    ATTR_AWAY_PRESET_TEMPERATURE = 0x0279  # uint32 - Away temp
    ATTR_WINDOW_OPEN = 0x027A           # uint8 - 1=Open, 0=Closed (status)
    ATTR_CALIBRATED = 0x027B            # bool "calibrated" flag (READ-ONLY): 1=valve calibrated, 0=needs calibration (mount on radiator valve + triple-press). NOT a 4-state enum — per Zigbee2MQTT this is binary on agl001/SRTS-A01.
    ATTR_SCHEDULE = 0x027D              # uint8 - Schedule enable/disable
    ATTR_SENSOR_TYPE = 0x027E           # uint8 - 0=Internal, 1=External sensor
    ATTR_EXTERNAL_TEMP = 0x0280         # int16 - External temp in centidegrees (signed)
    ATTR_BATTERY_PCT = 0x040A           # uint8 - Battery percentage
    ATTR_REPORTING_INTERVAL = 0x00EE    # uint32 - Aqara checkin/reporting interval (seconds)
    ATTR_BATTERY_REPLACE = 0x000B       # Bool  - Battery replacement (rarely reported)
    ATTR_SETUP_MODE = 0x027C            # uint8 - Setup/E11 commissioning flag
    # Alias kept for legacy code paths that referenced the input form of 0x0280
    ATTR_EXTERNAL_TEMP_INPUT = 0x0280

    # Temperature/Humidity Sensor Attributes
    ATTR_TEMP_DISPLAY_UNIT = 0xFF01     # uint8 - 0=Celsius, 1=Fahrenheit
    ATTR_MEASUREMENT_INTERVAL = 0x00EF  # uint16 - Measurement interval seconds

    # Type Enforcement Map
    ATTR_TYPES = {
        # Boolean Attributes
        0x0201: t.Bool,      # Power Outage Memory
        0x027A: t.Bool,      # Window Open Status
        0x0275: t.Bool,      # Valve Alarm

        # Integer Attributes
        0x0273: t.uint8_t,   # Window Detection
        0x0274: t.uint8_t,   # Valve Detection
        0x0277: t.uint8_t,   # Child Lock
        0x0270: t.uint8_t,   # Motor Calibration
        0x0200: t.uint8_t,   # Operation Mode
        0x0004: t.uint8_t,   # Switch Mode
        0x000A: t.uint8_t,   # Switch Type
        0x00F0: t.uint8_t,   # Indicator Light
        0x0271: t.uint8_t,   # System Mode
        0x0272: t.uint8_t,   # Preset
        0x0279: t.uint32_t,  # Away Preset Temperature (centidegrees)
        0x027B: t.uint8_t,   # Calibrated Status
        0x027E: t.uint8_t,   # Sensor Type
        0x0280: t.int16s,    # External Temperature (centidegrees, signed)
        0x0102: t.uint8_t,   # Detection Interval
        0x010C: t.uint8_t,   # Motion Sensitivity
        0x0152: t.uint8_t,   # Trigger Indicator
    }

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        """
        Handle attribute updates from the Aqara manufacturer cluster.
        Parses and updates device state based on attribute ID.
        """
        if hasattr(value, 'value'):
            value = value.value

        updates = {}

        # Thermostat/TRV Attributes
        if attrid == self.ATTR_WINDOW_DETECTION:
            updates["window_detection"] = bool(value)
            logger.info(f"[{self.device.ieee}] Window detection: {'enabled' if value else 'disabled'}")

        elif attrid == self.ATTR_VALVE_DETECTION:
            updates["valve_detection"] = bool(value)
            logger.info(f"[{self.device.ieee}] Valve detection: {'enabled' if value else 'disabled'}")

        # TRV System Mode
        elif attrid == self.ATTR_SYSTEM_MODE:  # 0x0271
            # Aqara supports only off and heat. Maps onto `system_mode`, the key
            # the frontend reads — not the legacy `aqara_system_mode`.
            SYSTEM_MODES = {0: "off", 1: "heat"}
            mode_name = SYSTEM_MODES.get(value, f"unknown({value})")
            updates["system_mode"] = mode_name
            logger.info(f"[{self.device.ieee}] System mode: {mode_name}")

        # TRV Preset (manual / away / auto)
        elif attrid == self.ATTR_PRESET:  # 0x0272
            # decodePreset(): {0: manual, 1: auto, 2: away}.
            # Value 3 = device firmware-internal "in setup / commissioning"
            PRESET_MAP = {0: "manual", 1: "auto", 2: "away", 3: "setup"}
            preset_name = PRESET_MAP.get(value, f"unknown({value})")
            updates["preset"] = preset_name
            logger.info(f"[{self.device.ieee}] Preset: {preset_name}")

        # 0x027B is a binary "calibrated" flag, not a 4-state enum (per Z2M).
        # Mapped onto the frontend status strings: true -> "ready",
        # false -> "not_ready". See docs/aqara_cluster_guide.md.
        elif attrid == self.ATTR_CALIBRATED:  # 0x027B
            is_cal = bool(value)
            updates["calibrated"] = is_cal
            updates["calibration_status"] = "ready" if is_cal else "not_ready"
            logger.info(f"[{self.device.ieee}] Calibrated: {is_cal}")

        # Setup / E11 commissioning flag
        elif attrid == self.ATTR_SETUP_MODE:  # 0x027C
            updates["setup_mode"] = bool(value)
            if value:
                logger.warning(
                    f"[{self.device.ieee}] Device is in SETUP mode — controls "
                    "will be ignored until calibration completes (triple-tap "
                    "the button on the device to start)"
                )

        # Schedule enable/disable
        elif attrid == self.ATTR_SCHEDULE:  # 0x027D
            updates["schedule_enabled"] = bool(value)
            logger.info(f"[{self.device.ieee}] Schedule: {'on' if value else 'off'}")

        # Sensor Type
        elif attrid == self.ATTR_SENSOR_TYPE:  # 0x027E
            sensor_name = "external" if value in (1, 2) else "internal"
            updates["sensor_type"] = sensor_name
            logger.info(f"[{self.device.ieee}] Sensor type: {sensor_name}")

        # 0x0280 is a status byte (0x00/0x01), not a temperature. External temp
        # is pushed via 0xFFF2 and echoed back through local_temperature.
        elif attrid == self.ATTR_EXTERNAL_TEMP:  # 0x0280
            logger.debug(f"[{self.device.ieee}] Ignoring 0x0280 report: {value!r}")

        # Away Preset Temperature
        elif attrid == self.ATTR_AWAY_PRESET_TEMPERATURE:  # 0x0279
            updates["away_preset_temperature"] = round(value / 100, 1) if value else 0

        # Battery Percentage
        elif attrid == self.ATTR_BATTERY_PCT:  # 0x040A
            updates["battery"] = min(value, 100)
            logger.info(f"[{self.device.ieee}] Battery: {value}%")

        # Aqara Checkin / Reporting Interval
        elif attrid == self.ATTR_REPORTING_INTERVAL:  # 0x00EE
            updates["checkin_interval"] = value
            logger.info(
                f"[{self.device.ieee}] Aqara checkin interval: {value}s "
                f"(~{value // 60}min)"
            )

        elif attrid == self.ATTR_CHILD_LOCK:
            updates["child_lock"] = bool(value)
            logger.info(f"[{self.device.ieee}] Child lock: {'locked' if value else 'unlocked'}")

        elif attrid == self.ATTR_WINDOW_OPEN:
            updates["window_open"] = bool(value)
            logger.info(f"[{self.device.ieee}] Window: {'open' if value else 'closed'}")

        elif attrid == self.ATTR_VALVE_ALARM:
            updates["valve_alarm"] = bool(value)
            if value:
                logger.warning(f"[{self.device.ieee}] Valve alarm triggered!")

        elif attrid == self.ATTR_MOTOR_CALIBRATION:
            # 0x0270 is write-only; the device auto-resets it to 0 once consumed,
            # which is not a status. Writing status from here strands the frontend
            # on "calibrating" — real status is ATTR_CALIBRATED (0x027B).
            logger.debug(f"[{self.device.ieee}] Motor calibration command echo: {value}")

        # Switch/Relay Attributes
        elif attrid == self.ATTR_OPERATION_MODE:
            mode = "decoupled" if value == 0 else "coupled"
            updates["operation_mode"] = mode
            logger.info(f"[{self.device.ieee}] Operation mode: {mode}")

        elif attrid == self.ATTR_SWITCH_MODE:
            mode_map = {1: "fast", 2: "multi"}
            mode = mode_map.get(value, f"unknown_{value}")
            updates["switch_mode"] = mode
            logger.info(f"[{self.device.ieee}] Switch mode: {mode}")

        elif attrid == self.ATTR_SWITCH_TYPE:
            type_map = {1: "toggle", 2: "momentary"}
            switch_type = type_map.get(value, f"unknown_{value}")
            updates["switch_type"] = switch_type
            logger.info(f"[{self.device.ieee}] Switch type: {switch_type}")

        elif attrid == self.ATTR_INDICATOR_LIGHT:
            mode = "reverse" if value == 1 else "normal"
            updates["indicator_light"] = mode
            logger.info(f"[{self.device.ieee}] Indicator light: {mode}")

        elif attrid == self.ATTR_POWER_OUTAGE_MEM:
            updates["power_outage_memory"] = bool(value)
            logger.info(f"[{self.device.ieee}] Power outage memory: {'on' if value else 'off'}")

        # Motion Sensor Attributes
        elif attrid == self.ATTR_DETECTION_INTERVAL:
            updates["detection_interval"] = value
            logger.info(f"[{self.device.ieee}] Detection interval: {value}s")

        elif attrid == self.ATTR_MOTION_SENSITIVITY:
            sens_map = {1: "low", 2: "medium", 3: "high"}
            sensitivity = sens_map.get(value, f"unknown_{value}")
            updates["motion_sensitivity"] = sensitivity
            logger.info(f"[{self.device.ieee}] Motion sensitivity: {sensitivity}")

        elif attrid == self.ATTR_TRIGGER_INDICATOR:
            updates["trigger_indicator"] = bool(value)
            logger.info(f"[{self.device.ieee}] Trigger indicator: {'on' if value else 'off'}")

        # Common Attributes
        elif attrid == self.ATTR_MODE:
            updates["device_mode"] = value
            logger.debug(f"[{self.device.ieee}] Device mode: {value}")

        elif attrid == self.ATTR_BATTERY_REPLACE:
            updates["battery_low"] = bool(value)
            if value:
                logger.warning(f"[{self.device.ieee}] Battery replacement needed!")

        # Temperature/Humidity Display
        elif attrid == self.ATTR_TEMP_DISPLAY_UNIT:
            unit = "fahrenheit" if value == 1 else "celsius"
            updates["temperature_unit"] = unit
            logger.info(f"[{self.device.ieee}] Temperature unit: {unit}")

        elif attrid == self.ATTR_MEASUREMENT_INTERVAL:
            updates["measurement_interval"] = value
            logger.info(f"[{self.device.ieee}] Measurement interval: {value}s")

        # Xiaomi Structured Attributes (0x00DF and 0x00F7)
        elif attrid in (0x00DC, 0x00DF, 0x00E5, 0x00F7):
            if isinstance(value, (bytes, bytearray)):
                try:
                    parsed = parse_xiaomi_struct(value)

                    for sub_id, sub_value in parsed.items():
                        if sub_id in XIAOMI_ATTR_MAP:
                            attr_name, converter = XIAOMI_ATTR_MAP[sub_id]
                            try:
                                converted_value = converter(sub_value)
                                updates[attr_name] = converted_value
                                logger.info(f"[{self.device.ieee}] {attr_name}={converted_value}")
                            except Exception as e:
                                logger.error(f"[{self.device.ieee}] Error converting {attr_name}: {e}")
                        else:
                            logger.debug(f"[{self.device.ieee}] Unknown Xiaomi sub-attr 0x{sub_id:02X} = {sub_value}")

                    # Clean up raw key if it exists in state from previous runs
                    raw_key = f"opple_0x{attrid:04x}"
                    if raw_key in self.device.state:
                        del self.device.state[raw_key]
                        logger.debug(f"[{self.device.ieee}] Cleaned up stale {raw_key}")

                except Exception as e:
                    logger.error(f"[{self.device.ieee}] Error parsing Xiaomi struct 0x{attrid:04X}: {e}")
            else:
                logger.debug(f"[{self.device.ieee}] Aqara 0x{attrid:04X} non-bytes value: {type(value).__name__}")
                updates[f"opple_0x{attrid:04x}"] = value

        else:
            # Unknown attribute - log for debugging (but don't log 0x00DF/0x00F7 here)
            logger.debug(f"[{self.device.ieee}] Aqara 0xFCC0 unknown attr 0x{attrid:04x} = {value}")
            # Store with opple prefix for visibility
            updates[f"opple_0x{attrid:04x}"] = value

        # Update device state
        if updates:
            self.device.update_state(updates)

    async def configure(self):
        """
        Configure the Aqara manufacturer cluster.

        For TRVs (lumi.airrtc.agl001 / SRTS-A01): write a baseline preset
        and schedule state so the device's internal schedule doesn't fight
        our setpoint writes. Without this, the device alternates between
        our writes (e.g. 21.5°C from heating controller) and its built-in
        schedule (e.g. 5°C "off" period), causing the visible bounce.

          - 0x0272 (preset)         = 0  (manual)
          - 0x027D (schedule)       = 0  (disabled)
        """
        logger.info(f"[{self.device.ieee}] Configuring Aqara manufacturer cluster 0xFCC0")

        if hasattr(self.device, 'hvac'):
            # Best-effort: a sleeping device times out and retries next configure
            # cycle. Swallowed so one failed write cannot abort the whole device
            # config — the controller can still drive it via 0x0201 setpoints.
            for attr_id, value, label in (
                    (self.ATTR_PRESET, 0, "preset=manual"),
                    (self.ATTR_SCHEDULE, 0, "schedule=off"),
            ):
                try:
                    ok = await self.write_attribute(attr_id, value)
                    if ok:
                        logger.info(
                            f"[{self.device.ieee}] TRV baseline: {label}"
                        )
                    else:
                        logger.debug(
                            f"[{self.device.ieee}] TRV baseline {label} not "
                            "applied (device may be asleep — will retry next "
                            "configure)"
                        )
                except Exception as e:
                    logger.debug(
                        f"[{self.device.ieee}] TRV baseline {label} write "
                        f"raised: {e}"
                    )

        logger.info(f"[{self.device.ieee}] Aqara manufacturer cluster configured")
        return True

    def _is_sleepy_end_device(self) -> bool:
        """
        True if the device is a battery-powered sleepy end device.
        Sleepy devices push state unsolicited — we should NOT actively poll.
        Reads done outside the device's wake window time out and clog the
        radio queue, delaying legitimate writes (setpoints, calibrate).
        """
        try:
            zdev = getattr(self.device, "zigpy_dev", None)
            if zdev is None or not getattr(zdev, "node_desc", None):
                return False
            nd = zdev.node_desc
            # logical_type 2 = EndDevice; mac flags & 0x08 = rx_on_when_idle
            is_end_device = int(nd.logical_type) == 2
            rx_on_when_idle = bool(int(nd.mac_capability_flags) & 0x08)
            return is_end_device and not rx_on_when_idle
        except Exception:
            return False

    async def _zcl_with_mfg(self, func, *args):
        """
        Call a cluster ZCL method with the 0x115F manufacturer code across
        zigpy versions
        """
        try:
            return await func(*args, manufacturer=self.MANUFACTURER_CODE)
        except TypeError as e:
            if "manufacturer" not in str(e):
                raise
            return await func(*args, manufacturer_code=self.MANUFACTURER_CODE)

    async def poll(self) -> Dict[str, Any]:
        """
        Poll manufacturer-specific attributes.

        For sleepy end devices: skip entirely. They push reports unsolicited.
        Otherwise: read with manufacturer=0x115F (mandatory — without it the
        zigpy schema lookup misroutes the request and raises a misleading
        cluster-id-shaped error like 513 = 0x0201).
        """
        if self._is_sleepy_end_device():
            logger.debug(
                f"[{self.device.ieee}] Skipping Aqara poll — sleepy end device, "
                "relying on unsolicited reports"
            )
            return {}

        attrs_to_read = [self.ATTR_POWER_OUTAGE_MEM]

        # Add thermostat-specific attributes if we have a thermostat cluster
        if hasattr(self.device, 'hvac'):
            attrs_to_read.extend([
                self.ATTR_SYSTEM_MODE,             # 0x0271
                self.ATTR_PRESET,                  # 0x0272
                self.ATTR_WINDOW_DETECTION,        # 0x0273
                self.ATTR_VALVE_DETECTION,         # 0x0274
                self.ATTR_VALVE_ALARM,             # 0x0275
                self.ATTR_CHILD_LOCK,              # 0x0277
                self.ATTR_AWAY_PRESET_TEMPERATURE, # 0x0279
                self.ATTR_WINDOW_OPEN,             # 0x027A
                self.ATTR_CALIBRATED,              # 0x027B
                # Note: do NOT poll ATTR_MOTOR_CALIBRATION (0x0270) — write-only.
                self.ATTR_SENSOR_TYPE,             # 0x027E
            ])

        # Add motion sensor attributes if we have occupancy cluster
        if hasattr(self.device, 'occupancy'):
            attrs_to_read.extend([
                self.ATTR_DETECTION_INTERVAL,
                self.ATTR_MOTION_SENSITIVITY,
                self.ATTR_TRIGGER_INDICATOR,
            ])

        # Add switch attributes if we have on/off cluster
        if hasattr(self.device, 'on_off'):
            attrs_to_read.extend([
                self.ATTR_OPERATION_MODE,
                self.ATTR_SWITCH_MODE,
                self.ATTR_SWITCH_TYPE,
                self.ATTR_INDICATOR_LIGHT,
            ])

        try:
            logger.debug(
                f"[{self.device.ieee}] Reading Aqara attrs: "
                f"{[hex(a) for a in attrs_to_read]}"
            )
            result = await self._zcl_with_mfg(
                self.cluster.read_attributes, attrs_to_read
            )
            if result and result[0]:
                logger.info(
                    f"[{self.device.ieee}] Aqara poll success: "
                    f"{len(result[0])} attrs"
                )
                for attrid, value in result[0].items():
                    self.attribute_updated(attrid, value)
            if result and result[1]:
                logger.debug(
                    f"[{self.device.ieee}] Aqara poll failures: {result[1]}"
                )
        except Exception as e:
            logger.warning(
                f"[{self.device.ieee}] Aqara manufacturer cluster poll failed: {e}"
            )
        return {}

    async def write_attribute(self, attr_id: int, value: Any) -> bool:
        """
        Write a manufacturer-specific attribute via write_attributes_raw so we
        bypass cluster-schema lookup (which otherwise raises KeyError for Aqara
        proprietary attrs on the bare 0xFCC0 cluster).
        """
        from zigpy import types as t
        from zigpy.zcl import foundation

        target_type = self.ATTR_TYPES.get(attr_id, t.uint8_t)
        type_id = _ZCL_DATATYPE_ID.get(target_type)
        if type_id is None:
            logger.error(
                f"[{self.device.ieee}] No ZCL data-type id mapped for "
                f"{target_type.__name__} (attr 0x{attr_id:04X})"
            )
            return False

        try:
            val_converted = target_type(value)
        except (ValueError, TypeError) as e:
            logger.error(
                f"[{self.device.ieee}] write 0x{attr_id:04X}: cast "
                f"{value!r} -> {target_type.__name__} failed: {e}"
            )
            return False

        # Route Aqara 0x02xx attrs to the Opple cluster (0xFCC0)
        target_cluster = self.cluster
        if attr_id >= 0x0200 and hasattr(self.cluster, "endpoint"):
            opple = self.cluster.endpoint.in_clusters.get(0xFCC0)
            if opple is None:
                logger.warning(
                    f"[{self.device.ieee}] 0x{attr_id:04X} requested but "
                    f"0xFCC0 not present on endpoint"
                )
                return False
            target_cluster = opple

        return await self._send_attr_write(
            target_cluster, attr_id, type_id, val_converted, target_type.__name__
        )

    async def _send_attr_write(self, target_cluster, attr_id: int, type_id: int,
                               value: Any, type_name: str = "") -> bool:
        """Send a single raw attribute write with the manufacturer code and
        check the WriteAttributesResponse status."""
        from zigpy.zcl import foundation

        tv = foundation.TypeValue()
        tv.type = type_id
        tv.value = value
        attr = foundation.Attribute()
        attr.attrid = attr_id
        attr.value = tv

        logger.info(
            f"[{self.device.ieee}] Writing 0x{attr_id:04X}={value!r} "
            f"to cluster 0x{target_cluster.cluster_id:04X} "
            f"(type=0x{type_id:02X} {type_name})"
        )

        try:
            result = await self._zcl_with_mfg(
                target_cluster.write_attributes_raw, [attr]
            )
        except Exception as e:
            logger.error(
                f"[{self.device.ieee}] Write 0x{attr_id:04X} exception: "
                f"{type(e).__name__}: {e}"
            )
            return False

        # Unwrap [[Record, ...]] -> [Record, ...] etc.
        records = result
        while (
                isinstance(records, (list, tuple))
                and len(records) == 1
                and isinstance(records[0], (list, tuple))
        ):
            records = records[0]
        if not isinstance(records, (list, tuple)) or not records:
            logger.warning(
                f"[{self.device.ieee}] Unexpected write result for "
                f"0x{attr_id:04X}: {result!r}"
            )
            return False

        record = records[0]
        status = getattr(record, "status", record)
        try:
            status_int = int(status)
        except (TypeError, ValueError):
            logger.warning(
                f"[{self.device.ieee}] Unparseable write status for "
                f"0x{attr_id:04X}: {status!r}"
            )
            return False

        if status_int == 0:
            logger.info(f"[{self.device.ieee}] ✓ Write 0x{attr_id:04X} succeeded")
            return True
        logger.warning(
            f"[{self.device.ieee}] Write 0x{attr_id:04X} failed: "
            f"status=0x{status_int:02X}"
        )
        return False

    async def read_attribute(self, attr_id: int) -> Any:
        """Read a single attribute with manufacturer code."""
        try:
            result = await self._zcl_with_mfg(
                self.cluster.read_attributes, [attr_id]
            )
            if result and result[0]:
                value = result[0].get(attr_id)
                if value is not None:
                    self.attribute_updated(attr_id, value)
                    return value
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Aqara read 0x{attr_id:04x} failed: {e}")
        return None


    async def apply_configuration(self, updates: Dict[str, Any]):
        """Apply Aqara manufacturer-specific configuration updates."""
        config_map = {
            'power_outage_memory': self.ATTR_POWER_OUTAGE_MEM,
            'window_detection': self.ATTR_WINDOW_DETECTION,
            'child_lock': self.ATTR_CHILD_LOCK,
            'valve_detection': self.ATTR_VALVE_DETECTION,
            'motor_calibration': self.ATTR_MOTOR_CALIBRATION,
            'detection_interval': self.ATTR_DETECTION_INTERVAL,
            'motion_sensitivity': self.ATTR_MOTION_SENSITIVITY,
            'trigger_indicator': self.ATTR_TRIGGER_INDICATOR,
            'operation_mode': self.ATTR_OPERATION_MODE,
            'switch_mode': self.ATTR_SWITCH_MODE,
            'switch_type': self.ATTR_SWITCH_TYPE,
            'indicator_light': self.ATTR_INDICATOR_LIGHT,
        }

        for key, attr_id in config_map.items():
            if key in updates:
                try:
                    success = await self.write_attribute(attr_id, int(updates[key]))
                    if not success:
                        logger.debug(f"[{self.device.ieee}] Device doesn't support {key}")
                except Exception as e:
                    logger.debug(f"[{self.device.ieee}] Skipping unsupported {key}: {e}")

    def get_pollable_attributes(self) -> Dict[int, str]:
        """
        Return pollable attributes based on device type.
        Used by periodic polling if enabled.
        """
        base_attrs = {
            self.ATTR_POWER_OUTAGE_MEM: "power_outage_memory",
        }

        # Add device-specific attributes
        if hasattr(self.device, 'hvac'):  # Thermostat/TRV
            base_attrs.update({
                self.ATTR_SYSTEM_MODE: "system_mode",
                self.ATTR_PRESET: "preset",
                self.ATTR_WINDOW_DETECTION: "window_detection",
                self.ATTR_CHILD_LOCK: "child_lock",
                self.ATTR_VALVE_DETECTION: "valve_detection",
                # 0x0270 is write-only, so read 0x027B. Named "calibrated" (raw) so
                # a base-poll path cannot overwrite the decoded string with an int.
                self.ATTR_CALIBRATED: "calibrated",
                self.ATTR_WINDOW_OPEN: "window_open",
                self.ATTR_VALVE_ALARM: "valve_alarm",
                self.ATTR_SENSOR_TYPE: "sensor_type",
            })

        if hasattr(self.device, 'occupancy'):  # Motion sensor
            base_attrs.update({
                self.ATTR_DETECTION_INTERVAL: "detection_interval",
                self.ATTR_MOTION_SENSITIVITY: "motion_sensitivity",
                self.ATTR_TRIGGER_INDICATOR: "trigger_indicator",
            })

        if hasattr(self.device, 'on_off'):  # Switch/Relay
            base_attrs.update({
                self.ATTR_OPERATION_MODE: "operation_mode",
                self.ATTR_INDICATOR_LIGHT: "indicator_light",
            })

        return base_attrs


    async def set_window_detection(self, enabled: bool):
        await self.write_attribute(
            self.ATTR_WINDOW_DETECTION,
            1 if enabled else 0
        )


    async def set_valve_detection(self, enabled: bool):
        await self.write_attribute(
            self.ATTR_VALVE_DETECTION,
            1 if enabled else 0
        )


    async def start_motor_calibration(self):
        """
        Starts valve motor calibration.
        Takes ~2 minutes, auto-resets to 0.
        """
        await self.write_attribute(
            self.ATTR_MOTOR_CALIBRATION,  # 0x0270, not 0x0279!
            1
        )

    # AGL001 external-sensor protocol (attribute 0xFFF2)

    # pseudo-IEEE as the "virtual" external sensor
    _VIRTUAL_SENSOR_IEEE = bytes.fromhex("00158d00019d1b98")

    @staticmethod
    def _lumi_blob_header(counter: int, params_len: int, action: int) -> bytes:
        header = [0xAA, 0x71, params_len + 3, 0x44, counter]
        integrity = 512 - sum(header)
        return bytes(header + [integrity, action, 0x41, params_len])

    async def _write_fff2(self, payload: bytes) -> bool:
        from zigpy import types as t
        return await self._send_attr_write(
            self.cluster, 0xFFF2, 0x41, t.LVBytes(payload), "LVBytes"
        )

    async def set_sensor_mode(self, external: bool) -> bool:
        """Switch the TRV between internal and external temperature sensor."""
        import time as _time

        dev_ieee = bytes.fromhex(str(self.device.ieee).replace(":", ""))
        ts = int(_time.time()).to_bytes(4, "big")

        if external:
            p1 = (ts + bytes([0x3D, 0x04]) + dev_ieee + self._VIRTUAL_SENSOR_IEEE
                  + bytes([0x00, 0x01, 0x00, 0x55, 0x13, 0x0A, 0x02, 0x00, 0x00,
                           0x64, 0x04, 0xCE, 0xC2, 0xB6, 0xC8, 0x00, 0x00, 0x00,
                           0x00, 0x00, 0x01, 0x3D, 0x64, 0x65]))
            p2 = (ts + bytes([0x3D, 0x05]) + dev_ieee + self._VIRTUAL_SENSOR_IEEE
                  + bytes([0x08, 0x00, 0x07, 0xFD, 0x16, 0x0A, 0x02, 0x0A, 0xC9,
                           0xE8, 0xB1, 0xB8, 0xD4, 0xDA, 0xCF, 0xDF, 0xC0, 0xEB,
                           0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x3D, 0x04, 0x65]))
            action = 0x02
        else:
            p1 = ts + bytes([0x3D, 0x05]) + dev_ieee + bytes(12)
            p2 = ts + bytes([0x3D, 0x04]) + dev_ieee + bytes(12)
            action = 0x04

        ok1 = await self._write_fff2(self._lumi_blob_header(0x12, len(p1), action) + p1)
        ok2 = await self._write_fff2(self._lumi_blob_header(0x13, len(p2), action) + p2)
        return ok1 and ok2

    async def set_external_temperature(self, temp_c: float) -> bool:
        """Push an external temperature reading (sensor must be 'external')."""
        import struct

        if self.device.state.get("sensor_type") != "external":
            logger.debug(
                f"[{self.device.ieee}] Skipping external temp push — "
                "sensor is set to internal"
            )
            return True

        # big-endian float32 of round(°C * 100)
        buf = struct.pack(">f", round(float(temp_c) * 100))
        params = self._VIRTUAL_SENSOR_IEEE + bytes([0x00, 0x01, 0x00, 0x55]) + buf
        return await self._write_fff2(
            self._lumi_blob_header(0x12, len(params), 0x05) + params
        )


    async def process_command(self, command: str, value: Any) -> bool:
        """
        Process commands — returns True on successful device write, False otherwise.

        For sleepy battery TRVs: do NOT issue follow-up reads after writes.
        Each read either succeeds (in-window) or hangs the queue for ~60s
        until APS timeout fires. We rely on the device's own unsolicited
        reports for state confirmation.
        """

        def to_bool_int(val):
            if isinstance(val, str):
                v_lower = val.lower()
                if v_lower in ["lock", "on", "true", "yes", "1", "calibrate"]: return 1
                if v_lower in ["unlock", "off", "false", "no", "0"]: return 0
            return 1 if val else 0

        val_int = to_bool_int(value)

        if command in ("motor_calibration", "calibrate"):
            ok = await self.write_attribute(self.ATTR_MOTOR_CALIBRATION, 1)
            if not ok:
                return False
            # No "in_progress" state exists on agl001 (0x027B is boolean), so do
            # not fake one. Adaptation takes ~10 s and needs the head mounted;
            # re-read the real flag afterwards rather than strand a phantom status.
            import asyncio

            async def _recheck_calibration():
                try:
                    await asyncio.sleep(12)
                    await self.read_attribute(self.ATTR_CALIBRATED)
                except Exception as e:
                    logger.debug(
                        f"[{self.device.ieee}] calibration re-read failed: {e}"
                    )

            asyncio.create_task(_recheck_calibration())
            return True

        elif command == "system_mode":
            # AGL001 firmware silently ignores standard ZCL 0x0201/0x001C
            # system_mode writes — only Aqara 0x0271 actually flips the device.
            if isinstance(value, str):
                sv = value.strip().lower()
                mode_int = 1 if sv in ("heat", "1", "on", "true") else 0
            else:
                mode_int = 1 if int(value) == 1 else 0
            ok = await self.write_attribute(self.ATTR_SYSTEM_MODE, mode_int)
            if ok:
                self.device.update_state({
                    "system_mode": "heat" if mode_int else "off"
                })
            return ok

        elif command == "preset":
            if isinstance(value, str):
                p_map = {"manual": 0, "auto": 1, "away": 2}
                p_int = p_map.get(value.strip().lower())
                if p_int is None:
                    logger.error(
                        f"[{self.device.ieee}] preset: unknown value {value!r}"
                    )
                    return False
            else:
                p_int = int(value)
                if p_int not in (0, 1, 2):
                    logger.error(
                        f"[{self.device.ieee}] preset: out-of-range {p_int}"
                    )
                    return False
            ok = await self.write_attribute(self.ATTR_PRESET, p_int)
            if ok:
                self.device.update_state({
                    "preset": {0: "manual", 1: "auto", 2: "away"}[p_int]
                })
            return ok

        elif command == "away_preset_temperature":
            try:
                temp_c = float(value)
            except (TypeError, ValueError):
                logger.error(
                    f"[{self.device.ieee}] away_preset_temperature: invalid {value!r}"
                )
                return False
            temp_c = max(5.0, min(30.0, temp_c))
            return await self.write_attribute(
                self.ATTR_AWAY_PRESET_TEMPERATURE, int(round(temp_c * 100))
            )

        elif command == "schedule":
            return await self.write_attribute(self.ATTR_SCHEDULE, val_int)

        elif command == "window_detection":
            return await self.write_attribute(self.ATTR_WINDOW_DETECTION, val_int)

        elif command == "valve_detection":
            return await self.write_attribute(self.ATTR_VALVE_DETECTION, val_int)

        elif command == "child_lock":
            return await self.write_attribute(self.ATTR_CHILD_LOCK, val_int)

        elif command == "external_temp":
            # Pushed via the 0xFFF2 blob protocol — NOT an attribute write.
            try:
                temp_c = float(value)
            except (TypeError, ValueError):
                logger.error(
                    f"[{self.device.ieee}] external_temp: invalid value {value!r}"
                )
                return False
            temp_c = max(-40.0, min(80.0, temp_c))
            return await self.set_external_temperature(temp_c)

        elif command == "sensor_type":
            # Accepts 'internal'/'external', 0/1 or bool. Switching goes through
            # the 0xFFF2 blob protocol — 0x027E is read-back only.
            if isinstance(value, str):
                sv = value.strip().lower()
                external = sv in ("external", "1", "true", "on", "yes")
            else:
                external = bool(value)
            ok = await self.set_sensor_mode(external)
            if ok:
                self.device.update_state({
                    "sensor_type": "external" if external else "internal"
                })
            return ok

        logger.debug(f"[{self.device.ieee}] Unhandled Aqara command: {command}")
        return False


    def get_configuration_options(self) -> List[Dict]:
        """
        Expose Aqara manufacturer-specific settings to the UI.
        Returns configuration options based on device capabilities.
        """
        options = []

        # Thermostat/TRV Configuration
        if hasattr(self.device, 'hvac'):
            options.extend([
                {
                    "name": "window_detection",
                    "label": "Window Detection",
                    "type": "select",
                    "options": [
                        {"value": 0, "label": "Disabled"},
                        {"value": 1, "label": "Enabled"}
                    ],
                    "description": "Automatically turn off heating when window is detected open",
                    "attribute_id": self.ATTR_WINDOW_DETECTION,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "child_lock",
                    "label": "Child Lock",
                    "type": "select",
                    "options": [
                        {"value": 0, "label": "Unlocked"},
                        {"value": 1, "label": "Locked"}
                    ],
                    "description": "Lock physical controls on device",
                    "attribute_id": self.ATTR_CHILD_LOCK,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "valve_detection",
                    "label": "Valve Detection",
                    "type": "select",
                    "options": [
                        {"value": 0, "label": "Disabled"},
                        {"value": 1, "label": "Enabled"}
                    ],
                    "description": "Detect and report valve errors",
                    "attribute_id": self.ATTR_VALVE_DETECTION,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "motor_calibration",
                    "label": "Valve Calibration",
                    "type": "button",
                    "action_value": 1,
                    "description": "Calibrate valve motor (takes ~2 minutes)",
                    "attribute_id": self.ATTR_MOTOR_CALIBRATION,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "sensor_type",
                    "label": "Temperature Sensor",
                    "type": "select",
                    "options": [
                        {"value": 0, "label": "Internal Sensor"},
                        {"value": 1, "label": "External Sensor"}
                    ],
                    "description": "Use internal or external temperature sensor",
                    "attribute_id": self.ATTR_SENSOR_TYPE,
                    "manufacturer_code": self.MANUFACTURER_CODE
                }
            ])

        # Motion Sensor Configuration
        if hasattr(self.device, 'occupancy'):
            options.extend([
                {
                    "name": "detection_interval",
                    "label": "Detection Interval",
                    "type": "number",
                    "min": 5,
                    "max": 300,
                    "unit": "seconds",
                    "description": "Minimum time between motion detections",
                    "attribute_id": self.ATTR_DETECTION_INTERVAL,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "motion_sensitivity",
                    "label": "Motion Sensitivity",
                    "type": "select",
                    "options": [
                        {"value": 1, "label": "Low"},
                        {"value": 2, "label": "Medium"},
                        {"value": 3, "label": "High"}
                    ],
                    "description": "Motion detection sensitivity",
                    "attribute_id": self.ATTR_MOTION_SENSITIVITY,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "trigger_indicator",
                    "label": "LED Indicator",
                    "type": "select",
                    "options": [
                        {"value": 0, "label": "Off"},
                        {"value": 1, "label": "On"}
                    ],
                    "description": "Flash LED when motion detected",
                    "attribute_id": self.ATTR_TRIGGER_INDICATOR,
                    "manufacturer_code": self.MANUFACTURER_CODE
                }
            ])

        # Switch/Relay Configuration
        if hasattr(self.device, 'on_off'):
            options.extend([
                {
                    "name": "operation_mode",
                    "label": "Operation Mode",
                    "type": "select",
                    "options": [
                        {"value": 0, "label": "Decoupled (Switch independent)"},
                        {"value": 1, "label": "Coupled (Switch controls relay)"}
                    ],
                    "description": "Decoupled mode allows switch to trigger automations without controlling relay",
                    "attribute_id": self.ATTR_OPERATION_MODE,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "switch_mode",
                    "label": "Switch Mode",
                    "type": "select",
                    "options": [
                        {"value": 1, "label": "Fast (Quick response)"},
                        {"value": 2, "label": "Multi (Support multi-press)"}
                    ],
                    "description": "Fast mode for immediate response, Multi for detecting double/triple press",
                    "attribute_id": self.ATTR_SWITCH_MODE,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "switch_type",
                    "label": "Switch Type",
                    "type": "select",
                    "options": [
                        {"value": 1, "label": "Toggle"},
                        {"value": 2, "label": "Momentary"}
                    ],
                    "attribute_id": self.ATTR_SWITCH_TYPE,
                    "manufacturer_code": self.MANUFACTURER_CODE
                },
                {
                    "name": "indicator_light",
                    "label": "Indicator Light",
                    "type": "select",
                    "options": [
                        {"value": 0, "label": "Normal (On when relay on)"},
                        {"value": 1, "label": "Reverse (On when relay off)"}
                    ],
                    "attribute_id": self.ATTR_INDICATOR_LIGHT,
                    "manufacturer_code": self.MANUFACTURER_CODE
                }
            ])

        # Common Configuration
        options.append({
            "name": "power_outage_memory",
            "label": "Power Outage Memory",
            "type": "select",
            "options": [
                {"value": 0, "label": "Off (Reset to default)"},
                {"value": 1, "label": "On (Remember last state)"}
            ],
            "description": "Remember device state after power loss",
            "attribute_id": self.ATTR_POWER_OUTAGE_MEM,
            "manufacturer_code": self.MANUFACTURER_CODE
        })

        return options


    async def discover_attributes(self):
        """Discover what TRV-relevant attributes this device actually supports."""
        logger.info(f"[{self.device.ieee}] Discovering Aqara cluster attributes...")
        try:
            attrs_to_check = [
                (self.ATTR_SYSTEM_MODE,              "system_mode"),
                (self.ATTR_PRESET,                   "preset"),
                (self.ATTR_WINDOW_DETECTION,         "window_detection"),
                (self.ATTR_VALVE_DETECTION,          "valve_detection"),
                (self.ATTR_VALVE_ALARM,              "valve_alarm"),
                (self.ATTR_CHILD_LOCK,               "child_lock"),
                (self.ATTR_AWAY_PRESET_TEMPERATURE,  "away_preset_temperature"),
                (self.ATTR_WINDOW_OPEN,              "window_open"),
                (self.ATTR_CALIBRATED,               "calibrated"),
                (self.ATTR_SENSOR_TYPE,              "sensor_type"),
                (self.ATTR_EXTERNAL_TEMP,            "external_temperature"),
                (self.ATTR_BATTERY_PCT,              "battery_pct"),
            ]
            for attr_id, attr_name in attrs_to_check:
                try:
                    result = await self._zcl_with_mfg(
                        self.cluster.read_attributes, [attr_id]
                    )
                    logger.info(f"[{self.device.ieee}] Attr 0x{attr_id:04X} ({attr_name}): {result}")
                except Exception as e:
                    logger.warning(f"[{self.device.ieee}] Attr 0x{attr_id:04X} ({attr_name}) not supported: {e}")
        except Exception as e:
            logger.error(f"[{self.device.ieee}] Discovery failed: {e}")


    def get_discovery_configs(self) -> List[Dict]:
        """Generate Home Assistant discovery configs for Aqara features."""
        configs = []

        # Only expose TRV features if device has HVAC capability
        if hasattr(self.device, 'hvac') or any(h.CLUSTER_ID == 0x0201 for h in self.device.handlers.values()):

            # READ-ONLY STATUS SENSORS
            configs.extend([
                {
                    "component": "binary_sensor",
                    "object_id": "window_open",
                    "config": {
                        "name": "Window Open",
                        "device_class": "window",
                        "value_template": "{{ value_json.window_open | default(false) }}",
                        "payload_on": True,
                        "payload_off": False
                    }
                },
                {
                    "component": "binary_sensor",
                    "object_id": "valve_alarm",
                    "config": {
                        "name": "Valve Alarm",
                        "device_class": "problem",
                        "value_template": "{{ value_json.valve_alarm | default(false) }}",
                        "payload_on": True,
                        "payload_off": False
                    }
                }
            ])

            # CONFIGURATION CONTROLS (Switches)
            configs.extend([
                {
                    "component": "switch",
                    "object_id": "window_detection",
                    "config": {
                        "name": "Window Detection",
                        "icon": "mdi:window-open-variant",
                        "entity_category": "config",
                        "value_template": "{{ value_json.window_detection | default(false) }}",
                        "command_topic": "CMD_TOPIC_PLACEHOLDER",
                        "command_template": '{"command": "window_detection", "value": {{ 1 if value == "ON" else 0 }}}'
                    }
                },
                {
                    "component": "switch",
                    "object_id": "valve_detection",
                    "config": {
                        "name": "Valve Detection",
                        "icon": "mdi:pipe-valve",
                        "entity_category": "config",
                        "value_template": "{{ value_json.valve_detection | default(false) }}",
                        "command_topic": "CMD_TOPIC_PLACEHOLDER",
                        "command_template": '{"command": "valve_detection", "value": {{ 1 if value == "ON" else 0 }}}'
                    }
                },
                {
                    "component": "switch",
                    "object_id": "child_lock",
                    "config": {
                        "name": "Child Lock",
                        "icon": "mdi:lock",
                        "entity_category": "config",
                        "value_template": "{{ value_json.child_lock | default(false) }}",
                        "command_topic": "CMD_TOPIC_PLACEHOLDER",
                        "command_template": '{"command": "child_lock", "value": {{ 1 if value == "ON" else 0 }}}'
                    }
                },
                {
                    "component": "button",
                    "object_id": "motor_calibration",
                    "config": {
                        "name": "Calibrate Valve",
                        "icon": "mdi:wrench",
                        "entity_category": "config",
                        "command_topic": "CMD_TOPIC_PLACEHOLDER",
                        "command_template": '{"command": "motor_calibration", "value": 1}'
                    }
                }
            ])

        return configs