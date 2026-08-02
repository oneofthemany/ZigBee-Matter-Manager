"""
Database cleanup mixin for ZigbeeService.
Handles orphaned device detection and database table cleanup.
"""
import asyncio
import logging
import sqlite3
import time

logger = logging.getLogger("core.database")

# Janitor defaults, overridable under zigbee.db_maintenance in config.yaml.
_RECOVER_INTERVAL = 300      # stale-device re-wrap sweep
_SWEEP_INTERVAL = 21600      # orphan removal + history prune (6h)
_HISTORY_RETENTION_DAYS = 30
# Refuse to auto-delete when orphans exceed this share of the device table.
_MAX_ORPHAN_FRACTION = 0.5


class DatabaseMixin:
    """Database cleanup and orphan management methods."""

    def _force_clean_database(self, ieee: str):
        """Force-clean all database tables for a device IEEE.

        Blocking sqlite + duckdb work — call via asyncio.to_thread.
        """
        db_path = "./data/zigbee.db"

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'devices_v%'")
            devices_table = cursor.fetchone()

            if not devices_table:
                logger.warning(f"[{ieee}] Could not detect zigpy table version")
                conn.close()
                return

            version = devices_table[0].split('_')[-1]

            tables = [
                f'devices_{version}', f'endpoints_{version}',
                f'clusters_{version}', f'node_descriptors_{version}',
                f'attributes_cache_{version}', f'neighbors_{version}',
                f'routes_{version}', f'relays_{version}'
            ]

            logger.info(f"[{ieee}] Force cleaning database tables (version: {version})...")

            for table in tables:
                try:
                    cursor.execute(f"DELETE FROM {table} WHERE ieee=?", (ieee,))
                    deleted = cursor.rowcount
                    if deleted > 0:
                        logger.info(f"[{ieee}] Deleted {deleted} rows from {table}")
                except sqlite3.Error as e:
                    logger.debug(f"[{ieee}] Could not clean {table}: {e}")

            conn.commit()
            conn.close()
            logger.info(f"[{ieee}] Database cleanup complete")

        except Exception as e:
            logger.error(f"[{ieee}] Database cleanup failed: {e}")

        # The cached topology/attrs/history live in a separate duckdb file and
        # would otherwise outlive the device, resurfacing under a reused IEEE.
        try:
            from modules.zigbee_cache import purge_device
            purge_device(ieee)
        except Exception as e:
            logger.warning(f"[{ieee}] Cache purge failed: {e}")

    def _read_db_ieees(self):
        """Read every IEEE in zigpy's device table. Blocking — use to_thread.

        Returns None when the table version can't be detected.
        """
        conn = sqlite3.connect("./data/zigbee.db")
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'devices_v%'"
            )
            devices_table = cursor.fetchone()
            if not devices_table:
                return None

            version = devices_table[0].split('_')[-1]
            cursor.execute(f"SELECT ieee FROM devices_{version}")
            return [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()

    async def find_duplicate_devices(self) -> dict:
        """Find devices that exist in database but not in active network."""
        try:
            db_devices = await asyncio.to_thread(self._read_db_ieees)
            if db_devices is None:
                return {"error": "Could not detect table version"}

            # Compare against BOTH zigpy and our wrapper dict
            zigpy_devices = [str(ieee).lower() for ieee in self.app.devices.keys()]
            managed_devices = [ieee.lower() for ieee in self.devices.keys()]
            db_devices_normalized = [ieee.lower() for ieee in db_devices]

            # Orphaned = in DB but not in zigpy (true DB orphans)
            db_orphaned = [ieee for ieee in db_devices_normalized if ieee not in zigpy_devices]

            # Stale = in DB/zigpy but missing from our wrapper dict (lost via device_left)
            stale = [ieee for ieee in db_devices_normalized
                     if ieee in zigpy_devices and ieee not in managed_devices]

            orphaned = list(set(db_orphaned + stale))

            return {
                "total_in_db": len(db_devices),
                "active_zigpy": len(zigpy_devices),
                "active_managed": len(managed_devices),
                "orphaned": orphaned,
                "db_orphaned": db_orphaned,
                "stale": stale,
                "count": len(orphaned)
            }
        except Exception as e:
            logger.error(f"Failed to find duplicate devices: {e}")
            import traceback
            traceback.print_exc()
            return {"error": str(e)}

    async def _remove_orphans(self, ieees) -> tuple:
        """Wipe every persisted row for each IEEE. Returns (removed, failed)."""
        removed, failed = [], []
        for ieee in ieees:
            try:
                await asyncio.to_thread(self._force_clean_database, ieee)
                removed.append(ieee)
                logger.info(f"Removed orphaned device: {ieee}")
            except Exception as e:
                failed.append({"ieee": ieee, "error": str(e)})
                logger.error(f"Failed to remove {ieee}: {e}")
        return removed, failed

    def _recover_stale(self, ieees) -> tuple:
        """Re-wrap zigpy devices missing from self.devices. (recovered, failed)."""
        import zigpy.types
        from device import ZigManDevice

        recovered, failed = [], []
        for ieee in ieees:
            try:
                z_ieee = zigpy.types.EUI64.convert(ieee)

                if z_ieee not in self.app.devices:
                    continue

                zigpy_dev = self.app.devices[z_ieee]

                # Guard: wrapping an un-interviewed device iterates endpoints
                # that may contain ZDO-only stubs — triggers the
                # "No such 'in_clusters' ZDO command" error from zigpy
                endpoints = getattr(zigpy_dev, 'endpoints', {}) or {}
                real_endpoints = {k: v for k, v in endpoints.items() if k != 0}

                if not zigpy_dev.is_initialized or not real_endpoints:
                    logger.info(
                        f"Skipping uninterviewed device {ieee}: "
                        f"is_initialized={zigpy_dev.is_initialized}, "
                        f"endpoints={list(endpoints.keys())}"
                    )
                    continue

                self.devices[ieee] = ZigManDevice(self, zigpy_dev)
                self.devices[ieee]._available = False
                if ieee in self.state_cache:
                    self.devices[ieee].restore_state(self.state_cache[ieee])
                recovered.append(ieee)
                logger.info(f"Recovered stale device: {ieee}")
            except Exception as e:
                failed.append({"ieee": ieee, "error": str(e)})
                logger.error(f"Failed to recover {ieee}: {e}")
        return recovered, failed

    async def cleanup_orphaned_devices(self) -> dict:
        """Remove orphaned devices and recover stale ones."""
        result = await self.find_duplicate_devices()

        if "error" in result:
            return result

        removed, failed = await self._remove_orphans(result.get("db_orphaned", []))
        recovered, recover_failed = self._recover_stale(result.get("stale", []))
        failed += recover_failed

        # Refresh frontend if anything changed
        if removed or recovered:
            self._rebuild_name_maps()

        return {
            "removed": removed,
            "recovered": recovered,
            "failed": failed,
            "count_removed": len(removed),
            "count_recovered": len(recovered),
            "count_failed": len(failed)
        }

    # AUTOMATIC MAINTENANCE

    def _orphan_removal_is_safe(self, result: dict, max_fraction: float):
        """Decide whether an orphan set is safe to delete unattended.

        `db_orphaned` means "in the device table but not in app.devices", which
        only carries that meaning once zigpy has finished loading. A radio that
        failed to come up, or a start() that bailed early, leaves app.devices
        empty or partial and makes the entire network look orphaned — so the
        automatic path deletes nothing unless the live view looks credible.
        Returns (safe, reason).
        """
        if not self.app or not getattr(self.app, "devices", None):
            return False, "zigpy application has no devices loaded"

        total = result.get("total_in_db", 0)
        if not total:
            return False, "device table is empty"

        if not result.get("active_zigpy", 0):
            return False, "no devices active in zigpy"

        orphans = len(result.get("db_orphaned", []))
        if orphans > total * max_fraction:
            return False, (
                f"{orphans}/{total} devices look orphaned "
                f"(over {max_fraction:.0%}) — refusing to delete unattended"
            )

        return True, ""

    async def _db_maintenance_loop(self):
        """Janitor: recover stale devices often, sweep dead rows rarely.

        Recovery is non-destructive repair, so it runs on the short interval.
        Deletion is irreversible and runs on the long one behind
        _orphan_removal_is_safe(); when that refuses, it logs at ERROR (which
        the alert center surfaces) and leaves the work to the manual button.
        """
        cfg = (self._config or {}).get("db_maintenance", {})
        if not cfg.get("enabled", True):
            logger.info("DB maintenance disabled by config")
            return

        recover_interval = int(cfg.get("recover_interval", _RECOVER_INTERVAL))
        sweep_interval = int(cfg.get("sweep_interval", _SWEEP_INTERVAL))
        retention_days = int(cfg.get("history_retention_days", _HISTORY_RETENTION_DAYS))
        max_fraction = float(cfg.get("max_orphan_fraction", _MAX_ORPHAN_FRACTION))

        # Never sweep on a fresh boot: bringup is exactly when the live device
        # view is least trustworthy. The first deletion pass is a full interval
        # away, by which point the radio has proven itself.
        next_sweep = time.monotonic() + sweep_interval
        logger.info(
            f"DB maintenance started (recover every {recover_interval}s, "
            f"sweep every {sweep_interval}s)"
        )

        while True:
            await asyncio.sleep(recover_interval)
            try:
                result = await self.find_duplicate_devices()
                if "error" in result:
                    logger.warning(f"DB maintenance scan failed: {result['error']}")
                    continue

                recovered, _ = self._recover_stale(result.get("stale", []))
                if recovered:
                    self._rebuild_name_maps()
                    logger.info(f"DB maintenance recovered {len(recovered)} stale device(s)")

                if time.monotonic() < next_sweep:
                    continue
                next_sweep = time.monotonic() + sweep_interval

                orphans = result.get("db_orphaned", [])
                if orphans:
                    safe, reason = self._orphan_removal_is_safe(result, max_fraction)
                    if not safe:
                        logger.error(f"Skipped automatic orphan cleanup: {reason}")
                    else:
                        removed, _ = await self._remove_orphans(orphans)
                        if removed:
                            self._rebuild_name_maps()
                            logger.info(
                                f"DB maintenance removed {len(removed)} orphaned device(s)"
                            )

                try:
                    from modules.zigbee_cache import prune_history
                    pruned = await asyncio.to_thread(prune_history, retention_days)
                    if pruned:
                        logger.info(
                            f"Pruned {pruned} attribute history rows "
                            f"older than {retention_days}d"
                        )
                except Exception as e:
                    logger.warning(f"History prune failed: {e}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"DB maintenance pass failed: {e}")
