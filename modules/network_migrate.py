"""
Write Zigbee network credentials to the coordinator radio.

zigpy applies the `network` config only when it FORMS a network, so a
coordinator that already holds one silently keeps its old credentials and the
key in config.yaml never reaches the radio. Handles staged imports from the
setup wizard and re-forms a virgin network to enforce config.
See docs/onboarding.md.
"""

import json
import logging
import os
import time
from contextlib import suppress

logger = logging.getLogger("modules.network_migrate")

PENDING_RESTORE_PATH = "./data/pending_network_restore.json"
MAX_RESTORE_ATTEMPTS = 3

# When importing credentials without a trustworthy frame counter, jump the
# counter far ahead so devices that saw the old coordinator's traffic don't
# drop ours as replays. The counter is uint32 (~4.3 billion), so over-jumping
# is harmless.
UNKNOWN_COUNTER_JUMP = 1_000_000
BACKUP_COUNTER_JUMP = 25_000


# Pending-restore staging (written by the setup API, consumed at radio start)

def stage_pending_restore(payload: dict) -> None:
    os.makedirs(os.path.dirname(PENDING_RESTORE_PATH) or ".", exist_ok=True)
    payload = dict(payload)
    payload.setdefault("staged_at", time.time())
    with open(PENDING_RESTORE_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    logger.info(f"Staged pending network restore (mode={payload.get('mode')})")


def load_pending_restore() -> dict | None:
    if not os.path.exists(PENDING_RESTORE_PATH):
        return None
    try:
        with open(PENDING_RESTORE_PATH) as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Unreadable pending restore file: {e}")
        return None


def discard_pending_restore() -> None:
    with suppress(OSError):
        os.remove(PENDING_RESTORE_PATH)


def clear_pending_restore(applied: bool) -> None:
    """Rename the staged file so it is applied exactly once but kept for audit."""
    if not os.path.exists(PENDING_RESTORE_PATH):
        return
    suffix = "applied" if applied else "failed"
    dest = PENDING_RESTORE_PATH.replace(".json", f".{suffix}.json")
    with suppress(OSError):
        os.replace(PENDING_RESTORE_PATH, dest)


# Backup construction

def build_backup_from_manual(creds: dict):
    """Build a zigpy NetworkBackup from manually entered credentials."""
    from modules.network_init import (
        normalize_network_key, pan_id_to_int, epid_to_colon_str,
    )

    key_list = normalize_network_key(creds.get("network_key"))
    if key_list is None:
        raise ValueError("Network key must be 16 bytes (32 hex characters)")

    pan_id = pan_id_to_int(creds.get("pan_id"))
    if pan_id is None:
        raise ValueError("PAN ID must be a 16-bit hex value, e.g. '1A2B'")

    epid = epid_to_colon_str(creds.get("extended_pan_id"))
    if epid is None:
        raise ValueError("Extended PAN ID must be 8 bytes (16 hex characters)")

    channel = int(creds.get("channel") or 0)
    if not 11 <= channel <= 26:
        raise ValueError("Channel must be between 11 and 26")

    frame_counter = max(0, int(creds.get("frame_counter") or 0))

    import zigpy.backups
    import zigpy.state
    import zigpy.types as t
    import zigpy.zdo.types as zdo_t

    network_info = zigpy.state.NetworkInfo(
        extended_pan_id=t.ExtendedPanId.convert(epid),
        pan_id=t.PanId(pan_id),
        nwk_update_id=0,
        nwk_manager_id=t.NWK(0x0000),
        channel=t.uint8_t(channel),
        channel_mask=t.Channels.from_channel_list([channel]),
        security_level=t.uint8_t(5),
        network_key=zigpy.state.Key(
            key=t.KeyData(bytes(key_list)),
            seq=int(creds.get("key_sequence_number") or 0),
            tx_counter=frame_counter,
        ),
        tc_link_key=zigpy.state.Key(
            key=t.KeyData(b"ZigBeeAlliance09"),
            partner_ieee=t.EUI64.UNKNOWN,
        ),
        source="zigbee-matter-manager@manual-import",
    )
    node_info = zigpy.state.NodeInfo(
        nwk=t.NWK(0x0000),
        ieee=t.EUI64.UNKNOWN,
        logical_type=zdo_t.LogicalType.Coordinator,
    )
    return zigpy.backups.NetworkBackup(
        network_info=network_info, node_info=node_info,
    )


def build_backup_from_json(obj: dict, overwrite_ieee: bool = False):
    """
    Parse a coordinator backup JSON into a zigpy NetworkBackup.

    Accepts zigpy's own backup format and the Open Coordinator Backup
    format (Zigbee2MQTT's coordinator_backup.json).
    """
    import zigpy.backups
    import zigpy.types as t

    if not isinstance(obj, dict):
        raise ValueError("Backup must be a JSON object")

    try:
        backup = zigpy.backups.NetworkBackup.from_dict(obj)
    except Exception as e:
        raise ValueError(
            f"Unrecognised backup format ({e}). Expected a zigpy/ZHA network "
            f"backup or a Zigbee2MQTT coordinator_backup.json"
        )

    if not overwrite_ieee:
        # Writing a foreign IEEE onto the radio is a one-shot, potentially
        # destructive operation on Silicon Labs adapters. Default to keeping
        # the adapter's own address; devices that negotiated hashed TC link
        # keys against the old IEEE may need a re-pair.
        backup.node_info.ieee = t.EUI64.UNKNOWN
    else:
        backup.network_info.stack_specific.setdefault("ezsp", {})[
            "i_understand_i_can_update_eui64_only_once_and_i_still_want_to_do_it"
        ] = True

    return backup


def credentials_from_backup(backup) -> dict:
    """Extract config.yaml-shaped zigbee credentials from a NetworkBackup."""
    ni = backup.network_info
    return {
        "channel": int(ni.channel),
        "pan_id": f"{int(ni.pan_id):04X}",
        "extended_pan_id": [int(p, 16) for p in str(ni.extended_pan_id).split(":")],
        "network_key": list(bytes(ni.network_key.key)),
    }


# Radio writes

def _bare_conf(conf: dict) -> dict:
    """Radio config without a database — connect/write/disconnect only."""
    bare = dict(conf)
    bare["database_path"] = None
    return bare


async def _write_backup_to_radio(app_cls, conf: dict, backup,
                                 counter_increment: int) -> None:
    app = app_cls(_bare_conf(conf))
    await app.connect()
    try:
        await app.backups.restore_backup(
            backup, counter_increment=counter_increment
        )
    finally:
        with suppress(Exception):
            await app.disconnect()


async def form_network_with_config(app_cls, conf: dict) -> None:
    """Form a network on the radio using conf['network'] credentials."""
    app = app_cls(_bare_conf(conf))
    await app.connect()
    try:
        await app.form_network()
    finally:
        with suppress(Exception):
            await app.disconnect()


async def apply_pending_restore(app_cls, conf: dict) -> bool:
    """
    If the setup wizard staged imported credentials, write them to the radio.
    Returns True when a restore was applied. Gives up after a few failed
    boots so a bad file cannot wedge startup forever.
    """
    pending = load_pending_restore()
    if pending is None:
        return False

    attempts = int(pending.get("attempts") or 0)
    if attempts >= MAX_RESTORE_ATTEMPTS:
        logger.error(
            f"Pending network restore failed {attempts} times — abandoning it "
            f"(see {PENDING_RESTORE_PATH.replace('.json', '.failed.json')})"
        )
        clear_pending_restore(applied=False)
        return False

    pending["attempts"] = attempts + 1
    stage_pending_restore(pending)

    mode = pending.get("mode")
    if mode == "backup":
        backup = build_backup_from_json(
            pending.get("backup") or {},
            overwrite_ieee=bool(pending.get("overwrite_ieee")),
        )
        increment = BACKUP_COUNTER_JUMP
    elif mode == "manual":
        creds = pending.get("credentials") or {}
        backup = build_backup_from_manual(creds)
        # No trustworthy counter from the user → jump far ahead
        increment = (BACKUP_COUNTER_JUMP
                     if int(creds.get("frame_counter") or 0) > 0
                     else UNKNOWN_COUNTER_JUMP)
    else:
        logger.error(f"Unknown pending restore mode: {mode!r} — discarding")
        clear_pending_restore(applied=False)
        return False

    logger.warning(
        f"Writing imported network credentials to the coordinator "
        f"(mode={mode}, channel={int(backup.network_info.channel)}, "
        f"pan_id=0x{int(backup.network_info.pan_id):04X})"
    )
    await _write_backup_to_radio(app_cls, conf, backup, increment)
    clear_pending_restore(applied=True)
    logger.info("✅ Imported network credentials written to coordinator")
    return True


# Verification

def network_mismatches(network_info, network_conf: dict) -> list:
    """
    Compare the live network against the zigpy `network` config section.
    Returns the list of fields that differ; unset config fields are skipped.
    """
    import zigpy.types as t

    diffs = []
    key = network_conf.get("key")
    if key is not None and bytes(network_info.network_key.key) != bytes(key):
        diffs.append("network_key")
    pan = network_conf.get("pan_id")
    if pan is not None and int(network_info.pan_id) != int(pan):
        diffs.append("pan_id")
    epid = network_conf.get("extended_pan_id")
    if epid is not None and \
            network_info.extended_pan_id != t.ExtendedPanId.convert(str(epid)):
        diffs.append("extended_pan_id")
    channel = network_conf.get("channel")
    if channel and int(network_info.channel) != int(channel):
        diffs.append("channel")
    return diffs


# Setup wizard entry point

def apply_network_setup(mode: str, *, credentials: dict | None = None,
                        backup_json: dict | None = None,
                        overwrite_ieee: bool = False,
                        config_path: str = "./config/config.yaml") -> dict:
    """
    Handle the wizard's network-credentials step.

    generate — mint fresh secure credentials into config.yaml; the radio
               picks them up at formation / credential enforcement.
    import   — validate the user's credentials or backup file, write them to
               config.yaml AND stage a pending radio restore.

    Returns a display summary (hex strings) for the wizard.
    """
    import yaml
    from modules.network_init import (
        generate_pan_id, generate_extended_pan_id, generate_network_key,
    )

    if not os.path.exists(config_path):
        raise ValueError(f"{config_path} not found")

    with open(config_path) as f:
        config = yaml.safe_load(f) or {}
    zigbee = config.setdefault("zigbee", {})

    if mode == "generate":
        zigbee["pan_id"] = generate_pan_id()
        zigbee["extended_pan_id"] = generate_extended_pan_id()
        zigbee["network_key"] = generate_network_key()
        # Any previously staged import is now obsolete
        discard_pending_restore()
        source = "generated"
    elif mode == "import":
        if backup_json:
            backup = build_backup_from_json(
                backup_json, overwrite_ieee=overwrite_ieee
            )
            creds = credentials_from_backup(backup)
            stage_pending_restore({
                "mode": "backup",
                "backup": backup_json,
                "overwrite_ieee": bool(overwrite_ieee),
            })
        else:
            backup = build_backup_from_manual(credentials or {})
            creds = credentials_from_backup(backup)
            stage_pending_restore({
                "mode": "manual",
                "credentials": dict(credentials or {}),
            })
        zigbee["channel"] = creds["channel"]
        zigbee["pan_id"] = creds["pan_id"]
        zigbee["extended_pan_id"] = creds["extended_pan_id"]
        zigbee["network_key"] = creds["network_key"]
        source = "imported"
    else:
        raise ValueError(f"Unknown network setup mode: {mode!r}")

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    return {
        "source": source,
        "channel": zigbee.get("channel", 15),
        "pan_id": str(zigbee["pan_id"]),
        "extended_pan_id_hex": "".join(
            f"{b:02X}" for b in zigbee["extended_pan_id"]
        ),
        "network_key_hex": "".join(f"{b:02X}" for b in zigbee["network_key"]),
    }
