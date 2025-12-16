"""
ZHA Device Wrapper - Handles cluster handlers and state management.
Based on ZHA's device architecture.
"""
import time
import logging
import asyncio
from typing import Dict, Any, Optional, List

# Import the registry from handlers package
from error_handler import with_retries, CommandWrapper
from device_capabilities import DeviceCapabilities
from zigpy.zcl.clusters.general import Basic

# Import handlers to trigger registration decorators
from handlers.base import HANDLER_REGISTRY
from handlers.security import *
from handlers.basic import *
from handlers.switches import *
from handlers.power import *
from handlers.hvac import *
from handlers.sensors import *
from handlers.tuya import *
from handlers.blinds import *
from handlers.aqara import *
from handlers.lightlink import *
from handlers.lighting import *

logger = logging.getLogger("device")

# How long before a device is considered unavailable (5 minutes)
CONSIDER_UNAVAILABLE_BATTERY = 60 * 60 * 6  # 6 hours for battery devices
CONSIDER_UNAVAILABLE_MAINS = 60 * 60 * 2    # 2 hours for mains-powered devices


class ZHADevice:
    """
    Wrapper around a zigpy device that manages cluster handlers
    and state aggregation.
    """

    def __init__(self, service, zigpy_dev):
        self.service = service
        self.zigpy_dev = zigpy_dev
        self.ieee = str(zigpy_dev.ieee)

        # Handlers stored by (endpoint_id, cluster_id) tuple
        self.handlers: Dict[Any, Any] = {}

        self.state: Dict[str, Any] = {}

        # Initialize to 0 so devices appear Offline until they communicate
        self.last_seen = 0

        self.quirk_name = "None"
        self._available = True

        # Track sources of attributes to detect duplicates
        self._attribute_sources: Dict[str, Dict[int, float]] = {}

        # User preferences for specific endpoints (loaded from settings)
        self._preferred_endpoints: Dict[str, int] = {}

        # Command wrapper for resilient operations
        self._cmd_wrapper = None  # Will be initialized after device is ready

        # Check if quirk is applied
        if hasattr(zigpy_dev, 'quirk_class'):
            self.quirk_name = zigpy_dev.quirk_class.__name__

        # Identify and attach handlers
        self._identify_handlers()

        # Initialize Capabilities Logic (The new robust implementation)
        self.capabilities = DeviceCapabilities(self)

        # Initialize basic info from Zigpy device, default to None if missing
        self.manufacturer = zigpy_dev.manufacturer
        self.model = zigpy_dev.model

        # --- Ensure Manufacturer/Model are in self.state for the cache ---
        if self.manufacturer:
            self.state["manufacturer"] = self.manufacturer
        else:
            self.state["manufacturer"] = "Unknown"

        if self.model:
            self.state["model"] = self.model
        else:
            self.state["model"] = "Unknown"

        # Initialize command wrapper
        try:
            self._cmd_wrapper = CommandWrapper(self)
        except Exception as e:
            logger.debug(f"[{self.ieee}] Could not create command wrapper: {e}")

        # Load preferred endpoints from settings if available
        if self.ieee in self.service.device_settings:
            settings = self.service.device_settings[self.ieee]
            if 'preferred_endpoints' in settings:
                self._preferred_endpoints = settings['preferred_endpoints']

        # Robustly fetch Manufacturer/Model if missing on startup
        if self.manufacturer is None or self.model is None:
            self._schedule_basic_info_query()

        logger.info(f"[{self.ieee}] Device wrapper created - "
                    f"Model: {zigpy_dev.model}, Manufacturer: {zigpy_dev.manufacturer}, "
                    f"Quirk: {self.quirk_name}")

    def _schedule_basic_info_query(self):
        """Schedule a background task to query basic info."""
        asyncio.create_task(self._query_basic_info())

    async def _query_basic_info(self):
        """Attempts to query the Basic cluster for manufacturer/model."""
        try:
            for ep_id, ep in self.zigpy_dev.endpoints.items():
                if ep_id == 0: continue
                if Basic.cluster_id in ep.in_clusters:
                    # Attr 0x0004=Manuf, 0x0005=Model
                    results = await ep.basic.read_attributes([0x0004, 0x0005])

                    updates = {}
                    if 0x0004 in results[0]:
                        self.manufacturer = results[0][0x0004]
                        self.zigpy_dev.manufacturer = self.manufacturer
                        updates["manufacturer"] = self.manufacturer

                    if 0x0005 in results[0]:
                        self.model = results[0][0x0005]
                        self.zigpy_dev.model = self.model
                        updates["model"] = self.model

                    if updates:
                        logger.info(f"[{self.ieee}] Resolved Info: {self.manufacturer} / {self.model}")
                        # Re-detect capabilities in case quirks apply now
                        self.capabilities._detect_capabilities()
                        # Update state so cache gets the new info
                        self.update_state(updates)
                    break
        except Exception as e:
            logger.warning(f"[{self.ieee}] Failed basic info query: {e}")

    def get_capabilities_info(self) -> Dict[str, Any]:
        """
        Get device capabilities information for debugging/UI display.
        """
        if not hasattr(self, 'capabilities'):
            return {
                "error": "Capabilities not initialized",
                "capabilities": [],
                "clusters": []
            }

        return {
            "capabilities": sorted(list(self.capabilities.get_capabilities())),
            "clusters": [f"0x{cid:04X}" for cid in sorted(self.capabilities._cluster_ids)],
            "allowed_field_categories": {
                "motion": list(DeviceCapabilities.MOTION_FIELDS) if self.capabilities.has_capability(
                    'motion_sensor') else [],
                "lighting": list(DeviceCapabilities.LIGHTING_FIELDS) if self.capabilities.has_capability(
                    'light') else [],
                "hvac": list(DeviceCapabilities.HVAC_FIELDS) if self.capabilities.has_capability('hvac') else [],
                "environmental": list(DeviceCapabilities.ENVIRONMENTAL_FIELDS) if self.capabilities.has_capability(
                    'environmental_sensor') else [],
                "power": list(DeviceCapabilities.POWER_FIELDS) if self.capabilities.has_capability(
                    'power_monitoring') else [],
            }
        }

    def _identify_handlers(self):
        """
        Scan device endpoints and attach appropriate cluster handlers.
        """
        self.handlers.clear()

        # Get device-specific endpoint preferences
        binding_prefs = self.get_binding_preferences()
        preferred_endpoints = {}

        # Build preferred endpoint map from binding preferences
        if 'target_endpoints' in binding_prefs:
            for cluster_id, ep_id in binding_prefs['target_endpoints'].items():
                preferred_endpoints[cluster_id] = ep_id

        # === ADD PHILIPS HUE MOTION SENSOR QUIRK ===
        manufacturer = str(self.zigpy_dev.manufacturer or "").lower()
        model = str(self.zigpy_dev.model or "").lower()

        if "philips" in manufacturer or "signify" in manufacturer:
            if "sml" in model:  # Philips Hue Motion (SML001, SML002, etc.)
                # Force sensor clusters to EP2
                preferred_endpoints[0x0406] = 2  # Occupancy Sensing
                preferred_endpoints[0x0400] = 2  # Illuminance
                preferred_endpoints[0x0402] = 2  # Temperature
                logger.info(f"[{self.ieee}] Applied Philips Hue Motion quirk: sensors on EP2")
        # === END QUIRK ===

        for ep_id, ep in self.zigpy_dev.endpoints.items():
            if ep_id == 0:
                continue  # Skip ZDO endpoint

            # Helper to attach a handler
            def attach_handler(cluster, is_server=True):
                cid = cluster.cluster_id
                handler_cls = HANDLER_REGISTRY.get(cid)

                if handler_cls:
                    # Skip if there's a preferred endpoint for this cluster and this isn't it
                    if cid in preferred_endpoints and ep_id != preferred_endpoints[cid]:
                        logger.debug(f"[{self.ieee}] Skipping cluster 0x{cid:04x} on EP{ep_id} "
                                     f"(preferred EP{preferred_endpoints[cid]})")
                        return

                    try:
                        # Create unique key for endpoint+cluster combination
                        handler_key = (ep_id, cid)

                        # Instantiate the handler
                        handler = handler_cls(self, cluster)
                        self.handlers[handler_key] = handler

                        # Also store by cluster ID for simple lookup (server side takes priority)
                        # Only overwrite if EP1, otherwise keep first one found
                        if cid not in self.handlers or ep_id == 1:
                            self.handlers[cid] = handler

                        direction = "Input/Server" if is_server else "Output/Client"
                        logger.debug(f"[{self.ieee}] Attached {handler_cls.__name__} "
                                     f"for endpoint {ep_id}, {direction} cluster 0x{cid:04x}")
                    except Exception as e:
                        logger.error(f"[{self.ieee}] Failed to attach handler for "
                                     f"endpoint {ep_id}, cluster 0x{cid:04x}: {e}")

            # Process input clusters (Server side - we read from these)
            for cluster in ep.in_clusters.values():
                attach_handler(cluster, is_server=True)

            # Process output clusters (Client side - device sends commands to us)
            for cluster in ep.out_clusters.values():
                attach_handler(cluster, is_server=False)

        # Re-detect capabilities after handlers (and potentially info) are processed
        if hasattr(self, 'capabilities'):
            self.capabilities._detect_capabilities()

    def restore_state(self, cached_state: Dict[str, Any]):
        """
        Restore state from cache on startup.
        Does NOT trigger update notifications.
        """
        if cached_state:
            self.state.update(cached_state)

            # Restore last_seen to calculate availability
            if 'last_seen' in cached_state:
                self.last_seen = cached_state['last_seen']

                # Calculate availability based on restored last_seen
                # This prevents all devices showing as offline on startup
                self._available = self.is_available()

                logger.debug(f"[{self.ieee}] Restored last_seen={self.last_seen}, "
                             f"available={self._available}")

    def get_state_cache_entry(self) -> Dict[str, Any]:
        """
        Generates the clean dictionary for device_states_cache.json.
        Uses capability filtering and robust manufacturer defaults.
        """
        # 1. Base Info
        entry = {
            "last_seen": int(self.last_seen), # already float seconds or ms
            "available": self.available,
            "ieee": self.ieee,
            "nwk": str(self.zigpy_dev.nwk),
            "lqi": self.state.get("lqi", 0)
        }

        # 2. Robust Manufacturer/Model
        manuf = self.manufacturer or self.zigpy_dev.manufacturer
        model = self.model or self.zigpy_dev.model

        entry["manufacturer"] = manuf if manuf else "Unknown"
        entry["model"] = model if model else "Unknown"

        # 3. Clean State Data
        # We re-filter here just to be safe against any legacy state pollution
        clean_state = self.capabilities.filter_state_update(self.state)
        entry.update(clean_state)

        return entry

    def check_availability_change(self) -> bool:
        """
        Check if availability state has changed (e.g., Online -> Offline).
        Called periodically by the Availability Watchdog in Core.
        """
        # 1. Calculate current status based on time
        is_now_available = self.is_available()

        # 2. Check if it differs from our cached status
        if is_now_available != self._available:
            self._available = is_now_available

            status_str = "Online" if is_now_available else "Offline"
            logger.info(f"[{self.ieee}] Availability changed to {status_str}")

            # 3. Force an update notification to Core/MQTT
            self.service.handle_device_update(self, {})

            return True

        return False

    def update_last_seen(self):
        """Update the last_seen timestamp to now."""
        self.last_seen = int(time.time() * 1000)
        self.state['last_seen'] = self.last_seen
        # Mark as available immediately when we hear from it
        if not self._available:
            self._available = True
            self.service.handle_device_update(self, {})

    def update_state(self, data: Dict[str, Any], qos: Optional[int] = None, endpoint_id: Optional[int] = None):
        """
        Update device state and notify the service.
        Includes smart duplicate detection logic and capability filtering.
        """
        # === FIX 1: CAPABILITY FILTERING ===
        # This prevents invalid attributes (like occupancy on a bulb) from polluting the state
        if hasattr(self, 'capabilities'):
            original_count = len(data)
            data = self.capabilities.filter_state_update(data)
            if len(data) < original_count:
                # logger.debug(f"[{self.ieee}] Capability filter removed {original_count - len(data)} fields")
                pass

        if not data:
            return

        changed = {}
        duplicates_detected = []

        for k, v in data.items():
            # SMART DUPLICATE DETECTION ---------------------------------------
            # If we know the source endpoint, check for collisions
            if endpoint_id is not None:
                if k not in self._attribute_sources:
                    self._attribute_sources[k] = {}

                # Record this endpoint as a source for this attribute
                self._attribute_sources[k][endpoint_id] = time.time()

                # If multiple endpoints are reporting this attribute...
                if len(self._attribute_sources[k]) > 1:
                    # 1. Check Preference: If user pinned an endpoint, ignore others
                    if k in self._preferred_endpoints:
                        preferred_ep = self._preferred_endpoints[k]
                        if endpoint_id != preferred_ep:
                            # logger.debug(f"[{self.ieee}] Ignoring {k}={v} from EP{endpoint_id} (Preferred: EP{preferred_ep})")
                            continue  # SKIP this update

                    # 2. Heuristic: Outlier Detection (The "Smart" part)
                    # If this value is 0 but we have a non-zero value from another EP recently, drop it
                    if (isinstance(v, (int, float)) and v == 0):
                        # Check if any OTHER endpoint has reported a non-zero value recently (last 1 hour)
                        has_better_source = False
                        for other_ep, ts in self._attribute_sources[k].items():
                            if other_ep != endpoint_id:
                                # We assume other sources might be better if this is 0
                                has_better_source = True
                                break

                        if has_better_source:
                            logger.warning(
                                f"[{self.ieee}] ⚠️ Dropped suspicious duplicate {k}={v} from EP{endpoint_id} (Outlier Check)")
                            # We can also flag this for the frontend
                            duplicates_detected.append({
                                "attribute": k,
                                "value": v,
                                "endpoint": endpoint_id,
                                "reason": "outlier_zero"
                            })
                            continue  # SKIP update

            # -----------------------------------------------------------------

            always_report = ['occupancy', 'presence', 'motion', 'contact', 'alarm',
                             'tamper', 'battery_low', 'vibration', 'on_with_timed_off', 'action']

            # Only mark as changed if value is different or it's an event type
            if k in always_report or self.state.get(k) != v:
                changed[k] = v

        self.state.update(data)

        # Only update last_seen if this is a "live" update, not a restore
        if 'last_seen' not in data:
            self.last_seen = int(time.time() * 1000)
            self.state['last_seen'] = self.last_seen

        self._available = True

        if changed:
            # Add last_seen to the changes so the UI knows the device is alive
            changed['last_seen'] = self.last_seen

            # Send update to service
            self.service.handle_device_update(self, changed, qos=qos, endpoint_id=endpoint_id)

            # If we detected issues, emit a warning event for the frontend
            if duplicates_detected:
                self.service._emit_sync("duplicate_attribute_warning", {
                    "ieee": self.ieee,
                    "details": duplicates_detected
                })

    def set_preferred_endpoint(self, attribute: str, endpoint_id: int):
        """Pin a specific endpoint for an attribute to ignore duplicates."""
        self._preferred_endpoints[attribute] = endpoint_id

        # Save to persistent settings
        if self.ieee not in self.service.device_settings:
            self.service.device_settings[self.ieee] = {}

        self.service.device_settings[self.ieee]['preferred_endpoints'] = self._preferred_endpoints
        self.service._save_json("device_settings.json", self.service.device_settings)

        logger.info(f"[{self.ieee}] Pinned {attribute} to Endpoint {endpoint_id}")

    def is_available(self) -> bool:
        """Check if device is considered available based on last_seen."""
        role = self.get_role()

        if role == "Coordinator":
            return True

        if self.last_seen == 0:
            return False

        now = time.time() * 1000
        elapsed = now - self.last_seen

        if self._is_battery_powered():
            threshold = CONSIDER_UNAVAILABLE_BATTERY * 1000
        else:
            threshold = CONSIDER_UNAVAILABLE_MAINS * 1000

        return elapsed < threshold

    def _is_battery_powered(self) -> bool:
        """Check if device is battery powered."""
        power_source = self.state.get('power_source', '')
        if 'battery' in str(power_source).lower():
            return True
        role = self.get_role()
        return role == "EndDevice"

    async def configure(self, config: Optional[Dict] = None):
        """
        Configure cluster handlers (bindings/reporting) and apply settings.
        """
        logger.info(f"[{self.ieee}] Configuring device...")

        # 1. Targeted Updates (Fast Path)
        if config and config.get('updates'):
            updates = config['updates']
            logger.info(f"[{self.ieee}] Applying targeted updates: {updates}")

            # --- Tuya Settings ---
            tuya_handler = self.handlers.get(0xEF00)
            if tuya_handler:
                await tuya_handler.apply_settings(updates)

            # --- On/Off Configuration (Power On Behavior) ---
            for key, val in updates.items():
                if key.startswith("startup_behavior_"):
                    try:
                        ep_id = int(key.split("_")[-1])
                        # Find the OnOff handler for this endpoint: (ep_id, cluster_id)
                        handler = self.handlers.get((ep_id, 0x0006))
                        if handler:
                            # 0x4003 = StartUpOnOff
                            await handler.cluster.write_attributes({0x4003: int(val)})
                            logger.info(f"[{self.ieee}] Set startup behavior for EP{ep_id} to {val}")
                    except Exception as e:
                        logger.error(f"[{self.ieee}] Failed to set startup behavior: {e}")

            # --- Occupancy / PIR / Sensitivity Settings ---
            occ_handler = self.handlers.get(0x0406)
            if occ_handler:
                if 'motion_timeout' in updates or 'pir_o_to_u_delay' in updates:
                    raw_val = updates.get('motion_timeout') or updates.get('pir_o_to_u_delay')
                    if raw_val is not None:
                        try:
                            val = int(raw_val)
                            await occ_handler.cluster.write_attributes({0x0010: val})
                            logger.info(f"[{self.ieee}] Set PIR delay to {val}s")
                        except (ValueError, TypeError) as e:
                            logger.error(f"[{self.ieee}] Invalid motion_timeout value '{raw_val}': {e}")
                    else:
                        logger.warning(f"[{self.ieee}] motion_timeout/pir_o_to_u_delay is None, skipping")

                if 'sensitivity' in updates:
                    raw_val = updates.get('sensitivity')
                    if raw_val is not None:
                        try:
                            val = int(raw_val)
                            await occ_handler.cluster.write_attributes({0x0030: val})
                            logger.info(f"[{self.ieee}] Set Sensitivity to {val}")
                        except (ValueError, TypeError) as e:
                            logger.error(f"[{self.ieee}] Invalid sensitivity value '{raw_val}': {e}")
                    else:
                        logger.warning(f"[{self.ieee}] sensitivity value is None, skipping")

            # --- Thermostat Settings ---
            thermostat_handler = self.handlers.get(0x0201)
            if thermostat_handler:
                if 'local_temperature_calibration' in updates:
                    val = int(float(updates['local_temperature_calibration']) * 100)
                    await thermostat_handler.cluster.write_attributes({0x0010: val})
                    logger.info(f"[{self.ieee}] Set Temp Calibration to {val}")

            return  # Exit fast if we only wanted to update settings

        # 2. Full Configuration (Slow Path)
        if config and 'tuya_settings' in config and config['tuya_settings']:
            tuya_handler = self.handlers.get(0xEF00)
            if tuya_handler:
                await tuya_handler.apply_settings(config['tuya_settings'])

        configured_handlers = set()
        for key, handler in self.handlers.items():
            if handler in configured_handlers:
                continue
            try:
                await handler.configure()
                configured_handlers.add(handler)
            except Exception as e:
                logger.warning(f"[{self.ieee}] Handler configuration failed for "
                               f"cluster 0x{handler.cluster_id:04x}: {e}")

    async def interview(self):
        """
        Re-interview the device - refresh descriptors and cluster information.
        """
        logger.info(f"[{self.ieee}] Re-interviewing device...")

        try:
            await self.zigpy_dev.zdo.Node_Desc_req()
            logger.debug(f"[{self.ieee}] Node descriptor requested")

            await self.zigpy_dev.zdo.Active_EP_req()
            logger.debug(f"[{self.ieee}] Active endpoints requested")

            for ep_id in self.zigpy_dev.endpoints:
                if ep_id == 0: continue
                try:
                    await self.zigpy_dev.zdo.Simple_Desc_req(ep_id)
                    logger.debug(f"[{self.ieee}] Simple descriptor requested for endpoint {ep_id}")
                except Exception as e:
                    logger.warning(f"[{self.ieee}] Failed to get simple descriptor for ep {ep_id}: {e}")

            self._identify_handlers()
            logger.info(f"[{self.ieee}] Interview complete")

        except Exception as e:
            logger.error(f"[{self.ieee}] Interview failed: {e}")
            raise

    async def poll(self) -> Dict[str, Any]:
        """
        Poll all handlers for current attribute values, ensuring resilience
        per-handler via the command wrapper.

        Returns:
            Dict containing polled data and a '__poll_success' status.
        """
        logger.info(f"[{self.ieee}] Polling device...")

        results = {}
        polled_handlers = set()

        # Track if all handlers polled successfully after retries
        poll_success = True

        for key, handler in self.handlers.items():
            if handler in polled_handlers: continue
            polled_handlers.add(handler)

            try:
                # CENTRALIZED RESILIENT CALL
                handler_results = await self._cmd_wrapper.execute(handler.poll)

                if handler_results:
                    results.update(handler_results)
            except Exception as e:
                # This is the final failure after all retries are exhausted.
                poll_success = False # Mark the overall poll as failed
                logger.warning(f"[{self.ieee}] Poll failed for cluster "
                               f"0x{handler.cluster_id:04x} after retries: {e}")

        if results:
            self.update_state(results)

        # Add success status to the result set, to be checked by the calling service.
        results['__poll_success'] = poll_success
        return results

    def get_control_commands(self) -> List[Dict[str, Any]]:
        """
        Aggregates available control methods (commands) from all handlers.
        This list will be used to populate the 'Control' tab in the UI
        and determine if the tab should be shown at all.
        """
        commands = []
        seen_handlers = set()

        for handler in self.handlers.values():
            if handler in seen_handlers: continue
            seen_handlers.add(handler)

            # Use Cluster ID to determine available high-level commands (writeable actions)

            # --- On/Off/Toggle (0x0006) ---
            if handler.CLUSTER_ID == 0x0006:
                commands.extend([
                    {"command": "on", "label": "Turn On", "endpoint_id": handler.endpoint.endpoint_id},
                    {"command": "off", "label": "Turn Off", "endpoint_id": handler.endpoint.endpoint_id},
                    {"command": "toggle", "label": "Toggle State", "endpoint_id": handler.endpoint.endpoint_id}
                ])

            # --- Brightness/Level Control (0x0008) ---
            if handler.CLUSTER_ID == 0x0008:
                commands.append({
                    "command": "brightness",
                    "label": "Set Brightness (%)",
                    "type": "slider",
                    "min": 0, "max": 100,
                    "endpoint_id": handler.endpoint.endpoint_id
                })

            # --- Color Control (0x0300) (Requires 0x0300 handler implementation) ---
            if handler.CLUSTER_ID == 0x0300:
                commands.append({
                    "command": "color_temp",
                    "label": "Set Color Temperature (K)",
                    "type": "slider",
                    "min": 2000, "max": 6500,
                    "endpoint_id": handler.endpoint.endpoint_id
                })

            # --- Thermostat/HVAC (0x0201) (Requires 0x0201 handler implementation) ---
            if handler.CLUSTER_ID == 0x0201:
                commands.append({
                    "command": "temperature",
                    "label": "Set Temperature",
                    "type": "number",
                    "unit": "C",
                    "endpoint_id": handler.endpoint.endpoint_id
                })
                commands.append({
                    "command": "system_mode",
                    "label": "Set System Mode",
                    "type": "select",
                    "options": ["off", "heat", "cool", "auto"],
                    "endpoint_id": handler.endpoint.endpoint_id
                })

            # --- Window Covering (0x0102) (Requires 0x0102 handler implementation) ---
            if handler.CLUSTER_ID == 0x0102:
                commands.extend([
                    {"command": "open", "label": "Open Cover", "endpoint_id": handler.endpoint.endpoint_id},
                    {"command": "close", "label": "Close Cover", "endpoint_id": handler.endpoint.endpoint_id},
                    {"command": "stop", "label": "Stop Cover", "endpoint_id": handler.endpoint.endpoint_id},
                    {"command": "position", "label": "Set Position (%)", "type": "slider", "min": 0, "max": 100,
                     "endpoint_id": handler.endpoint.endpoint_id}
                ])

        return commands

    @with_retries(max_retries=3, backoff_base=1.5, timeout=10.0)
    async def send_command(self, command: str, value: Any = None, endpoint_id: Optional[int] = None) -> Any:
        """
        Send a command to the device, optionally targeting a specific endpoint.
        Now with automatic retry on transient failures.
        """
        logger.info(f"[{self.ieee}] Sending command: {command} = {value} (EP: {endpoint_id})")

        command = command.lower()

        def get_handler(cluster_id):
            if endpoint_id:
                key = (endpoint_id, cluster_id)
                if key in self.handlers:
                    return self.handlers[key]
            # Fallback to default
            return self.handlers.get(cluster_id)

        if command in ['on', 'off', 'toggle']:
            handler = get_handler(0x0006)
            if handler:
                if command == 'on':
                    await handler.turn_on()
                elif command == 'off':
                    await handler.turn_off()
                else:
                    await handler.toggle()
                return True

        elif command == 'brightness' and value is not None:
            handler = get_handler(0x0008)
            if handler:
                await handler.set_brightness_pct(int(value))
                return True

        elif command == 'color_temp' and value is not None:
            handler = get_handler(0x0300)
            if handler:
                await handler.set_color_temp_kelvin(int(value))
                return True

        elif command == 'temperature' and value is not None:
            handler = get_handler(0x0201)
            if handler:
                await handler.set_heating_setpoint(float(value))
                return True

        # --- SYSTEM MODE HANDLING ---
        elif command == 'system_mode' and value is not None:
            handler = get_handler(0x0201)
            if handler:
                # Value can be string "heat" or int 4
                await handler.set_system_mode(value)
                return True

        elif command == 'identify':
            handler = get_handler(0x0003)
            if handler:
                await handler.identify(5)
                return True

        # --- WINDOW COVERING COMMANDS ---
        elif command == 'open':
            handler = get_handler(0x0102)
            if handler:
                await handler.open()
                return True

        elif command == 'close':
            handler = get_handler(0x0102)
            if handler:
                await handler.close()
                return True

        elif command == 'stop':
            handler = get_handler(0x0102)
            if handler:
                await handler.stop()
                return True

        elif command == 'position' and value is not None:
            handler = get_handler(0x0102)
            if handler:
                await handler.set_position(int(value))
                return True

        logger.warning(f"[{self.ieee}] Unknown command: {command}")
        return False

    async def read_attribute_raw(self, ep_id: int, cluster_id: int, attr_name: str) -> Any:
        ep = self.zigpy_dev.endpoints.get(ep_id)
        if not ep: raise ValueError(f"Endpoint {ep_id} not found")
        cluster = ep.in_clusters.get(cluster_id) or ep.out_clusters.get(cluster_id)
        if not cluster: raise ValueError(f"Cluster 0x{cluster_id:04x} not found")
        result = await cluster.read_attributes([attr_name])
        if result and attr_name in result[0]: return result[0][attr_name]
        return None

    def handle_raw_message(self, cluster_id: int, message: bytes):
        handler = self.handlers.get(cluster_id)
        if handler and hasattr(handler, 'handle_raw_data'):
            handler.handle_raw_data(message)

    def get_role(self) -> str:
        d = self.zigpy_dev
        if self.service.app.state.node_info.ieee == d.ieee: return "Coordinator"
        if "_TZE204" in str(d.manufacturer) or "_TZE200" in str(d.manufacturer): return "Router"
        if hasattr(d, 'node_desc') and d.node_desc:
            if d.node_desc.logical_type == 1:
                return "Router"
            elif d.node_desc.logical_type == 2:
                return "EndDevice"
        nt = getattr(d, 'node_type', None)
        if nt and "ROUTER" in str(nt).upper(): return "Router"
        return "EndDevice"

    def get_binding_preferences(self) -> Dict[str, Dict[int, int]]:
        """
        Get device-specific binding endpoint preferences.

        Different devices use different endpoints for the same clusters.
        This method allows devices to specify their preferred endpoints
        for binding operations.

        Returns:
            Dict with optional 'source_endpoints' and/or 'target_endpoints'
            mappings of {cluster_id: preferred_endpoint_id}

        Example:
            {
                'source_endpoints': {0x0201: 9},  # Use EP9 when binding FROM this device
                'target_endpoints': {0x0201: 5}   # Use EP5 when binding TO this device
            }
        """
        model = str(self.zigpy_dev.model or "").upper()
        manufacturer = str(self.zigpy_dev.manufacturer or "").upper()

        # Hive SLT6 (Thermostat) - prefers EP9 for thermostat cluster
        if "SLT6" in model or "SLT6" in manufacturer:
            return {
                'source_endpoints': {
                    0x0201: 9  # Thermostat cluster on EP9
                }
            }

        # Hive SLR1c (Receiver/Heatlink) - prefers EP5 for thermostat cluster
        if "SLR1C" in model or "SLR" in model or "RECEIVER" in model or "HEATLINK" in model:
            return {
                'target_endpoints': {
                    0x0201: 5  # Thermostat cluster on EP5
                }
            }

        # Add more device-specific preferences here as needed
        # Example for other devices:
        # if "SOME_MODEL" in model:
        #     return {'source_endpoints': {0x0006: 1}}

        # No special preferences - use default endpoint selection
        return {}

    def get_device_config_schema(self) -> List[Dict]:
        """Aggregate configuration options from all attached handlers."""
        schema = []
        seen_handlers = set()
        for handler in self.handlers.values():
            if handler in seen_handlers: continue
            seen_handlers.add(handler)

            if hasattr(handler, 'get_configuration_options'):
                options = handler.get_configuration_options()
                if options: schema.extend(options)

        unique_schema = []
        seen_keys = set()
        for opt in schema:
            if opt['name'] not in seen_keys:
                unique_schema.append(opt)
                seen_keys.add(opt['name'])
        return unique_schema

    def emit_event(self, event_type: str, event_data: Dict[str, Any]):
        self.service._emit_sync("device_event", {"ieee": self.ieee, "event_type": event_type, "data": event_data})

    def get_device_discovery_configs(self) -> List[Dict]:
        """Aggregate HA discovery configs from all handlers."""
        configs = []
        seen_handlers = set()
        for handler in self.handlers.values():
            if handler in seen_handlers: continue
            seen_handlers.add(handler)

            if hasattr(handler, 'get_discovery_configs'):
                c = handler.get_discovery_configs()
                if c: configs.extend(c)

        # Add generic Link Quality sensor
        configs.append({
            "component": "sensor",
            "object_id": "linkquality",
            "config": {
                "name": "Link Quality",
                "device_class": "signal_strength",
                "unit_of_measurement": "lqi",
                "value_template": "{{ value_json.lqi }}"
            }
        })
        return configs