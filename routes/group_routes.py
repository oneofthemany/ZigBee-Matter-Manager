"""
Groups API routes.
Extracted from main.py.
"""
import logging
import os
from typing import Any, Dict

import yaml
from fastapi import FastAPI

from modules.chambers import registry_ids, valid_id

logger = logging.getLogger("routes.groups")

CONFIG_PATH = "./config/config.yaml"


def _load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f) or {}


def register_group_routes(app: FastAPI, get_zigbee_service, get_manager):
    """Register Zigbee group management routes."""

    @app.get("/api/groups")
    async def get_groups():
        """Get all Zigbee groups."""
        try:
            zigbee_service = get_zigbee_service()
            if not hasattr(zigbee_service, 'group_manager'):
                return []
            return zigbee_service.group_manager.get_all_groups()
        except Exception as e:
            logger.error(f"Failed to get groups: {e}")
            return {"error": str(e)}

    @app.post("/api/groups/create")
    async def create_group(data: dict):
        """Create a new Zigbee group."""
        try:
            zigbee_service = get_zigbee_service()
            if not hasattr(zigbee_service, 'group_manager'):
                return {"error": "Group manager not initialized"}

            name = data.get('name')
            devices = data.get('devices', [])

            if not name:
                return {"error": "Group name required"}
            if len(devices) < 2:
                return {"error": "At least 2 devices required"}

            result = await zigbee_service.group_manager.create_group(name, devices)

            if 'success' in result:
                await get_manager().broadcast({
                    "type": "group_created",
                    "group": result['group']
                })

            return result
        except Exception as e:
            logger.error(f"Failed to create group: {e}")
            return {"error": str(e)}

    @app.post("/api/groups/{group_id}/chamber")
    async def set_group_chamber(group_id: int, data: dict):
        """
        Assign (or clear) the chamber a group belongs to. Body: ``{chamber}``.

        ``chamber: null`` clears it. A group with a chamber becomes its own
        controllable cell in that chamber's section of the Frames "by chamber"
        view — the same idea as a device's own chamber assignment.
        """
        try:
            zigbee_service = get_zigbee_service()
            if not hasattr(zigbee_service, 'group_manager'):
                return {"error": "Group manager not initialized"}

            raw_chamber = data.get('chamber')
            chamber_id = None
            if raw_chamber not in (None, ""):
                chamber_id = valid_id(raw_chamber)
                if not chamber_id:
                    return {"error": f"invalid chamber id: {raw_chamber!r}"}
                if chamber_id not in registry_ids(_load_config()):
                    # Reject unknown ids outright — same rule chambers/assign uses:
                    # a typo shouldn't create a phantom chamber no UI renders.
                    return {"error": f"no chamber '{chamber_id}'"}

            result = zigbee_service.group_manager.set_chamber(group_id, chamber_id)
            if 'success' in result:
                await get_manager().broadcast({
                    "type": "group_updated",
                    "group": result['group']
                })
            return result
        except Exception as e:
            logger.error(f"Failed to set group chamber: {e}")
            return {"error": str(e)}

    @app.post("/api/groups/{group_id}/add_device")
    async def add_device_to_group(group_id: int, data: dict):
        """Add device to existing group."""
        try:
            zigbee_service = get_zigbee_service()
            if not hasattr(zigbee_service, 'group_manager'):
                return {"error": "Group manager not initialized"}

            ieee = data.get('ieee')
            if not ieee:
                return {"error": "Device IEEE required"}

            result = await zigbee_service.group_manager.add_device_to_group(group_id, ieee)

            if 'success' in result:
                await get_manager().broadcast({
                    "type": "group_updated",
                    "group": result['group']
                })

            return result
        except Exception as e:
            logger.error(f"Failed to add device to group: {e}")
            return {"error": str(e)}

    @app.post("/api/groups/{group_id}/remove_device")
    async def remove_device_from_group(group_id: int, data: dict):
        """Remove device from group."""
        try:
            zigbee_service = get_zigbee_service()
            if not hasattr(zigbee_service, 'group_manager'):
                return {"error": "Group manager not initialized"}

            ieee = data.get('ieee')
            if not ieee:
                return {"error": "Device IEEE required"}

            result = await zigbee_service.group_manager.remove_device_from_group(group_id, ieee)

            if 'success' in result:
                await get_manager().broadcast({
                    "type": "group_updated",
                    "group": result.get('group')
                })

            return result
        except Exception as e:
            logger.error(f"Failed to remove device from group: {e}")
            return {"error": str(e)}

    @app.delete("/api/groups/{group_id}")
    async def delete_group(group_id: int):
        """Delete a group."""
        try:
            zigbee_service = get_zigbee_service()
            if not hasattr(zigbee_service, 'group_manager'):
                return {"error": "Group manager not initialized"}

            result = await zigbee_service.group_manager.remove_group(group_id)

            if 'success' in result:
                await get_manager().broadcast({
                    "type": "group_deleted",
                    "group_id": group_id
                })

            return result
        except Exception as e:
            logger.error(f"Failed to delete group: {e}")
            return {"error": str(e)}

    @app.post("/api/groups/{group_id}/control")
    async def control_group(group_id: int, data: dict):
        """Control all devices in a group."""
        try:
            zigbee_service = get_zigbee_service()
            if not hasattr(zigbee_service, 'group_manager'):
                return {"error": "Group manager not initialized"}
            return await zigbee_service.group_manager.control_group(group_id, data)
        except Exception as e:
            logger.error(f"Failed to control group: {e}")
            return {"error": str(e)}

    @app.get("/api/devices/{ieee}/compatible")
    async def get_compatible_devices(ieee: str):
        """Get devices compatible with this device for grouping."""
        try:
            zigbee_service = get_zigbee_service()
            if not hasattr(zigbee_service, 'group_manager'):
                return []
            return zigbee_service.group_manager.get_compatible_devices_for(ieee)
        except Exception as e:
            logger.error(f"Failed to get compatible devices: {e}")
            return {"error": str(e)}