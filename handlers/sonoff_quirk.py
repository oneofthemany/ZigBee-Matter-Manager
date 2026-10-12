"""Sonoff 0xFC11: the ZBMINIR2 relay's settings. Other Sonoff products share
the cluster id and are described by their ZMM entry."""
import logging
from .base import ClusterHandler, register_handler

logger = logging.getLogger("handlers.sonoff")

@register_handler(0xFC11)
class SonoffManufacturerHandler(ClusterHandler):
    """Handle Sonoff manufacturer cluster 0xFC11"""
    CLUSTER_ID = 0xFC11

    ATTR_EXTERNAL_TRIGGER = 0x0016
    ATTR_DETACH_RELAY = 0x0017
    ATTR_WORK_MODE = 0x0018

    async def configure(self):
        """Read device mode on startup"""
        # 0xFC11 is Sonoff's cluster on every product; these are the relay's
        # attributes. A thermostat's or cover's are named by its entry.
        from modules.endpoint_kind import primary_function
        if primary_function(self.endpoint.in_clusters):
            return
        try:
            result = await self.cluster.read_attributes([
                self.ATTR_WORK_MODE,
                self.ATTR_DETACH_RELAY
            ])
            logger.info(f"[{self.device.ieee}] Sonoff config: {result}")
        except Exception as e:
            logger.warning(f"[{self.device.ieee}] Failed to read Sonoff config: {e}")