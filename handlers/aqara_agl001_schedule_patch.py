"""
Runtime fix for overly-strict time validation in zhaquirks' Aqara E1 (agl001)
schedule parser.

ScheduleEvent._validate_time raises on a sentinel time field, aborting the whole
attribute report and losing setpoint, temperature and running_state with it.
This replaces it with a no-op. Safe because schedules are only ever read here,
never written. Import once at startup.
"""
import logging

logger = logging.getLogger("modules.aqara_agl001_patch")

try:
    from zhaquirks.xiaomi.aqara.thermostat_agl001 import ScheduleEvent

    # Preserve original for debugging
    _original = ScheduleEvent._validate_time

    def _tolerant_validate_time(self, time_value):
        # Accept any value; log the out-of-range ones at DEBUG
        if not (0 <= int(time_value) <= 1439):
            logger.debug(
                f"agl001 schedule time out of range ({time_value}) — accepted anyway"
            )

    ScheduleEvent._validate_time = _tolerant_validate_time
    logger.info("Aqara E1 (agl001) _validate_time patched — schedule decode no longer raises")

except ImportError:
    logger.debug("agl001 quirk not installed — patch skipped")
except AttributeError:
    logger.warning("ScheduleEvent._validate_time not found — quirk structure changed, patch skipped")