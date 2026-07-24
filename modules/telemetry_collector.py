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
from collections import deque
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("modules.telemetry_collector")

FLUSH_INTERVAL = 60          # seconds between packet stats flushes
PRUNE_INTERVAL = 86400       # seconds between retention prune runs (24h)
DEFAULT_RETENTION_DAYS = 30
APPENDER_FLUSH_INTERVAL = 5  # seconds — keeps History tab queries near-realtime
SNAPSHOT_INTERVAL = 3600     # seconds between keep-alive state snapshots
LINK_SAMPLE_INTERVAL = 300   # seconds between LQI/RSSI samples
STATE_DRAIN_INTERVAL = 2     # seconds between drains of the state-change buffer

# Upper bound on buffered state rows awaiting write. Only approached if the
# DB is wedged (e.g. DuckDB replaying a damaged WAL); at that point dropping
# the oldest telemetry is strictly better than growing until the OOM killer
# takes the process.
STATE_BUFFER_MAX = 50_000

# A healthy drain keeps the buffer near zero, so a sustained backlog past this
# means the DB is falling behind. Warned about while there is still headroom,
# rather than silently at the point rows start being discarded.
STATE_BUFFER_WARN = STATE_BUFFER_MAX // 10

# Keep-alive rows must only be written for devices that have actually
# communicated recently. Cached state survives long after a device goes
# silent, and consumers like the heating controller's freshness check treat
# a DuckDB row as proof of a live report — writing stale cached values
# would mask a dead sensor.
KEEPALIVE_MAX_SILENCE = 2 * SNAPSHOT_INTERVAL  # seconds since last_seen


def _seen_recently(dev, max_silence_sec: float) -> bool:
    """True if the device communicated within max_silence_sec (last_seen is ms)."""
    last_seen_ms = getattr(dev, 'last_seen', 0) or 0
    return (time.time() - last_seen_ms / 1000.0) < max_silence_sec

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
        # Pending (ieee, attribute, value) rows from record_state_change,
        # drained by _state_drain_loop. deque.append/popleft are atomic under
        # the GIL, so producers never need a lock.
        self._state_buffer: deque = deque(maxlen=STATE_BUFFER_MAX)
        self._state_dropped = 0            # since last drain (for reporting)
        self._state_dropped_total = 0      # cumulative, for get_stats()
        self._state_high_water = 0
        self._state_write_failures = 0
        self._state_backlog_warned = False
        self._fatal_logged = False
        self._last_batch = 0
        self._last_drain_ts: Optional[float] = None

    def start(self):
        """Start background flush and prune tasks."""
        if not self._running:
            self._running = True
            self._flush_task = asyncio.create_task(self._flush_loop())
            self._prune_task = asyncio.create_task(self._prune_loop())
            self._appender_flush_task = asyncio.create_task(self._appender_flush_loop())
            self._snapshot_task = asyncio.create_task(self._snapshot_loop())
            self._link_task = asyncio.create_task(self._link_sample_loop())
            self._state_drain_task = asyncio.create_task(self._state_drain_loop())
            logger.info("Telemetry collector started")

    def stop(self):
        """Stop background tasks."""
        self._running = False
        for task in (self._flush_task, self._prune_task,
                     getattr(self, '_appender_flush_task', None),
                     getattr(self, '_snapshot_task', None),
                     getattr(self, '_link_task', None),
                     getattr(self, '_state_drain_task', None)):
            if task:
                task.cancel()

        # Best-effort final drain so buffered rows aren't lost on shutdown.
        # Blocking here is fine — the loop is going away regardless.
        try:
            self._drain_state_buffer()
        except Exception as e:
            logger.debug(f"Final state drain failed: {e}")


    async def _appender_flush_loop(self):
        """Drain the Rust appender's buffers periodically so readers see fresh rows."""
        while self._running:
            try:
                await asyncio.sleep(APPENDER_FLUSH_INTERVAL)
            except asyncio.CancelledError:
                break
            try:
                from modules.telemetry_db import flush_appender
                # Off the loop thread: the DuckDB drain can take seconds on a
                # grown DB, and blocking the loop here starved the bellows ASH
                # serial ACKs → NCP ERROR_EXCEEDED_MAXIMUM_ACK_TIMEOUT_COUNT →
                # watchdog stalls. The Rust flush now releases the GIL, so the
                # loop keeps running while this worker drains.
                await asyncio.to_thread(flush_appender)
            except Exception as e:
                logger.debug(f"Appender flush loop error: {e}")

    async def _state_drain_loop(self):
        """
        Write buffered device-state changes in batches, off the loop thread.

        record_state_change() runs on the event loop for every attribute
        update from every device, so it must never touch the DB lock itself:
        the first write after an abrupt restart can block for a minute while
        DuckDB replays the WAL, which stalls the whole app and trips
        loop_monitor's 60s self-restart. It buffers instead, and this loop
        does the actual write in a worker thread.
        """
        while self._running:
            try:
                await asyncio.sleep(STATE_DRAIN_INTERVAL)
            except asyncio.CancelledError:
                break
            try:
                await asyncio.to_thread(self._drain_state_buffer)
            except Exception as e:
                logger.debug(f"State drain error: {e}")

    def _drain_state_buffer(self):
        """Pop everything currently buffered and write it in one commit."""
        buf = self._state_buffer

        depth = len(buf)
        if depth > self._state_high_water:
            self._state_high_water = depth

        # Early warning while there is still headroom, and an explicit
        # recovery notice so a transient spike doesn't look permanent.
        if depth >= STATE_BUFFER_WARN and not self._state_backlog_warned:
            self._state_backlog_warned = True
            logger.warning(
                f"Telemetry state buffer backlog: {depth:,} rows pending "
                f"({depth * 100 // STATE_BUFFER_MAX}% of capacity) — "
                f"the telemetry DB is falling behind."
            )
        elif self._state_backlog_warned and depth < STATE_BUFFER_WARN // 2:
            self._state_backlog_warned = False
            logger.info(f"Telemetry state buffer recovered ({depth:,} pending)")

        batch = []
        while True:
            try:
                batch.append(buf.popleft())
            except IndexError:
                break

        if not batch:
            return

        dropped, self._state_dropped = self._state_dropped, 0
        if dropped:
            logger.warning(
                f"Telemetry state buffer overflowed — {dropped:,} row(s) dropped. "
                f"The telemetry DB is not keeping up with writes."
            )

        from modules.telemetry_db import write_device_states_batch, is_fatal
        try:
            write_device_states_batch(batch)
        except Exception as e:
            # The rows are already popped, so a failed batch is lost. Say so
            # rather than letting it vanish into a debug line upstream — but
            # only once when the DB has gone terminal, since it will otherwise
            # repeat every drain until the app restarts.
            self._state_write_failures += 1
            if is_fatal():
                if not self._fatal_logged:
                    self._fatal_logged = True
                    logger.warning(
                        f"Telemetry DB unusable — dropping buffered state rows "
                        f"until restart (lost {len(batch):,} in this batch). "
                        f"telemetry_db has raised an alert with the details."
                    )
            else:
                logger.warning(
                    f"Telemetry batch write failed — {len(batch):,} row(s) lost: {e}"
                )
            return

        self._last_batch = len(batch)
        self._last_drain_ts = time.time()

    def get_stats(self) -> Dict[str, Any]:
        """Cheap snapshot of the state-write buffer, for /api/system/health.

        Reads only a len() and some ints, so it is safe to call from the
        event loop and from the health endpoint's no-I/O contract.
        """
        last_drain = self._last_drain_ts
        return {
            "buffered": len(self._state_buffer),
            "capacity": STATE_BUFFER_MAX,
            "high_water": self._state_high_water,
            "dropped": self._state_dropped_total,
            "write_failures": self._state_write_failures,
            "db_fatal": self._fatal_logged,
            "last_batch": self._last_batch,
            "last_drain_age_s": (round(time.time() - last_drain, 1)
                                 if last_drain else None),
        }

    async def _flush_loop(self):
        """Periodically flush packet stats to DuckDB."""
        await asyncio.sleep(30)  # Initial delay

        while self._running:
            try:
                # Worker thread: write_packet_stats takes the DB lock, and
                # blocking the loop on it stalls the app (see _state_drain_loop).
                await asyncio.to_thread(self._flush_packet_stats)
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
                # Worker thread: the snapshot writes hundreds of rows, and a
                # blocking DB write on the event loop stalls the whole app
                # (seen as 5s+ loop-monitor stalls / watchdog restarts).
                await asyncio.to_thread(self._snapshot_states)
            except Exception as e:
                logger.debug(f"State snapshot error: {e}")

            try:
                await asyncio.sleep(SNAPSHOT_INTERVAL)
            except asyncio.CancelledError:
                break

    def _snapshot_states(self):
        """Write keep-alive rows for attributes with no recent write.

        Runs in a worker thread (see _snapshot_loop) and batches every row
        into a single commit — per-row commits took seconds for a full house
        of devices.
        """
        from modules.telemetry_db import write_device_states_batch

        if not hasattr(self, '_dedup_state'):
            self._dedup_state: Dict[tuple, tuple] = {}

        now = time.time()
        batch = []

        for ieee, dev in (self._get_devices() or {}).items():
            try:
                if not getattr(dev, '_available', False):
                    continue  # offline devices have nothing new to say
                if not _seen_recently(dev, KEEPALIVE_MAX_SILENCE):
                    continue  # silent device — cached state is unverified
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

                    batch.append((ieee, attr, value))
                    self._dedup_state[(ieee, attr)] = (value, now)
            except Exception as e:
                logger.debug(f"[{ieee}] state snapshot error: {e}")

        if batch:
            write_device_states_batch(batch)
            logger.debug(f"State snapshot: {len(batch)} keep-alive rows written")

    async def _link_sample_loop(self):
        """
        Sample link quality (LQI/RSSI) from zigpy every LINK_SAMPLE_INTERVAL.

        zigpy updates `zigpy_dev.lqi`/`rssi` on every received frame, but
        those values never flow through update_state — the device list reads
        them live at serialisation time, so History had no rows for them.
        This loop copies the live values into device state (so the History
        dropdown sees them) and writes a row when the value changed; the
        hourly keep-alive covers the unchanged case, and query-time
        carry-forward fills the gaps in between.
        """
        await asyncio.sleep(90)

        while self._running:
            try:
                # Worker thread: writes one row per changed LQI/RSSI value
                # across every device, all under the DB lock.
                await asyncio.to_thread(self._sample_link_quality)
            except Exception as e:
                logger.debug(f"Link quality sample error: {e}")

            try:
                await asyncio.sleep(LINK_SAMPLE_INTERVAL)
            except asyncio.CancelledError:
                break

    def _sample_link_quality(self):
        """Record changed LQI/RSSI values for online devices."""
        from modules.telemetry_db import write_device_state

        if not hasattr(self, '_dedup_state'):
            self._dedup_state: Dict[tuple, tuple] = {}

        now = time.time()
        for ieee, dev in (self._get_devices() or {}).items():
            try:
                if not getattr(dev, '_available', False):
                    continue
                if not _seen_recently(dev, KEEPALIVE_MAX_SILENCE):
                    continue  # silent device — zigpy's lqi/rssi are stale
                zigpy_dev = getattr(dev, 'zigpy_dev', None)
                if zigpy_dev is None:
                    continue

                for attr in ('lqi', 'rssi'):
                    value = getattr(zigpy_dev, attr, None)
                    if value is None:
                        continue
                    value = int(value)

                    dev_state = getattr(dev, 'state', None)
                    if isinstance(dev_state, dict):
                        dev_state[attr] = value

                    prev = self._dedup_state.get((ieee, attr))
                    if prev is not None and prev[0] == value \
                            and (now - prev[1]) < SNAPSHOT_INTERVAL:
                        continue  # unchanged — hourly keep-alive covers it

                    write_device_state(ieee, attr, value)
                    self._dedup_state[(ieee, attr)] = (value, now)
            except Exception as e:
                logger.debug(f"[{ieee}] link quality sample error: {e}")

    async def _prune_loop(self):
        """Daily retention pruning."""
        await asyncio.sleep(3600)  # First prune after 1 hour

        while self._running:
            try:
                from modules.telemetry_db import prune
                # Multi-table DELETEs take seconds on a grown DB — worker thread
                await asyncio.to_thread(prune, retention_days=self._retention_days)
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
        Queue attribute changes for persistence to DuckDB.

        Runs on the event loop for every attribute update from every device,
        so it only does in-memory work: filtering, dedup, and a buffer append.
        The write itself happens in _state_drain_loop's worker thread — see
        the note there for why touching the DB lock from here is unsafe.

        Includes a short-window dedup: if the same (ieee, attribute, value)
        has already been queued within _DEDUP_WINDOW_SECONDS, the new write
        is dropped. This collapses the duplicate writes that arise when a
        poll's read_attributes() triggers both the zigpy attribute_updated
        callback path and the handler-return path within microseconds.
        Out-of-window changes (real new values, or repeats > 1s apart) are
        always written.
        """
        if not changed_attrs:
            return

        # DB has gone terminal (set by the drain): buffering would just fill
        # to capacity and start reporting overflows for writes that can never
        # land. Drop cheaply until the app restarts.
        if self._fatal_logged:
            return

        # Lazy state init
        if not hasattr(self, '_dedup_state'):
            self._dedup_state: Dict[tuple, tuple] = {}

        now = time.time()

        try:
            buf = self._state_buffer
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

                # maxlen makes append evict the oldest row silently, so count
                # the loss ourselves and let the drain report it.
                if len(buf) >= STATE_BUFFER_MAX:
                    self._state_dropped += 1
                    self._state_dropped_total += 1
                buf.append((ieee, attr, value))
                self._dedup_state[key] = (value, now)

            # Periodically prune cold entries to bound memory growth
            if len(self._dedup_state) > 5000:
                cutoff = now - 3600  # entries idle > 1h evicted
                self._dedup_state = {
                    k: v for k, v in self._dedup_state.items() if v[1] >= cutoff
                }

        except Exception as e:
            logger.debug(f"[{ieee}] record_state_change error: {e}")