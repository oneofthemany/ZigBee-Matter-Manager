"""
Poll Control cluster handler (0x0020).

Sleepy end devices (battery sensors like Philips Hue SML, Aqara motion, etc.)
send periodic Check-in commands via this cluster. The coordinator MUST respond
with a Check-in Response or the device will eventually consider the parent lost
and stop communicating.

This is the root cause of Philips Hue motion sensors silently dropping off
the network after days/weeks — unanswered check-ins.
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

from .base import ClusterHandler, register_handler

logger = logging.getLogger("handlers.poll_control")

# Poll Control Command IDs (Client -> Server, i.e. device -> coordinator)
CHECKIN_CMD = 0x00  # Check-in (device asks "are you still there?")

# Poll Control Command IDs (Server -> Client, i.e. coordinator -> device)
CHECKIN_RESPONSE_CMD = 0x00  # Check-in Response
FAST_POLL_STOP_CMD = 0x01
SET_LONG_POLL_CMD = 0x02
SET_SHORT_POLL_CMD = 0x03


@register_handler(0x0020)
class PollControlHandler(ClusterHandler):
    """
    Handles Poll Control cluster (0x0020).

    Critical for sleepy end devices:
    - Philips Hue SML motion sensors
    - Aqara motion/door sensors
    - Any battery-powered Zigbee end device

    Without responding to check-ins, devices silently leave the network.
    """
    CLUSTER_ID = 0x0020
    REPORT_CONFIG = []  # No reporting needed — this is command-driven
    ALWAYS_CONFIGURE = True  # Bind must run even though there's nothing to report

    # Desired check-in cadence written at join time (values in SECONDS; the
    # attributes themselves are quarter-seconds, converted below).
    #
    # check-in: how often the device wakes to ask "are you still there?".
    #   1h keeps the app<->device relationship fresh without wrecking battery.
    # long poll: the MAC data-poll cadence to the parent while idle. This is the
    #   actual keep-alive that stops the parent aging the child out — it must be
    #   comfortably shorter than the parent's child timeout (Silabs default
    #   ~256 min). Left at a conservative 30s.
    DESIRED_CHECKIN_INTERVAL_S = 3600
    DESIRED_LONG_POLL_INTERVAL_S = 30

    # Poll Control Attributes
    ATTR_CHECKIN_INTERVAL = 0x0000      # How often device checks in (quarter-seconds)
    ATTR_LONG_POLL_INTERVAL = 0x0001    # Long poll interval (quarter-seconds)
    ATTR_SHORT_POLL_INTERVAL = 0x0002   # Short poll interval (quarter-seconds)
    ATTR_FAST_POLL_TIMEOUT = 0x0003     # Fast poll timeout (quarter-seconds)
    ATTR_CHECKIN_INTERVAL_MIN = 0x0004  # Minimum check-in interval
    ATTR_LONG_POLL_INTERVAL_MIN = 0x0005
    ATTR_FAST_POLL_TIMEOUT_MAX = 0x0006

    def __init__(self, device, cluster):
        super().__init__(device, cluster)
        self._checkin_count = 0
        logger.info(f"[{self.device.ieee}] Poll Control handler initialised")

    def cluster_command(self, tsn: int, command_id: int, args):
        """Handle Poll Control commands from the device."""
        super().cluster_command(tsn, command_id, args)

        if command_id == CHECKIN_CMD:
            self._handle_checkin(tsn)
        else:
            logger.debug(
                f"[{self.device.ieee}] Poll Control command 0x{command_id:02X}, args={args}"
            )

    def _handle_checkin(self, tsn: int):
        """
        Respond to a Check-in from a sleepy end device.

        The Check-in Response tells the device:
        - start_fast_polling: False (no need for fast polling)
        - fast_poll_timeout: 0 (not entering fast poll mode)

        This keeps the device on the network with minimal power consumption.
        """
        self._checkin_count += 1
        self.device.update_last_seen()

        logger.debug(
            f"[{self.device.ieee}] Poll Control Check-in #{self._checkin_count} — responding"
        )

        # Send Check-in Response. The device only stays awake for a short
        # fast-poll window after checking in, so this must go out promptly — and
        # it must echo the check-in's TSN or the device may not match it to its
        # request and will treat the check-in as unanswered.
        try:
            asyncio.create_task(self._send_checkin_response(tsn))
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Failed to schedule check-in response: {e}")

    async def _send_checkin_response(self, tsn: Optional[int] = None):
        """Send the actual Check-in Response to the device."""
        try:
            # zigpy's PollControl cluster has a checkin_response() server command
            result = await self.cluster.checkin_response(
                start_fast_polling=False,
                fast_poll_timeout=0,
                tsn=tsn,
            )
            logger.debug(f"[{self.device.ieee}] Check-in response sent: {result}")
        except AttributeError:
            # Fallback: manually construct the response if method unavailable
            try:
                from zigpy.zcl.foundation import GeneralCommand
                result = await self.cluster.command(
                    CHECKIN_RESPONSE_CMD,
                    False,  # start_fast_polling
                    0,      # fast_poll_timeout
                    direction=True,  # client-to-server
                    tsn=tsn,
                )
                logger.debug(f"[{self.device.ieee}] Check-in response sent (fallback): {result}")
            except Exception as e:
                logger.warning(f"[{self.device.ieee}] Check-in response fallback failed: {e}")
        except asyncio.TimeoutError:
            # Device may have already gone back to sleep — this is normal
            logger.debug(f"[{self.device.ieee}] Check-in response timed out (device sleeping)")
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Check-in response failed: {e}")

    def attribute_updated(self, attrid: int, value: Any, timestamp: Optional[float] = None):
        """Handle Poll Control attribute reports."""
        if hasattr(value, 'value'):
            value = value.value

        names = {
            self.ATTR_CHECKIN_INTERVAL: "checkin_interval",
            self.ATTR_LONG_POLL_INTERVAL: "long_poll_interval",
            self.ATTR_SHORT_POLL_INTERVAL: "short_poll_interval",
            self.ATTR_FAST_POLL_TIMEOUT: "fast_poll_timeout",
        }

        attr_name = names.get(attrid)
        if attr_name:
            # Values are in quarter-seconds
            seconds = value / 4 if value else 0
            logger.debug(
                f"[{self.device.ieee}] Poll Control {attr_name}: "
                f"{value} quarter-seconds ({seconds}s)"
            )

    async def configure(self):
        """
        Configure Poll Control so the sleepy end device stays on the network.

        Order matters:
          1. bind() — registers the coordinator as the recipient of this
             device's Check-in commands. WITHOUT THIS the device has no
             check-in destination, never gets a Check-in Response, eventually
             decides its parent is lost, and silently drops off (the exact
             failure behind Hive SLT thermostats going dark after a day or two).
          2. write the long-poll / check-in intervals so the device MAC-polls
             its parent often enough to never be aged out of the child table,
             and checks in on a known cadence.

        Both steps run at join time while the device is guaranteed awake.
        Interval writes are best-effort — a failure here never fails the bind.
        """
        try:
            async with asyncio.timeout(5.0):
                await self.cluster.bind()
            logger.info(f"[{self.device.ieee}] ✅ Poll Control cluster bound to coordinator")
        except asyncio.TimeoutError:
            logger.warning(f"[{self.device.ieee}] Poll Control bind timed out")
            return False
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Poll Control bind failed: {e}")
            return False

        # ── Set the long-poll (MAC keep-alive) and check-in cadence ──────────
        # Sent as Poll Control commands (server-received) while the device is
        # awake. Quiet best-effort: a sleeping/older device just keeps its
        # firmware defaults, and the bind above is what actually matters.
        long_poll_qs = self.DESIRED_LONG_POLL_INTERVAL_S * 4
        checkin_qs = self.DESIRED_CHECKIN_INTERVAL_S * 4

        try:
            async with asyncio.timeout(5.0):
                await self.cluster.set_long_poll_interval(long_poll_qs)
            logger.info(
                f"[{self.device.ieee}] Poll Control long-poll interval set to "
                f"{self.DESIRED_LONG_POLL_INTERVAL_S}s"
            )
        except Exception as e:
            logger.debug(
                f"[{self.device.ieee}] set_long_poll_interval skipped: "
                f"{type(e).__name__}: {e}"
            )

        try:
            async with asyncio.timeout(5.0):
                await self.cluster.write_attributes(
                    {self.ATTR_CHECKIN_INTERVAL: checkin_qs}
                )
            logger.info(
                f"[{self.device.ieee}] Poll Control check-in interval set to "
                f"{self.DESIRED_CHECKIN_INTERVAL_S}s"
            )
        except Exception as e:
            logger.debug(
                f"[{self.device.ieee}] check-in interval write skipped: "
                f"{type(e).__name__}: {e}"
            )

        return True

    def get_discovery_configs(self) -> List[Dict]:
        """No HA discovery needed for Poll Control."""
        return []

    def get_pollable_attributes(self) -> Dict[int, str]:
        """Not pollable — check-ins are device-initiated."""
        return {}