"""
Telemetry Collector - Bridges live data into DuckDB
=====================================================
Periodically flushes in-memory packet_stats to DuckDB and handles
device state change recording. Also runs daily retention pruning.

Hook into main.py after system_monitor and telemetry_db are ready.
"""

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("modules.telemetry_collector")

FLUSH_INTERVAL = 60          # seconds between packet stats flushes
PRUNE_INTERVAL = 86400       # seconds between retention prune runs (24h)
DEFAULT_RETENTION_DAYS = 30
APPENDER_FLUSH_INTERVAL = 5  # seconds — keeps History tab queries near-realtime
SNAPSHOT_INTERVAL = 3600     # seconds between keep-alive state snapshots

# Attributes that are metadata, not telemetry — never recorded.
SKIP_ATTRS = {"manufacturer", "model", "power_source", "last_seen", "available"}


class TelemetryCollector:
    """
    Bridges live in-memory data into the DuckDB telemetry store.

    Responsibilities:
      1. Periodic flush of packet_stats singleton → DuckDB
      2. Device state change recording (called from core.py)
      3. Daily retention pruning
    """

    def __init__(self, device_registry_getter: Callable,
                 retention_days: int = DEFAULT_RETENTION_DAYS):
        self._get_devices = device_registry_getter
        self._retention_days = retention_days
        self._flush_task: Optional[asyncio.Task] = None
        self._prune_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_packet_snapshot: Dict[str, Dict] = {}

    def start(self):
        """Start background flush and prune tasks."""
        if not self._running:
            self._running = True
            self._flush_task = asyncio.create_task(self._flush_loop())
            self._prune_task = asyncio.create_task(self._prune_loop())
            self._appender_flush_task = asyncio.create_task(self._appender_flush_loop())
            self._snapshot_task = asyncio.create_task(self._snapshot_loop())
            logger.info("Telemetry collector started")

    def stop(self):
        """Stop background tasks."""
        self._running = False
        for task in (self._flush_task, self._prune_task,
                     getattr(self, '_appender_flush_task', None),
                     getattr(self, '_snapshot_task', None)):
            if task:
                task.cancel()


    async def _appender_flush_loop(self):
        """Drain the Rust appender's buffers periodically so readers see fresh rows."""
        while self._running:
            try:
                await asyncio.sleep(APPENDER_FLUSH_INTERVAL)
            except asyncio.CancelledError:
                break
            try:
                from modules.telemetry_db import flush_appender
                flush_appender()
            except Exception as e:
                logger.debug(f"Appender flush loop error: {e}")

    async def _flush_loop(self):
        """Periodically flush packet stats to DuckDB."""
        await asyncio.sleep(30)  # Initial delay

        while self._running:
            try:
                self._flush_packet_stats()
            except Exception as e:
                logger.debug(f"Packet stats flush error: {e}")

            try:
                await asyncio.sleep(FLUSH_INTERVAL)
            except asyncio.CancelledError:
                break

    async def _snapshot_loop(self):
        """
        Hourly keep-alive snapshot of device state.

        Recording is change-driven, so a static attribute (child_lock,
        sensitivity, startup_behaviour, ...) only gets a row when the app
        restarts — the History tab then shows "No data in this range" for
        any window that doesn't include a restart. This loop re-writes the
        current value of every state attribute that hasn't been written in
        the last SNAPSHOT_INTERVAL seconds, so each attribute always has
        recent rows and survives retention pruning.
        """
        await asyncio.sleep(120)  # let devices restore cached state first

        while self._running:
            try:
                self._snapshot_states()
            except Exception as e:
                logger.debug(f"State snapshot error: {e}")

            try:
                await asyncio.sleep(SNAPSHOT_INTERVAL)
            except asyncio.CancelledError:
                break

    def _snapshot_states(self):
        """Write keep-alive rows for attributes with no recent write."""
        from modules.telemetry_db import write_device_state

        if not hasattr(self, '_dedup_state'):
            self._dedup_state: Dict[tuple, tuple] = {}

        now = time.time()
        written = 0

        for ieee, dev in (self._get_devices() or {}).items():
            try:
                if not getattr(dev, '_available', False):
                    continue  # offline devices have nothing new to say
                dev_state = getattr(dev, 'state', None)
                if not isinstance(dev_state, dict):
                    continue

                for attr, value in list(dev_state.items()):
                    if attr in SKIP_ATTRS:
                        continue
                    if attr.endswith('_raw') or attr.startswith('attr_'):
                        continue

                    prev = self._dedup_state.get((ieee, attr))
                    if prev is not None and (now - prev[1]) < SNAPSHOT_INTERVAL:
                        continue  # written recently — no keep-alive needed

                    write_device_state(ieee, attr, value)
                    self._dedup_state[(ieee, attr)] = (value, now)
                    written += 1
            except Exception as e:
                logger.debug(f"[{ieee}] state snapshot error: {e}")

        if written:
            logger.debug(f"State snapshot: {written} keep-alive rows written")

    async def _prune_loop(self):
        """Daily retention pruning."""
        await asyncio.sleep(3600)  # First prune after 1 hour

        while self._running:
            try:
                from modules.telemetry_db import prune
                prune(retention_days=self._retention_days)
            except Exception as e:
                logger.warning(f"Telemetry prune error: {e}")

            try:
                await asyncio.sleep(PRUNE_INTERVAL)
            except asyncio.CancelledError:
                break

    def _flush_packet_stats(self):
        """
        Read current packet_stats singleton, compute deltas since last
        flush, and write the deltas to DuckDB.
        """
        from modules.packet_stats import packet_stats
        from modules.telemetry_db import write_packet_stats

        current = packet_stats.get_all_stats()
        batch = []

        for ieee, stats in current.items():
            prev = self._last_packet_snapshot.get(ieee, {})

            # Compute deltas
            delta_rx = stats.get("rx_packets", 0) - prev.get("rx_packets", 0)
            delta_tx = stats.get("tx_packets", 0) - prev.get("tx_packets", 0)
            delta_rx_bytes = stats.get("rx_bytes", 0) - prev.get("rx_bytes", 0)
            delta_tx_bytes = stats.get("tx_bytes", 0) - prev.get("tx_bytes", 0)
            delta_errors = stats.get("errors", 0) - prev.get("errors", 0)
            delta_retries = stats.get("retries", 0) - prev.get("retries", 0)

            # Only write if there was activity
            if delta_rx > 0 or delta_tx > 0 or delta_errors > 0:
                # Get LQI from device if available
                lqi = 0
                try:
                    devices = self._get_devices()
                    dev = devices.get(ieee)
                    if dev and hasattr(dev, 'zigpy_dev'):
                        lqi = getattr(dev.zigpy_dev, 'lqi', 0) or 0
                except Exception:
                    pass

                batch.append({
                    "ieee": ieee,
                    "rx_packets": delta_rx,
                    "tx_packets": delta_tx,
                    "rx_bytes": delta_rx_bytes,
                    "tx_bytes": delta_tx_bytes,
                    "errors": delta_errors,
                    "retries": delta_retries,
                    "lqi": lqi,
                })

        if batch:
            write_packet_stats(batch)
            logger.debug(f"Flushed packet stats: {len(batch)} devices")

        # Store current as the baseline for next delta
        self._last_packet_snapshot = current

    # Per-device dedup state. Keyed by (ieee, attribute), value is
    # (last_numeric_or_str_value, last_ts_epoch). Trimmed lazily.
    # 2s window: the duplicate writes from the double record path
    # (update_state + handle_device_update) arrive 0.25–1s apart in
    # practice, and a same-value re-report within 2s carries no
    # information — real value changes always write regardless.
    _DEDUP_WINDOW_SECONDS = 2.0  # collapse writes for same (ieee,attr,value)
    # arriving within this many seconds

    def record_state_change(self, ieee: str, changed_attrs: Dict[str, Any]):
        """
        Persist attribute changes to DuckDB.

        Includes a short-window dedup: if the same (ieee, attribute, value)
        has already been written within _DEDUP_WINDOW_SECONDS, the new write
        is dropped. This collapses the duplicate writes that arise when a
        poll's read_attributes() triggers both the zigpy attribute_updated
        callback path and the handler-return path within microseconds.
        Out-of-window changes (real new values, or repeats > 1s apart) are
        always written.
        """
        if not changed_attrs:
            return

        # Lazy state init
        if not hasattr(self, '_dedup_state'):
            self._dedup_state: Dict[tuple, tuple] = {}

        now = time.time()

        try:
            from modules.telemetry_db import write_device_state
            for attr, value in changed_attrs.items():
                if attr in SKIP_ATTRS:
                    continue
                if attr.endswith('_raw') or attr.startswith('attr_'):
                    continue

                key = (ieee, attr)
                prev = self._dedup_state.get(key)
                if prev is not None:
                    prev_value, prev_ts = prev
                    if prev_value == value and (now - prev_ts) < self._DEDUP_WINDOW_SECONDS:
                        # Same value, same device, same attr, within window -> skip
                        continue

                write_device_state(ieee, attr, value)
                self._dedup_state[key] = (value, now)

            # Periodically prune cold entries to bound memory growth
            if len(self._dedup_state) > 5000:
                cutoff = now - 3600  # entries idle > 1h evicted
                self._dedup_state = {
                    k: v for k, v in self._dedup_state.items() if v[1] >= cutoff
                }

        except Exception as e:
            logger.debug(f"[{ieee}] record_state_change error: {e}")