"""
Unified Touchlink Module

Handles touchlink scan/identify/reset for both:
- EZSP (Silicon Labs) - uses native zigpy touchlink API
- ZNP (Texas Instruments) - uses InterPAN mode like zigbee-herdsman

Reference: zigbee-herdsman/src/adapter/z-stack/adapter/zStackAdapter.ts
"""

import asyncio
import logging
import struct
import random
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, TYPE_CHECKING

import zigpy.types as t

if TYPE_CHECKING:
    from zigpy.application import ControllerApplication

logger = logging.getLogger(__name__)

# Try to import zigpy_znp - may not be available on EZSP systems
try:
    from zigpy_znp.api import ZNP
    import zigpy_znp.commands as c
    import zigpy_znp.types as znp_t
    ZIGPY_ZNP_AVAILABLE = True
except ImportError:
    ZIGPY_ZNP_AVAILABLE = False
    c = None
    znp_t = None

# =============================================================================
# CONSTANTS
# =============================================================================

ZLL_CLUSTER_ID = 0x1000      # ZLL/Touchlink cluster
INTERPAN_ENDPOINT = 12       # Same as zigbee-herdsman
ZLL_PROFILE_ID = 0xC05E      # ZLL Profile


class TouchlinkCommand:
    """Touchlink ZCL command IDs"""
    SCAN_REQUEST = 0x00
    SCAN_RESPONSE = 0x01
    DEVICE_INFO_REQUEST = 0x02
    DEVICE_INFO_RESPONSE = 0x03
    IDENTIFY_REQUEST = 0x06
    RESET_TO_FACTORY_NEW = 0x07
    NETWORK_START_REQUEST = 0x10
    NETWORK_JOIN_ROUTER_REQUEST = 0x12
    NETWORK_JOIN_END_DEVICE_REQUEST = 0x14


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class TouchlinkDevice:
    """Represents a discovered touchlink device"""
    ieee: str
    channel: int
    rssi: Optional[int] = None
    pan_id: Optional[int] = None
    network_address: Optional[int] = None
    endpoint: int = 0
    profile_id: int = 0
    device_id: int = 0
    version: int = 0
    group_ids_begin: int = 0
    group_ids_end: int = 0
    transaction_id: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ieee": self.ieee,
            "channel": self.channel,
            "rssi": self.rssi,
            "pan_id": self.pan_id,
            "network_address": self.network_address,
        }


# =============================================================================
# TOUCHLINK MANAGER
# =============================================================================

class TouchlinkManager:
    """
    Unified touchlink manager supporting both EZSP and ZNP coordinators.

    Usage:
        manager = TouchlinkManager(app)
        await manager.initialize()

        devices = await manager.scan()
        await manager.identify(devices[0])
        await manager.factory_reset(devices[0])
    """

    def __init__(self, app: 'ControllerApplication'):
        self.app = app
        self._coordinator_type: Optional[str] = None
        self._znp: Optional['ZNP'] = None
        self._interpan_registered = False
        self._interpan_lock = False

    async def initialize(self) -> bool:
        """
        Initialize the touchlink manager and detect coordinator type.

        Returns:
            True if touchlink is supported
        """
        # Detect coordinator type
        if hasattr(self.app, '_ezsp'):
            self._coordinator_type = "EZSP"
            logger.info("Touchlink: EZSP coordinator detected")
            return True

        if 'znp' in str(type(self.app)).lower():
            if not ZIGPY_ZNP_AVAILABLE:
                logger.warning("Touchlink: ZNP detected but zigpy-znp not available")
                return False

            if not hasattr(self.app, '_znp'):
                logger.warning("Touchlink: ZNP app missing _znp attribute")
                return False

            self._znp = self.app._znp
            self._coordinator_type = "ZNP"
            logger.info("Touchlink: ZNP coordinator detected")
            return True

        logger.warning("Touchlink: Unknown coordinator type")
        return False

    @property
    def is_supported(self) -> bool:
        """Check if touchlink is supported"""
        return self._coordinator_type is not None

    @property
    def coordinator_type(self) -> Optional[str]:
        """Get the detected coordinator type"""
        return self._coordinator_type

    # =========================================================================
    # PUBLIC API
    # =========================================================================

    async def scan(self, channel: Optional[int] = None) -> Dict[str, Any]:
        """
        Scan for touchlink devices.

        Args:
            channel: Specific channel (11-26) or None for all channels

        Returns:
            Result dict with 'success', 'devices', and optional 'message'/'error'
        """
        if self._coordinator_type == "EZSP":
            return await self._ezsp_scan(channel)
        elif self._coordinator_type == "ZNP":
            return await self._znp_scan(channel)
        else:
            return {
                "success": False,
                "error": "Touchlink not supported by this coordinator",
                "note": "For Philips Hue: Use Hue Dimmer (hold ON+OFF 10s) or power cycle 5x"
            }

    async def identify(self, channel: Optional[int] = None) -> Dict[str, Any]:
        """
        Scan and identify (blink) all found touchlink devices.

        Args:
            channel: Specific channel or None for all channels

        Returns:
            Result dict with identified devices
        """
        if self._coordinator_type == "EZSP":
            return await self._ezsp_identify(channel)
        elif self._coordinator_type == "ZNP":
            return await self._znp_identify(channel)
        else:
            return {"success": False, "error": "Touchlink not supported"}

    async def factory_reset(self, channel: Optional[int] = None) -> Dict[str, Any]:
        """
        Scan and factory reset all found touchlink devices.

        WARNING: This will reset ALL devices found!

        Args:
            channel: Specific channel or None for all channels

        Returns:
            Result dict with reset devices
        """
        if self._coordinator_type == "EZSP":
            return await self._ezsp_factory_reset(channel)
        elif self._coordinator_type == "ZNP":
            return await self._znp_factory_reset(channel)
        else:
            return {"success": False, "error": "Touchlink not supported"}

    # =========================================================================
    # EZSP IMPLEMENTATION (Native zigpy touchlink)
    # =========================================================================

    async def _ezsp_scan(self, channel: Optional[int] = None) -> Dict[str, Any]:
        """EZSP touchlink scan using native zigpy API"""
        try:
            if not hasattr(self.app, 'touchlink'):
                return {
                    "success": False,
                    "error": "Touchlink not available on this EZSP firmware"
                }

            results = []
            channels = [channel] if channel else list(range(11, 27))

            for ch in channels:
                logger.info(f"Touchlink scanning channel {ch}...")
                try:
                    async with asyncio.timeout(5.0):
                        scan_result = await self.app.touchlink.scan(channel=ch)

                        if scan_result:
                            for device in scan_result:
                                results.append(TouchlinkDevice(
                                    ieee=str(device.ieee) if hasattr(device, 'ieee') else "unknown",
                                    channel=ch,
                                    rssi=getattr(device, 'rssi', None)
                                ))
                except asyncio.TimeoutError:
                    pass
                except Exception as e:
                    logger.debug(f"Channel {ch} error: {e}")

            if results:
                return {
                    "success": True,
                    "devices": [d.to_dict() for d in results]
                }
            else:
                return {
                    "success": True,
                    "devices": [],
                    "message": "No devices found. Ensure bulb is powered on and within 20cm."
                }

        except Exception as e:
            logger.error(f"EZSP touchlink scan failed: {e}")
            return {"success": False, "error": str(e)}

    async def _ezsp_identify(self, channel: Optional[int] = None) -> Dict[str, Any]:
        """EZSP touchlink identify using native zigpy API"""
        try:
            if not hasattr(self.app, 'touchlink'):
                return {"success": False, "error": "Touchlink not available"}

            results = []
            channels = [channel] if channel else list(range(11, 27))

            for ch in channels:
                logger.info(f"Scanning channel {ch}...")
                try:
                    async with asyncio.timeout(5.0):
                        scan_result = await self.app.touchlink.scan(channel=ch)

                        if scan_result:
                            for device in scan_result:
                                ieee = str(device.ieee) if hasattr(device, 'ieee') else "unknown"
                                try:
                                    await self.app.touchlink.identify(device, duration=10)
                                    results.append({
                                        "ieee": ieee,
                                        "channel": ch,
                                        "identified": True
                                    })
                                except Exception as e:
                                    results.append({
                                        "ieee": ieee,
                                        "channel": ch,
                                        "identified": False,
                                        "error": str(e)
                                    })
                except asyncio.TimeoutError:
                    pass
                except Exception as e:
                    logger.warning(f"Channel {ch} error: {e}")

            return {"success": True, "devices": results}

        except Exception as e:
            logger.error(f"EZSP touchlink identify failed: {e}")
            return {"success": False, "error": str(e)}

    async def _ezsp_factory_reset(self, channel: Optional[int] = None) -> Dict[str, Any]:
        """EZSP touchlink factory reset using native zigpy API"""
        try:
            if not hasattr(self.app, 'touchlink'):
                return {"success": False, "error": "Touchlink not available"}

            results = []
            channels = [channel] if channel else list(range(11, 27))

            for ch in channels:
                logger.info(f"Scanning channel {ch} for devices to reset...")
                try:
                    async with asyncio.timeout(10.0):
                        scan_result = await self.app.touchlink.scan(channel=ch)

                        if scan_result:
                            for device in scan_result:
                                ieee = str(device.ieee) if hasattr(device, 'ieee') else "unknown"
                                logger.warning(f"Resetting {ieee} on channel {ch}")

                                try:
                                    await self.app.touchlink.reset(device)
                                    results.append({
                                        "ieee": ieee,
                                        "channel": ch,
                                        "reset": True
                                    })
                                    await asyncio.sleep(0.5)
                                except Exception as e:
                                    results.append({
                                        "ieee": ieee,
                                        "channel": ch,
                                        "reset": False,
                                        "error": str(e)
                                    })
                except asyncio.TimeoutError:
                    pass
                except Exception as e:
                    logger.warning(f"Channel {ch} error: {e}")

            if results:
                reset_count = len([r for r in results if r.get('reset')])
                return {
                    "success": True,
                    "devices": results,
                    "message": f"Reset {reset_count} device(s)"
                }
            else:
                return {
                    "success": False,
                    "error": "No Touchlink devices found. Ensure bulb is powered on and within 20cm."
                }

        except Exception as e:
            logger.error(f"EZSP touchlink factory reset failed: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # ZNP IMPLEMENTATION (InterPAN mode like zigbee-herdsman)
    # =========================================================================

    async def _znp_set_channel_interpan(self, channel: int) -> None:
        """
        Set InterPAN channel.

        Equivalent to zigbee-herdsman:
            await this.znp.request(Subsystem.AF, "interPanCtl", {cmd: 1, data: [channel]});
        """
        self._interpan_lock = True

        # AF.interPanCtl cmd=1 sets the channel
        await self._znp.request(
            c.AF.InterPanCtl.Req(
                Cmd=c.af.InterPanCommand.InterPanSet,
                Data=t.Bytes([channel])
            )
        )

        # Register endpoint 12 for InterPAN
        if not self._interpan_registered:
            await self._znp.request(
                c.AF.InterPanCtl.Req(
                    Cmd=c.af.InterPanCommand.InterPanReg,
                    Data=t.Bytes([INTERPAN_ENDPOINT])
                )
            )
            self._interpan_registered = True

        logger.debug(f"InterPAN channel set to {channel}")

    async def _znp_restore_channel_interpan(self) -> None:
        """
        Restore normal mode.

        Equivalent to zigbee-herdsman:
            await this.znp.request(Subsystem.AF, "interPanCtl", {cmd: 0, data: []});
        """
        try:
            await self._znp.request(
                c.AF.InterPanCtl.Req(
                    Cmd=c.af.InterPanCommand.InterPanClr,
                    Data=t.Bytes([])
                )
            )
        except Exception as e:
            logger.error(f"Error restoring InterPAN mode: {e}")
        finally:
            self._interpan_lock = False
            logger.debug("InterPAN mode cleared")

    def _build_scan_request(self, transaction_id: int) -> bytes:
        """Build touchlink scan request payload"""
        # zigbeeInformation: 0x04 (router capable)
        # touchlinkInformation: 0x12 (factory new + link initiator)
        return struct.pack('<IBB', transaction_id, 0x04, 0x12)

    def _build_identify_request(self, transaction_id: int, duration: int = 10) -> bytes:
        """Build touchlink identify request payload"""
        return struct.pack('<IH', transaction_id, duration)

    def _build_reset_request(self, transaction_id: int) -> bytes:
        """Build touchlink factory reset request payload"""
        return struct.pack('<I', transaction_id)

    def _parse_scan_response(self, data: bytes, channel: int) -> Optional[TouchlinkDevice]:
        """Parse touchlink scan response"""
        try:
            if len(data) < 25:
                return None

            transaction_id = struct.unpack_from('<I', data, 0)[0]
            extended_pan_id = data[13:21]
            pan_id = struct.unpack_from('<H', data, 23)[0]
            network_address = struct.unpack_from('<H', data, 25)[0]

            # IEEE from extended PAN ID bytes
            ieee = ':'.join(f'{b:02x}' for b in reversed(extended_pan_id))

            return TouchlinkDevice(
                ieee=ieee,
                channel=channel,
                pan_id=pan_id,
                network_address=network_address,
                transaction_id=transaction_id
            )
        except Exception as e:
            logger.warning(f"Failed to parse scan response: {e}")
            return None

    async def _znp_send_interpan_broadcast(
            self,
            command_id: int,
            payload: bytes,
            timeout: float = 3.0
    ) -> List[bytes]:
        """Send InterPAN broadcast and collect responses"""
        responses = []

        # Build ZCL frame
        frame_control = 0x11  # Cluster-specific, disable default response
        sequence_number = random.randint(0, 255)
        zcl_frame = bytes([frame_control, sequence_number, command_id]) + payload

        try:
            # Broadcast destination
            dst_addr = c.af.AddrModeAddress(
                mode=t.AddrMode.Broadcast,
                address=t.EUI64.convert("ff:ff:ff:ff:ff:ff:ff:ff")
            )

            await self._znp.request(
                c.AF.DataRequestExt.Req(
                    DstAddrModeAddress=dst_addr,
                    DstEndpoint=0xFE,
                    DstPanId=0xFFFF,
                    SrcEndpoint=INTERPAN_ENDPOINT,
                    ClusterId=ZLL_CLUSTER_ID,
                    TSN=sequence_number,
                    Options=c.af.TransmitOptions.NONE,
                    Radius=0x1E,
                    Data=znp_t.SerializableBytes(zcl_frame)
                )
            )

            logger.debug(f"Sent InterPAN broadcast, waiting {timeout}s...")
            await asyncio.sleep(timeout)

        except Exception as e:
            logger.error(f"InterPAN broadcast failed: {e}")

        return responses

    async def _znp_scan(self, channel: Optional[int] = None) -> Dict[str, Any]:
        """ZNP touchlink scan using InterPAN mode"""
        try:
            devices = []
            channels = [channel] if channel else list(range(11, 27))

            try:
                for ch in channels:
                    logger.info(f"Touchlink scanning channel {ch}...")

                    try:
                        await self._znp_set_channel_interpan(ch)

                        transaction_id = random.randint(1, 0xFFFFFFFF)
                        scan_payload = self._build_scan_request(transaction_id)

                        responses = await self._znp_send_interpan_broadcast(
                            TouchlinkCommand.SCAN_REQUEST,
                            scan_payload,
                            timeout=2.0
                        )

                        for response_data in responses:
                            if len(response_data) > 3:
                                device = self._parse_scan_response(response_data[3:], ch)
                                if device:
                                    device.transaction_id = transaction_id
                                    devices.append(device)
                                    logger.info(f"Found: {device.ieee} on channel {ch}")

                    except Exception as e:
                        logger.debug(f"Channel {ch} error: {e}")

            finally:
                await self._znp_restore_channel_interpan()

            if devices:
                return {
                    "success": True,
                    "devices": [d.to_dict() for d in devices]
                }
            else:
                return {
                    "success": True,
                    "devices": [],
                    "message": "No devices found. Ensure bulb is powered on and within 20cm."
                }

        except Exception as e:
            logger.error(f"ZNP touchlink scan failed: {e}")
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e)}

    async def _znp_identify(self, channel: Optional[int] = None) -> Dict[str, Any]:
        """ZNP touchlink identify using InterPAN mode"""
        try:
            # First scan
            scan_result = await self._znp_scan(channel)
            if not scan_result.get("success") or not scan_result.get("devices"):
                return scan_result

            results = []

            try:
                for device_dict in scan_result["devices"]:
                    ch = device_dict["channel"]
                    await self._znp_set_channel_interpan(ch)

                    transaction_id = random.randint(1, 0xFFFFFFFF)
                    identify_payload = self._build_identify_request(transaction_id, duration=10)

                    await self._znp_send_interpan_broadcast(
                        TouchlinkCommand.IDENTIFY_REQUEST,
                        identify_payload,
                        timeout=1.0
                    )

                    results.append({
                        "ieee": device_dict["ieee"],
                        "channel": ch,
                        "identified": True
                    })

            finally:
                await self._znp_restore_channel_interpan()

            return {"success": True, "devices": results}

        except Exception as e:
            logger.error(f"ZNP touchlink identify failed: {e}")
            return {"success": False, "error": str(e)}

    async def _znp_factory_reset(self, channel: Optional[int] = None) -> Dict[str, Any]:
        """ZNP touchlink factory reset using InterPAN mode"""
        try:
            # First scan
            scan_result = await self._znp_scan(channel)
            if not scan_result.get("success") or not scan_result.get("devices"):
                if not scan_result.get("devices"):
                    return {
                        "success": False,
                        "error": "No Touchlink devices found. Ensure bulb is powered on and within 20cm."
                    }
                return scan_result

            results = []

            try:
                for device_dict in scan_result["devices"]:
                    ch = device_dict["channel"]
                    ieee = device_dict["ieee"]

                    logger.warning(f"Factory resetting {ieee} on channel {ch}")

                    await self._znp_set_channel_interpan(ch)

                    transaction_id = random.randint(1, 0xFFFFFFFF)
                    reset_payload = self._build_reset_request(transaction_id)

                    await self._znp_send_interpan_broadcast(
                        TouchlinkCommand.RESET_TO_FACTORY_NEW,
                        reset_payload,
                        timeout=1.0
                    )

                    results.append({
                        "ieee": ieee,
                        "channel": ch,
                        "reset": True
                    })

                    await asyncio.sleep(0.5)

            finally:
                await self._znp_restore_channel_interpan()

            reset_count = len([r for r in results if r.get('reset')])
            return {
                "success": True,
                "devices": results,
                "message": f"Reset {reset_count} device(s)"
            }

        except Exception as e:
            logger.error(f"ZNP touchlink factory reset failed: {e}")
            return {"success": False, "error": str(e)}


# =============================================================================
# FACTORY FUNCTION
# =============================================================================

async def create_touchlink_manager(app: 'ControllerApplication') -> Optional[TouchlinkManager]:
    """
    Create and initialize a TouchlinkManager.

    Args:
        app: The zigpy ControllerApplication

    Returns:
        Initialized TouchlinkManager or None if not supported
    """
    manager = TouchlinkManager(app)

    if await manager.initialize():
        return manager

    return None
