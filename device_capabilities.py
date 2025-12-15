"""
Device Capabilities Tracker - Production Implementation

This module provides capability detection for devices to prevent
handlers from updating irrelevant state fields.

"""

from typing import Set, Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


class DeviceCapabilities:
    """
    Detects and tracks device capabilities based on clusters.
    Does NOT filter state updates - that's the frontend's job.
    """
    
    # === CLUSTER IDs - COMPLETE LIST ===
    BASIC = 0x0000
    POWER_CONFIGURATION = 0x0001
    DEVICE_TEMPERATURE = 0x0002
    IDENTIFY = 0x0003
    GROUPS = 0x0004
    SCENES = 0x0005
    ON_OFF = 0x0006
    ON_OFF_CONFIGURATION = 0x0007
    LEVEL_CONTROL = 0x0008
    ALARMS = 0x0009
    TIME = 0x000A
    ANALOG_INPUT = 0x000C
    ANALOG_OUTPUT = 0x000D
    ANALOG_VALUE = 0x000E
    BINARY_INPUT = 0x000F
    BINARY_OUTPUT = 0x0010
    BINARY_VALUE = 0x0011
    MULTISTATE_INPUT = 0x0012
    MULTISTATE_OUTPUT = 0x0013
    MULTISTATE_VALUE = 0x0014
    
    # Closures
    SHADE_CONFIGURATION = 0x0100
    DOOR_LOCK = 0x0101
    WINDOW_COVERING = 0x0102
    
    # HVAC
    THERMOSTAT = 0x0201
    FAN_CONTROL = 0x0202
    DEHUMIDIFICATION_CONTROL = 0x0203
    THERMOSTAT_UI = 0x0204
    
    # Lighting
    COLOR_CONTROL = 0x0300
    BALLAST_CONFIGURATION = 0x0301
    
    # Measurement & Sensing
    ILLUMINANCE_MEASUREMENT = 0x0400
    ILLUMINANCE_LEVEL_SENSING = 0x0401
    TEMPERATURE_MEASUREMENT = 0x0402
    PRESSURE_MEASUREMENT = 0x0403
    FLOW_MEASUREMENT = 0x0404
    RELATIVE_HUMIDITY = 0x0405
    OCCUPANCY_SENSING = 0x0406
    LEAF_WETNESS = 0x0407
    SOIL_MOISTURE = 0x0408
    CO2_MEASUREMENT = 0x040D
    PM25_MEASUREMENT = 0x042A
    
    # Security & Safety
    IAS_ZONE = 0x0500
    IAS_ACE = 0x0501
    IAS_WD = 0x0502
    
    # Smart Energy
    PRICE = 0x0700
    DEMAND_RESPONSE = 0x0701
    METERING = 0x0702
    MESSAGING = 0x0703
    TUNNELING = 0x0704
    PREPAYMENT = 0x0705
    ENERGY_MANAGEMENT = 0x0706
    
    # Lighting Link (Touchlink)
    TOUCHLINK = 0x1000
    
    # Measurement & Sensing (cont.)
    ELECTRICAL_MEASUREMENT = 0x0B04
    DIAGNOSTICS = 0x0B05
    
    # Manufacturer Specific
    TUYA_MANUFACTURER = 0xEF00
    XIAOMI_MANUFACTURER = 0xFCC0
    XIAOMI_AQARA = 0xFCC0
    PHILIPS_MANUFACTURER = 0xFC00
    
    # === STATE FIELD CATEGORIES ===
    # These define which fields belong to which capability
    
    MOTION_FIELDS = {
        'motion', 'occupancy', 'presence', 
        'motion_on_time', 'motion_timeout', 'sensitivity',
        'pir_o_to_u_delay', 'pir_u_to_o_delay', 'pir_u_to_o_threshold'
    }
    
    CONTACT_FIELDS = {
        'contact', 'is_open', 'is_closed'
    }
    
    IAS_ZONE_FIELDS = {
        'zone_status', 'tamper', 'battery_low', 'trouble', 
        'water_leak', 'smoke', 'co_detected', 'vibration', 'alarm'
    }
    
    LIGHTING_FIELDS = {
        'state', 'on', 'brightness', 'level', 
        'color_temp', 'color_temp_kelvin', 'color_temp_startup',
        'color_x', 'color_y', 'hue', 'saturation',
        'color_mode', 'enhanced_hue', 'color_loop_active',
        'color_loop_direction', 'color_loop_time'
    }
    
    HVAC_FIELDS = {
        'local_temperature', 'occupied_heating_setpoint', 
        'occupied_cooling_setpoint', 'unoccupied_heating_setpoint',
        'unoccupied_cooling_setpoint', 'min_heat_setpoint_limit',
        'max_heat_setpoint_limit', 'min_cool_setpoint_limit',
        'max_cool_setpoint_limit', 'system_mode', 'running_mode', 
        'running_state', 'pi_heating_demand', 'pi_cooling_demand',
        'hvac_system_type', 'thermostat_programming_operation_mode',
        'occupied_setback', 'unoccupied_setback', 'setpoint_change_source',
        'valve_position', 'window_detection', 'child_lock', 'away_mode'
    }
    
    POWER_FIELDS = {
        'power', 'voltage', 'current', 'energy', 
        'power_factor', 'reactive_power', 'apparent_power',
        'rms_voltage', 'rms_current', 'active_power',
        'ac_frequency', 'power_divisor', 'power_multiplier'
    }
    
    ENVIRONMENTAL_FIELDS = {
        'temperature', 'humidity', 'pressure', 
        'illuminance', 'illuminance_lux', 'co2', 'pm25',
        'voc', 'formaldehyde', 'air_quality', 'soil_moisture'
    }
    
    BATTERY_FIELDS = {
        'battery', 'battery_voltage', 'battery_percentage_remaining',
        'battery_size', 'battery_quantity', 'battery_alarm_mask'
    }
    
    COVER_FIELDS = {
        'position', 'tilt', 'lift_percentage', 'tilt_percentage',
        'current_position_lift_percentage', 'current_position_tilt_percentage',
        'cover_position'
    }
    
    TUYA_FIELDS = {
        'radar_sensitivity', 'presence_sensitivity', 'keep_time',
        'distance', 'detection_distance_min', 'detection_distance_max',
        'fading_time', 'self_test', 'target_distance'
    }
    
    # Fields that are ALWAYS allowed regardless of capability
    UNIVERSAL_FIELDS = {
        'last_seen', 'power_source', 'manufacturer', 'model', 
        'available', 'lqi', 'sw_version', 'date_code',
        'application_version', 'stack_version', 'hw_version',
        'manufacturer_id', 'power_source_raw', 'device_type',
        'linkquality', 'update_available', 'update_state', 'action'
    }

    def __init__(self, device):
        """
        Initialize capabilities for a device.
        
        Args:
            device: ZHADevice instance
        """
        self.device = device
        self._capabilities: Set[str] = set()
        self._cluster_ids: Set[int] = set()
        self._detect_capabilities()

        caps_str = ', '.join(sorted(self._capabilities)) if self._capabilities else 'none'
        logger.info(f"[{self.device.ieee}] Detected capabilities: {caps_str}")

    def _detect_capabilities(self):
        """Detect device capabilities based on attached handlers and clusters."""
        capabilities = set()
        cluster_ids = set()
        
        # Examine all handlers to determine capabilities
        for handler_key, handler in self.device.handlers.items():
            # Handler keys can be (ep_id, cluster_id) or just cluster_id
            if isinstance(handler_key, tuple):
                ep_id, cluster_id = handler_key
            else:
                cluster_id = handler_key

            cluster_ids.add(cluster_id)
            
            # === MOTION & OCCUPANCY SENSING ===
            if cluster_id == self.OCCUPANCY_SENSING:
                capabilities.add('occupancy_sensing')
                capabilities.add('motion_sensor')
            
            # === IAS ZONE (Security Sensors) ===
            elif cluster_id == self.IAS_ZONE:
                capabilities.add('ias_zone')
                
                # Try to determine specific IAS zone type
                if hasattr(handler, '_zone_type'):
                    zone_type = handler._zone_type
                    if zone_type in [0x000D, 0x0000]:  # Motion sensor
                        capabilities.add('motion_sensor')
                    elif zone_type == 0x0015:  # Contact switch
                        capabilities.add('contact_sensor')
                    elif zone_type == 0x002A:  # Water sensor
                        capabilities.add('water_sensor')
                    elif zone_type == 0x0028:  # Fire/smoke
                        capabilities.add('smoke_sensor')
                    elif zone_type == 0x002B:  # CO sensor
                        capabilities.add('co_sensor')
                    elif zone_type == 0x002C:  # Vibration
                        capabilities.add('vibration_sensor')
            
            # === LIGHTING ===
            elif cluster_id == self.ON_OFF:
                # Check if this is a Philips motion sensor (uses OnOff for motion)
                manufacturer = str(self.device.zigpy_dev.manufacturer or "").lower()
                model = str(self.device.zigpy_dev.model or "").lower()
                
                if ("philips" in manufacturer or "signify" in manufacturer) and "sml" in model:
                    capabilities.add('motion_sensor')
                else:
                    # Check if it's a contact sensor (OnOff in outputs, minimal clusters)
                    ep = handler.endpoint if hasattr(handler, 'endpoint') else None
                    if ep:
                        onoff_is_output = 0x0006 in [c.cluster_id for c in ep.out_clusters.values()]
                        has_lighting = any(cid in [c.cluster_id for c in ep.in_clusters.values()] 
                                         for cid in [0x0008, 0x0300, 0x1000])  # Level, Color, LightLink
                        input_count = len(ep.in_clusters)
                        
                        # Contact sensor if OnOff in outputs and no lighting clusters
                        if onoff_is_output and not has_lighting and input_count <= 6:
                            capabilities.add('contact_sensor')
                        else:
                            capabilities.add('on_off')
                            capabilities.add('light')
                    else:
                        capabilities.add('on_off')
                        capabilities.add('light')
            
            elif cluster_id == self.LEVEL_CONTROL:
                capabilities.add('level_control')
                capabilities.add('light')

            elif cluster_id == self.COLOR_CONTROL:
                capabilities.add('color_control')
                capabilities.add('light')
            
            # === HVAC ===
            elif cluster_id == self.THERMOSTAT:
                capabilities.add('thermostat')
                capabilities.add('hvac')

            elif cluster_id == self.FAN_CONTROL:
                capabilities.add('fan_control')
                capabilities.add('hvac')
            
            # === ENVIRONMENTAL SENSORS ===
            elif cluster_id == self.TEMPERATURE_MEASUREMENT:
                capabilities.add('temperature_sensor')
                capabilities.add('environmental_sensor')

            elif cluster_id == self.RELATIVE_HUMIDITY:
                capabilities.add('humidity_sensor')
                capabilities.add('environmental_sensor')

            elif cluster_id == self.PRESSURE_MEASUREMENT:
                capabilities.add('pressure_sensor')
                capabilities.add('environmental_sensor')

            elif cluster_id == self.ILLUMINANCE_MEASUREMENT:
                capabilities.add('illuminance_sensor')
                capabilities.add('environmental_sensor')
            
            elif cluster_id == self.CO2_MEASUREMENT:
                capabilities.add('co2_sensor')
                capabilities.add('environmental_sensor')
            
            elif cluster_id == self.PM25_MEASUREMENT:
                capabilities.add('pm25_sensor')
                capabilities.add('environmental_sensor')
            
            # === POWER & ENERGY ===
            elif cluster_id == self.POWER_CONFIGURATION:
                capabilities.add('battery')

            elif cluster_id == self.ELECTRICAL_MEASUREMENT:
                capabilities.add('power_monitoring')
                capabilities.add('electrical_measurement')

            elif cluster_id == self.METERING:
                capabilities.add('metering')
                capabilities.add('energy_monitoring')

            # === WINDOW COVERING / BLINDS ===
            elif cluster_id == self.WINDOW_COVERING:
                capabilities.add('window_covering')
                capabilities.add('cover')

            # === MANUFACTURER SPECIFIC ===
            elif cluster_id == self.TUYA_MANUFACTURER:
                capabilities.add('tuya')

                # Check if it's a Tuya radar/presence sensor
                manufacturer = str(self.device.zigpy_dev.manufacturer or "")
                if '_TZE' in manufacturer:
                    model = str(self.device.zigpy_dev.model or "")
                    if model == 'TS0601':
                        # Many TS0601 are radar/presence sensors
                        capabilities.add('presence_sensor')
                        capabilities.add('radar_sensor')

            elif cluster_id == self.XIAOMI_MANUFACTURER:
                capabilities.add('xiaomi')

        self._capabilities = capabilities
        self._cluster_ids = cluster_ids

    def has_capability(self, capability: str) -> bool:
        """
        Check if device has a specific capability.

        Args:
            capability: Capability name (e.g., 'motion_sensor', 'thermostat')

        Returns:
            True if device has the capability
        """
        return capability in self._capabilities

    def has_cluster(self, cluster_id: int) -> bool:
        """
        Check if device has a specific cluster.

        Args:
            cluster_id: Zigbee cluster ID

        Returns:
            True if device has the cluster
        """
        return cluster_id in self._cluster_ids

    def get_capabilities(self) -> Set[str]:
        """Get all detected capabilities."""
        return self._capabilities.copy()

    def get_info(self) -> Dict[str, Any]:
        """
        Get capabilities info for API/debugging.

        Returns:
            Dictionary with capabilities and cluster information
        """
        return {
            "capabilities": sorted(list(self._capabilities)),
            "clusters": [f"0x{cid:04X}" for cid in sorted(self._cluster_ids)]
        }

    def allows_field(self, field_name: str) -> bool:
        """
        Check if a state field is allowed for this device.

        Returns True if the field is allowed, False if it should be filtered out.
        """
        # Always allow universal fields
        if field_name in self.UNIVERSAL_FIELDS:
            return True

        # Always allow raw values
        if field_name.endswith('_raw'):
            return True

        # Always allow Tuya DP fields
        if field_name.startswith('dp_'):
            return True

        # Always allow startup behavior fields
        if field_name.startswith('startup_behavior_'):
            return True

        # Allow endpoint-specific fields (e.g., 'on_1', 'brightness_2')
        if '_' in field_name:
            parts = field_name.rsplit('_', 1)
            if len(parts) == 2 and parts[1].isdigit():
                # This is an endpoint-specific field, check the base name
                base_name = parts[0]
                # Recursively check if base name is allowed
                return self.allows_field(base_name)

        # === CAPABILITY-SPECIFIC FIELD FILTERING ===

        # Motion/occupancy fields require motion sensing capability
        if field_name in self.MOTION_FIELDS:
            return self.has_capability('motion_sensor') or \
                   self.has_capability('occupancy_sensing') or \
                   self.has_capability('presence_sensor') or \
                   self.has_capability('radar_sensor')

        # Contact fields require contact sensing
        if field_name in self.CONTACT_FIELDS:
            # Note: 'is_open'/'is_closed' can also be for covers
            if self.has_capability('contact_sensor'):
                return True
            if field_name in {'is_open', 'is_closed'} and self.has_capability('cover'):
                return True
            return False

        # IAS Zone fields require IAS Zone cluster
        if field_name in self.IAS_ZONE_FIELDS:
            return self.has_capability('ias_zone')

        # Lighting fields require on/off or level/color control
        if field_name in self.LIGHTING_FIELDS:
            return self.has_capability('on_off') or \
                   self.has_capability('level_control') or \
                   self.has_capability('color_control') or \
                   self.has_capability('light')

        # HVAC fields require thermostat or fan control
        if field_name in self.HVAC_FIELDS:
            return self.has_capability('thermostat') or \
                   self.has_capability('hvac') or \
                   self.has_capability('fan_control')

        # Power/energy fields require power monitoring
        if field_name in self.POWER_FIELDS:
            return self.has_capability('power_monitoring') or \
                   self.has_capability('electrical_measurement') or \
                   self.has_capability('metering')

        # Environmental fields require appropriate sensors
        if field_name in self.ENVIRONMENTAL_FIELDS:
            return self.has_capability('environmental_sensor') or \
                   self.has_capability('temperature_sensor') or \
                   self.has_capability('humidity_sensor') or \
                   self.has_capability('pressure_sensor') or \
                   self.has_capability('illuminance_sensor') or \
                   self.has_capability('co2_sensor') or \
                   self.has_capability('pm25_sensor')

        # Battery fields require battery capability
        if field_name in self.BATTERY_FIELDS:
            return self.has_capability('battery')

        # Cover fields require window covering capability
        if field_name in self.COVER_FIELDS:
            return self.has_capability('cover') or \
                   self.has_capability('window_covering')

        # Tuya-specific fields require Tuya capability
        if field_name in self.TUYA_FIELDS:
            return self.has_capability('tuya')

        # Allow unknown fields by default for extensibility
        # But log them at DEBUG level for awareness
        logger.debug(f"[{self.device.ieee}] Unknown field '{field_name}' - allowing")
        return True

    def filter_state_update(self, state_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        Filter a state update dictionary to only include allowed fields.

        This is called by the device's update_state() method to ensure
        only relevant fields are stored.

        Args:
            state_dict: Dictionary of state updates

        Returns:
            Filtered dictionary with only allowed fields
        """
        filtered = {}
        blocked_fields = []

        for key, value in state_dict.items():
            if self.allows_field(key):
                filtered[key] = value
            else:
                blocked_fields.append(key)

        if blocked_fields:
            logger.debug(
                f"[{self.device.ieee}] Filtered out {len(blocked_fields)} "
                f"irrelevant fields: {', '.join(blocked_fields)}"
            )

        return filtered