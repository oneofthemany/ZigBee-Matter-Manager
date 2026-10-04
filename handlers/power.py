"""
Power cluster handlers.
Handles: Electrical Measurement (0x0B04), Metering (0x0702)
"""
import logging
from typing import Any, Dict, List, Optional

from .base import ClusterHandler, register_handler
from modules import measurement_sanity

logger = logging.getLogger("handlers.power")

#: Bound without a reporting config, as Z2M does (aurora_lighting.ts). Power
#: arrives in the device's own reports; voltage and current answer a read but
#: are unreportable (0x8C), so they come from the poll. No scaling attributes.
BIND_ONLY_MODELS = frozenset({"DoubleSocket50AU"})

# ELECTRICAL MEASUREMENT CLUSTER (0x0B04)
@register_handler(0x0B04)
class ElectricalMeasurementHandler(ClusterHandler):
    CLUSTER_ID = 0x0B04
    REPORT_CONFIG = [("active_power", 10, 60, 10), ("rms_voltage", 60, 600, 5), ("rms_current", 10, 60, 100)]

    ATTR_ACTIVE_POWER          = 0x050B
    ATTR_RMS_VOLTAGE           = 0x0505
    ATTR_RMS_CURRENT           = 0x0508
    ATTR_AC_VOLTAGE_MULTIPLIER = 0x0600
    ATTR_AC_VOLTAGE_DIVISOR    = 0x0601
    ATTR_AC_CURRENT_MULTIPLIER = 0x0602
    ATTR_AC_CURRENT_DIVISOR    = 0x0603
    ATTR_AC_POWER_MULTIPLIER   = 0x0604
    ATTR_AC_POWER_DIVISOR      = 0x0605

    def __init__(self, device, cluster):
        super().__init__(device, cluster)
        self._power_multiplier   = 1
        self._power_divisor      = 1
        self._voltage_multiplier = 1
        self._voltage_divisor    = 1
        self._current_multiplier = 1
        self._current_divisor    = 1000
        self._resolve_scaling()

    # measurement -> (multiplier field, divisor field, multiplier attr, divisor attr)
    SCALING = {
        "active_power": ("_power_multiplier",   "_power_divisor",   "ac_power_multiplier",   "ac_power_divisor"),
        "rms_voltage":  ("_voltage_multiplier", "_voltage_divisor", "ac_voltage_multiplier", "ac_voltage_divisor"),
        "rms_current":  ("_current_multiplier", "_current_divisor", "ac_current_multiplier", "ac_current_divisor"),
    }
    MEASUREMENT_ATTRS = {"active_power": 0x050B, "rms_voltage": 0x0505, "rms_current": 0x0508}

    def _entry(self) -> Dict[str, Any]:
        from modules.device_profiles import profile_for_device
        return profile_for_device(self.device) or {}

    def _entry_measurements(self) -> Dict[str, Any]:
        return (self._entry().get("zmm") or {}).get("measurements") or {}

    def _device_scaling(self, attr: str, answered: Optional[Dict[str, Any]]) -> Optional[int]:
        """The device's own scaling value: from a fresh read, else zigpy's
        persisted attribute cache (so a restart does not fall back to 1)."""
        v = (answered or {}).get(attr)
        if v is None:
            try:
                v = self.cluster.get(attr)
            except Exception:
                v = None
        return int(v) if isinstance(v, int) and v else None

    def _resolve_scaling(self, answered: Optional[Dict[str, Any]] = None) -> None:
        """Scaling per measurement: ZMM entry, then the device's own, then the
        handler default; the choice is recorded with its source."""
        from modules import device_decisions
        entry = self._entry()
        z_meas = (entry.get("zmm") or {}).get("measurements") or {}
        measured = set(self._measured())
        for name, (mf, df, ma, da) in self.SCALING.items():
            z = z_meas.get(name)
            if isinstance(z, dict) and (z.get("multiplier") or z.get("divisor")):
                setattr(self, mf, z.get("multiplier") or 1)
                setattr(self, df, z.get("divisor") or 1)
                source, reason = "zmm", f"ZMM entry {entry.get('id')}"
            elif self._bind_only():
                source, reason = "default", "bind-only model has no scaling attributes"
            else:
                m, d = self._device_scaling(ma, answered), self._device_scaling(da, answered)
                if m or d:
                    if m:
                        setattr(self, mf, m)
                    if d:
                        setattr(self, df, d)
                    source, reason = "answered", "the device's scaling attributes"
                else:
                    source, reason = "default", "device gives no scaling; handler default"
            if self.MEASUREMENT_ATTRS[name] in measured:
                device_decisions.record(str(self.device.ieee), self.endpoint.endpoint_id,
                                        f"scaling:{name}",
                                        f"x{getattr(self, mf)}/{getattr(self, df)}", source, reason)

    def _scope(self) -> Optional[tuple]:
        """(metering scope, source, reason) set by the user or an entry, else None."""
        from modules.device_facts import user_facts
        from modules.device_profiles import valid_metering
        ep = self.endpoint.endpoint_id
        user = user_facts(str(self.device.ieee)).get((ep, "metering"))
        if valid_metering(user):
            return user, "user", "set by user"
        entry = self._entry()
        scope = ((entry.get("endpoints") or {}).get(str(ep)) or {}).get("metering")
        if valid_metering(scope):
            zmm = (entry.get("meta") or {}).get("source") == "zmm"
            return scope, "zmm" if zmm else "profile", f"{'ZMM entry' if zmm else 'profile'} {entry.get('id')}"
        return None

    def _power_key(self) -> str:
        """Where this EP's power reading is published: the device total for a
        whole-device EP, the measured EP's key for "measures:..." (so a socket's
        power shows under that socket), else this EP's own."""
        from modules.device_profiles import measured_endpoints
        scope = (self._scope() or (None,))[0]
        if scope == "device_total":
            return "power"
        targets = measured_endpoints(scope)
        return f"power_{targets[0]}" if targets else f"power_{self.endpoint.endpoint_id}"

    def _power_name(self) -> str:
        from modules.device_identity import endpoint_label
        from modules.device_profiles import measured_endpoints
        scope = (self._scope() or (None,))[0]
        if scope == "device_total":
            return "Power (whole device)"
        targets = measured_endpoints(scope)
        if targets:
            return "Power " + " + ".join(endpoint_label(self.device, t) or f"EP{t}" for t in targets)
        return self.entity_name("Power")

    def _peers(self) -> Dict[int, "ElectricalMeasurementHandler"]:
        return {k[0]: h for k, h in (getattr(self.device, "handlers", None) or {}).items()
                if isinstance(k, tuple) and k[1] == self.CLUSTER_ID
                and isinstance(h, ElectricalMeasurementHandler)}

    def _total_power(self, ep_id: int, val: float) -> float:
        """The device's power: its whole-device EP when one is known, else the
        sum over EPs, leaving out any known not to meter."""
        state = getattr(self.device, "state", None) or {}
        scopes = {ep: (h._scope() or (None,))[0] for ep, h in self._peers().items()}
        scopes.setdefault(ep_id, (self._scope() or (None,))[0])
        whole = [ep for ep, sc in scopes.items() if sc == "device_total"]
        if whole:
            src = whole[0]
            return round(val if src == ep_id else float(state.get("power", 0) or 0), 1)
        total = 0.0 if scopes.get(ep_id) == "none" else val
        own = self._power_key()
        for key, other in state.items():
            if not key.startswith("power_") or key == own:
                continue
            try:
                other_ep = int(key[6:])
            except ValueError:
                continue
            if scopes.get(other_ep) == "none":
                continue
            try:
                total += float(other)
            except (TypeError, ValueError):
                continue
        return round(total, 1)

    def _bind_only(self) -> bool:
        # Read per call: the model may not be known when the handler attaches.
        model = getattr(self.device, "model", None) or \
            getattr(getattr(self.device, "zigpy_dev", None), "model", None)
        return str(model or "") in BIND_ONLY_MODELS

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if value is None:
            return
        ep_id = self.endpoint.endpoint_id
        updates = {}

        if attrid == self.ATTR_ACTIVE_POWER:
            val = round(float(value) * self._power_multiplier / self._power_divisor, 1)
            key = self._power_key()
            held = self._held_back("active_power", val, key)
            if held is not None:
                updates.update(held)
                if held and key != "power":
                    updates["power"] = self._total_power(ep_id, 0.0)
            elif key == "power":
                updates["power"] = val
            else:
                updates[key] = val
                # Unsuffixed alias: Frames and the device list read "power", not
                # "power_1" (modules/frames.py:83). Summed, so a double socket
                # reads as the whole device; a single meter is unchanged.
                updates["power"] = self._total_power(ep_id, val)

        elif attrid == self.ATTR_RMS_VOLTAGE:
            val = round(float(value) * self._voltage_multiplier / self._voltage_divisor, 1)
            held = self._held_back("rms_voltage", val, f"voltage_{ep_id}")
            if held is not None:
                updates.update(held)
            else:
                updates[f"voltage_{ep_id}"] = val
                if ep_id == 1:
                    updates["voltage"] = val      # not additive: mains is shared

        elif attrid == self.ATTR_RMS_CURRENT:
            val = round(float(value) * self._current_multiplier / self._current_divisor, 3)
            held = self._held_back("rms_current", val, f"current_{ep_id}")
            if held is not None:
                updates.update(held)
            else:
                updates[f"current_{ep_id}"] = val
                if ep_id == 1:
                    updates["current"] = val

        elif attrid == self.ATTR_AC_POWER_MULTIPLIER:   self._power_multiplier   = value or 1
        elif attrid == self.ATTR_AC_POWER_DIVISOR:      self._power_divisor      = value or 1
        elif attrid == self.ATTR_AC_VOLTAGE_MULTIPLIER: self._voltage_multiplier = value or 1
        elif attrid == self.ATTR_AC_VOLTAGE_DIVISOR:    self._voltage_divisor    = value or 1
        elif attrid == self.ATTR_AC_CURRENT_MULTIPLIER: self._current_multiplier = value or 1
        elif attrid == self.ATTR_AC_CURRENT_DIVISOR:    self._current_divisor    = value or 1

        if updates:
            self.device.update_state(updates)

    def parse_value(self, attr_id: int, value: Any) -> Any:
        if attr_id == self.ATTR_ACTIVE_POWER:
            return round(float(value) * self._power_multiplier / self._power_divisor, 1)
        elif attr_id == self.ATTR_RMS_VOLTAGE:
            return round(float(value) * self._voltage_multiplier / self._voltage_divisor, 1)
        elif attr_id == self.ATTR_RMS_CURRENT:
            return round(float(value) * self._current_multiplier / self._current_divisor, 3)
        return value

    async def configure(self):
        if self._bind_only():
            # Instance attribute shadows the class list: bind, write nothing.
            self.REPORT_CONFIG = []
            await super().configure()
            logger.info(f"[{self.device.ieee}] {self.device.model}: bound 0x0B04 "
                        f"without reporting config (bind-only model)")
            return
        try:
            # Reading first lets zigpy record which measurements the device
            # lacks, so reporting, polling and discovery skip them.
            await self.cluster.read_attributes(['active_power', 'rms_voltage', 'rms_current'])
        except Exception as e:
            logger.debug(f"[{self.device.ieee}] EM measurement pre-read failed: {e}")
        await super().configure()
        try:
            result = await self.cluster.read_attributes([
                'ac_voltage_multiplier', 'ac_voltage_divisor',
                'ac_current_multiplier', 'ac_current_divisor',
                'ac_power_multiplier',   'ac_power_divisor',
            ])
            logger.info(f"[{self.device.ieee}] EM raw scaling result: {result}")
            if result and result[0] is not None:
                # An unanswered attribute keeps its default: a current divisor
                # of 1 would read milliamps as amps.
                self._resolve_scaling(answered=result[0])
                logger.info(
                    f"[{self.device.ieee}] EM scaling — "
                    f"V: {self._voltage_multiplier}/{self._voltage_divisor}, "
                    f"I: {self._current_multiplier}/{self._current_divisor}, "
                    f"P: {self._power_multiplier}/{self._power_divisor}"
                )
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Failed to read EM scaling attrs: {e}", exc_info=True)

    def _measured(self) -> List[int]:
        """Measurements this EP has: nothing its entry marks absent or zigpy
        recorded the device refusing."""
        if (self._scope() or (None,))[0] == "none":
            return []
        attrs = [self.ATTR_ACTIVE_POWER, self.ATTR_RMS_VOLTAGE, self.ATTR_RMS_CURRENT]
        absent = {self.MEASUREMENT_ATTRS[n] for n, m in self._entry_measurements().items()
                  if m is None and n in self.MEASUREMENT_ATTRS}
        return [a for a in attrs if a not in absent and self.attribute_supported(a)]

    def get_pollable_attributes(self) -> Dict[int, str]:
        ep = self.endpoint.endpoint_id
        names = {self.ATTR_ACTIVE_POWER: self._power_key(),
                 self.ATTR_RMS_VOLTAGE:  f"voltage_{ep}",
                 self.ATTR_RMS_CURRENT:  f"current_{ep}"}
        return {a: names[a] for a in self._measured()}

    def _held_back(self, measurement: str, val: float, key: str) -> Optional[Dict[str, Any]]:
        """None for a plausible reading. For an impossible one, the updates to
        make instead: blank the shown value once per episode, then nothing."""
        verdict, reason = measurement_sanity.judge(self, measurement, val)
        if verdict == "ok":
            measurement_sanity.sane(self, measurement)
            return None
        if verdict == "unscaled":
            return {key: None} if measurement_sanity.unscaled(self, measurement, reason) else {}
        return {key: None} if measurement_sanity.fault(self, measurement, val, reason) else {}

    async def poll(self) -> Dict[str, Any]:
        # Polled values bypass attribute_updated, so alias here too.
        results = await super().poll()
        ep = self.endpoint.endpoint_id
        power_key = self._power_key()
        for measurement, key in (("active_power", power_key), ("rms_voltage", f"voltage_{ep}"),
                                 ("rms_current", f"current_{ep}")):
            if key in results:
                held = self._held_back(measurement, results[key], key)
                if held is not None:
                    results.pop(key)
                    results.pop(f"{key}_raw", None)
                    results.update(held)
        if power_key != "power" and results.get(power_key) is not None:
            results["power"] = self._total_power(ep, results[power_key])
        if ep == 1:
            for name in ("voltage", "current"):
                if f"{name}_{ep}" in results:
                    results[name] = results[f"{name}_{ep}"]
        return results

    def _sensor_configs(self) -> Dict[int, Dict]:
        ep = self.endpoint.endpoint_id
        return {
            self.ATTR_ACTIVE_POWER: {"component": "sensor", "object_id": f"power_{ep}",   "config": {"name": self._power_name(),          "device_class": "power",   "unit_of_measurement": "W",  "value_template": f"{{{{ value_json.{self._power_key()} }}}}"}},
            self.ATTR_RMS_VOLTAGE:  {"component": "sensor", "object_id": f"voltage_{ep}", "config": {"name": self.entity_name("Voltage"), "device_class": "voltage", "unit_of_measurement": "V",  "value_template": f"{{{{ value_json.voltage_{ep} }}}}"}},
            self.ATTR_RMS_CURRENT:  {"component": "sensor", "object_id": f"current_{ep}", "config": {"name": self.entity_name("Current"), "device_class": "current", "unit_of_measurement": "A",  "value_template": f"{{{{ value_json.current_{ep} }}}}"}},
        }

    def _record_scope(self) -> Optional[str]:
        """Store the metering-scope decision; the rules alone can only say
        which EPs have reported power, not what each one measures."""
        from modules import device_decisions
        from modules.zigbee_cache import get_facts
        ieee, ep = str(self.device.ieee), self.endpoint.endpoint_id
        scope = self._scope()
        if scope:
            device_decisions.record(ieee, ep, "metering", *scope)
            return scope[0]
        import modules.zigbee_cache as zigbee_cache
        if zigbee_cache._db is None:
            return None     # never pay the DB open from a handler: warm() does
        try:
            import json
            live = sorted(f["endpoint_id"] for f in get_facts(ieee)
                          if f["source"] == "observed" and f["subject"] == "reports:0x0B04/0x050B"
                          and json.loads(f["value"]).get("nonzero"))
        except Exception:
            live = []
        reason = (f"power seen on EP{', EP'.join(map(str, live))}; a load test settles it"
                  if live else "no power seen yet")
        device_decisions.record(ieee, ep, "metering", "unknown", "rule", reason)
        return None

    def get_discovery_configs(self) -> List[Dict]:
        scope = self._record_scope()
        configs = self._sensor_configs()
        return [configs[a] for a in self._measured()]

    def get_retired_discovery_configs(self) -> List[Dict]:
        # Sensors published before the device was known to lack them.
        measured = set(self._measured())
        return [{"component": c["component"], "object_id": c["object_id"]}
                for a, c in self._sensor_configs().items() if a not in measured]

# METERING CLUSTER (0x0702)
@register_handler(0x0702)
class MeteringHandler(ClusterHandler):
    CLUSTER_ID = 0x0702
    REPORT_CONFIG = [("instantaneous_demand", 30, 300, 10), ("current_summation_delivered", 300, 3600, 100)]

    ATTR_CURRENT_SUMMATION_DELIVERED = 0x0000
    ATTR_INSTANTANEOUS_DEMAND = 0x0400
    ATTR_MULTIPLIER = 0x0301
    ATTR_DIVISOR = 0x0302

    def __init__(self, device, cluster):
        super().__init__(device, cluster)
        self._multiplier = 1
        self._divisor = 1
        self._resolve_scaling()

    def _resolve_scaling(self, answered: Optional[Dict[Any, Any]] = None) -> None:
        """Scaling: ZMM entry, then the device's own multiplier/divisor (a fresh
        read, else zigpy's persisted cache), then 1/1. Multiplier and divisor
        are static, so a device rarely reports them: waiting for a report left
        the divisor at 1 and read watt-hours as kilowatt-hours."""
        from modules import device_decisions
        from modules.device_profiles import profile_for_device
        entry = profile_for_device(self.device) or {}
        z = ((entry.get("zmm") or {}).get("measurements") or {}).get("energy")
        if isinstance(z, dict) and (z.get("multiplier") or z.get("divisor")):
            self._multiplier = z.get("multiplier") or 1
            self._divisor = z.get("divisor") or 1
            source, reason = "zmm", f"ZMM entry {entry.get('id')}"
        else:
            m, d = self._device_value("multiplier", answered), self._device_value("divisor", answered)
            if m or d:
                self._multiplier, self._divisor = m or 1, d or 1
                source, reason = "answered", "the device's multiplier and divisor"
            else:
                source, reason = "default", "device gives no scaling; handler default"
        device_decisions.record(str(self.device.ieee), self.endpoint.endpoint_id, "scaling:energy",
                                f"x{self._multiplier}/{self._divisor}", source, reason)

    def _device_value(self, attr: str, answered: Optional[Dict[Any, Any]]) -> Optional[int]:
        v = (answered or {}).get(attr)
        if v is None:
            try:
                v = self.cluster.get(attr)
            except Exception:
                v = None
        return int(v) if isinstance(v, int) and v else None

    async def configure(self):
        ok = await super().configure()
        try:
            result = await self.cluster.read_attributes(["multiplier", "divisor"])
            if result and result[0]:
                self._resolve_scaling(answered=result[0])
        except Exception as e:
            logger.debug(f"[{self.device.ieee}] Metering scaling read failed: {e}")
        return ok

    def _energy(self, value: Any) -> float:
        return round(float(value) * self._multiplier / self._divisor, 3)

    def attribute_updated(self, attrid: int, value: Any, timestamp=None):
        if value is None: return
        ep_id = self.endpoint.endpoint_id
        updates = {}

        if attrid == self.ATTR_CURRENT_SUMMATION_DELIVERED:
            val = self._energy(value)
            updates[f"energy_{ep_id}"] = val
            if ep_id == 1: updates["energy"] = val

        elif attrid == self.ATTR_INSTANTANEOUS_DEMAND:
            val = round(float(value) * self._multiplier / self._divisor, 1)
            updates[f"power_demand_{ep_id}"] = val

        elif attrid == self.ATTR_MULTIPLIER:
            self._multiplier = value or 1
        elif attrid == self.ATTR_DIVISOR:
            self._divisor = value or 1

        if updates: self.device.update_state(updates)

    def parse_value(self, attr_id: int, value: Any) -> Any:
        if attr_id == self.ATTR_CURRENT_SUMMATION_DELIVERED:
            return self._energy(value)
        return value

    def get_pollable_attributes(self) -> Dict[int, str]:
        return {
            self.ATTR_CURRENT_SUMMATION_DELIVERED: f"energy_{self.endpoint.endpoint_id}",
            self.ATTR_INSTANTANEOUS_DEMAND: "instantaneous_demand",
        }

    async def poll(self) -> Dict[str, Any]:
        # Polled values bypass attribute_updated, so alias here too.
        results = await super().poll()
        ep = self.endpoint.endpoint_id
        if ep == 1 and f"energy_{ep}" in results:
            results["energy"] = results[f"energy_{ep}"]
        return results

    def get_discovery_configs(self) -> List[Dict]:
        ep = self.endpoint.endpoint_id
        return [{
            "component": "sensor", "object_id": f"energy_{ep}",
            "config": {
                "name": self.entity_name("Energy"), "device_class": "energy", "unit_of_measurement": "kWh",
                "state_class": "total_increasing",
                "value_template": f"{{{{ value_json.energy_{ep} }}}}"
            }
        }]
