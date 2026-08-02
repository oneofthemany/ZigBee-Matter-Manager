# Zigbee Manager Debugging Guide

## Overview

The Zigbee Manager includes comprehensive debugging capabilities to help troubleshoot device communication issues, especially for motion sensors and other battery-powered devices.

## Features

### 1. Frontend Debug Controls
- **Enable/Disable Debugging**: Toggle debugging on/off via the web interface
- **File Logging**: Optionally log all debug data to rotating log files
- **Live Packet View**: See Zigbee packets in real-time
- **Motion Event Tracking**: Dedicated view for motion sensor events
- **Downloadable Logs**: Download debug logs for offline analysis

### 2. Debug Capabilities
- Raw Zigbee packet capture with full decoding
- ZCL frame header and payload parsing
- Cluster command identification
- Attribute report tracking
- Handler trigger monitoring
- Motion detection event logging
- IAS Zone status change tracking
- On/Off cluster command analysis (including `on_with_timed_off`)

### 3. File Logging
- Automatic log rotation (10MB per file, 5 backups)
- Structured log format for easy parsing
- Separate debug log from main application log
- Configurable retention policies

## Usage

### Enabling Debugging

1. **Via Web Interface**:
   - Navigate to the "Debug Log" tab
   - Click "Enable Debug" button
   - Debugging starts immediately with file logging

2. **Via API**:
   ```bash
   curl -X POST http://localhost:8000/api/debug/enable
   ```

### Viewing Debug Data

1. **Live Logs**: Real-time logs appear in the Debug tab

2. **Packet View**: Click "Packets" to see captured Zigbee packets with:
   - Timestamp
   - Source device
   - Direction (RX/TX)
   - Cluster information
   - Command details
   - Decoded payload

3. **Motion Events**: Click "Motion" to see motion detection events including:
   - Detection source (IAS Zone, Occupancy Sensing, On/Off cluster)
   - Detection time and duration
   - Device information

4. **Download Logs**: Click "Download" to save debug logs locally

### Filtering

- **Log Level**: Filter by INFO, WARNING, ERROR, DEBUG
- **Device**: Filter by IEEE address
- **Packet Importance**: Filter by critical, high, medium, normal
- **Cluster**: Filter by cluster ID

## Understanding the Output

### Philips Hue Motion Sensor Example

When a Philips Hue SML001 motion sensor detects motion, you'll see:

```
[18:06:19.826] RX | [00:17:88:] | EP1→1 | On/Off
CMD: On With Timed Off | 🚨 MOTION DETECTED
→ OnOffHandler

Details:
- on_off_control: 0
- on_time: 3000 (300 seconds)
- off_wait_time: 0
```

### IAS Zone Motion Sensor Example

For sensors using IAS Zone cluster:

```
[18:06:19.826] RX | [00:17:88:] | EP1→1 | IAS Zone
CMD: Zone Status Change Notification | 🚨 MOTION DETECTED
→ IASZoneHandler

Zone Status: 0x0001
- alarm1_motion: True
- tamper: False
- battery_low: False
```

### Attribute Reports

Temperature sensor reporting:

```
[18:06:19.826] RX | [00:17:88: | EP1→1 | Temperature Measurement
Report Attributes | temperature=22.5°C
→ TemperatureMeasurementHandler
```

## Log Files

### Locations

- **Main Log**: `logs/zigbee.log`
- **Debug Log**: `logs/zigbee_debug.log`
- **Rotated Logs**: `logs/zigbee_debug.log.YYYYMMDD`

### Log Format

```
[TIMESTAMP] | DIRECTION | [IEEE] | EP_SRC→EP_DST | CLUSTER_NAME | DETAILS
```

Example:
```
2025-01-26 18:06:19.826 | 📡 [00:17:88:] On/Off cluster_command callback! tsn=24, cmd=0x42, args=(0, 3000, 0)
2025-01-26 18:06:19.827 | 🚨 MOTION: [00:17:88:] via on_with_timed_off (on_time=300.0s)
```

## Log Rotation

### Automatic Rotation (Built-in)

The Python `RotatingFileHandler` automatically rotates logs when they reach 10MB.

### System Logrotate (Recommended)

For production deployments, use the provided logrotate configuration:

```bash
# Install logrotate config
sudo cp zigbee-logrotate.conf /etc/logrotate.d/zigbee-gateway
sudo chmod 644 /etc/logrotate.d/zigbee-gateway

# Test configuration
sudo logrotate -d /etc/logrotate.d/zigbee-gateway

# Force rotation (for testing)
sudo logrotate -f /etc/logrotate.d/zigbee-gateway
```

### Customising Rotation

Edit `/etc/logrotate.d/zigbee-gateway` to customize:

- **Rotation frequency**: `daily`, `hourly`, `weekly`, `monthly`, `size 10M`
- **Retention**: `rotate 7` (keep 7 rotated files)
- **Compression**: `compress`, `delaycompress`
- **Max age**: `maxage 30` (delete files older than 30 days)

## Troubleshooting Common Issues

### Motion Sensor Not Triggering

1. **Enable debugging** to see raw Zigbee traffic
2. **Check for packets** from the sensor's IEEE address
3. **Look for**:
   - IAS Zone Status Change Notifications (cluster 0x0500)
   - Occupancy Sensing attribute reports (cluster 0x0406)
   - On/Off cluster commands (cluster 0x0006, command 0x42)

4. **Verify handler registration**:
   - Look for "✅ HANDLER" or "✅ COMMAND" log entries
   - Ensure the appropriate handler is triggered

### Debug Logs Growing Too Large

1. **Disable debugging** when not needed
2. **Configure logrotate** for more aggressive rotation:
   ```
   # Rotate every hour, keep only 10 files
   hourly
   rotate 10
   size 5M
   ```

3. **Use filters** to reduce captured data:
   ```python
   debugger.set_filter(ieee="00:17:88:)  # Only one device
   debugger.set_filter(cluster=0x0006)  # Only On/Off cluster
   ```

### Performance Impact

Debugging has minimal performance impact:
- **CPU**: <5% increase
- **Memory**: ~50MB for 1000 packets
- **Disk I/O**: ~1MB/minute during active debugging

Disable debugging in production or use filters to minimize overhead.

## API Reference

### Enable Debugging
```
POST /api/debug/enable
Body: { "file_logging": true }
Response: { "status": "enabled", "file_logging": true }
```

### Disable Debugging
```
POST /api/debug/disable
Response: { "status": "disabled" }
```

### Get Debug Status
```
GET /api/debug/status
Response: {
  "enabled": true,
  "file_logging": true,
  "packets_captured": 1234,
  "total_packets": 5678,
  "motion_events": 42
}
```

### Get Packets
```
GET /api/debug/packets?limit=100&importance=critical
Response: {
  "success": true,
  "packets": [...]
}
```

### Get Motion Events
```
GET /api/debug/motion_events?limit=50
Response: {
  "success": true,
  "events": [...]
}
```

### Download Log File
```
GET /api/debug/log_file?lines=1000
Response: {
  "success": true,
  "content": "log file contents..."
}
```

## Best Practices

1. **Enable debugging only when needed** - it generates significant log data
2. **Use filters** to focus on specific devices or clusters
3. **Configure logrotate** for production environments
4. **Monitor disk space** when debugging is enabled for extended periods
5. **Download and analyse logs offline** for complex issues
6. **Disable file logging** if you only need live monitoring
7. **Clear debug data** periodically to free memory

## Advanced Debugging

### Analysing Packet Flows

1. Enable debugging
2. Trigger the issue (e.g., move in front of motion sensor)
3. Download debug log
4. analyse the sequence of packets:
   ```
   grep "00:17:88:" zigbee_debug.log
   ```

### Identifying Missing Handlers

Look for "No explicit handler for cluster command" messages:

```
DEBUG:zigpy.zcl:[0xC58E:1:0x0006] No explicit handler for cluster command 0x42
```

This indicates a command was received but no handler processed it.

### Checking Binding Status

Enable debugging and look for binding-related messages during device configuration:

```
INFO:handlers.base:[00:17:88:] ✅ Bound On/Off, result: [0]
```

## Support

For issues or questions:
1. Enable debugging
2. Reproduce the issue
3. Download debug logs
4. Create an issue with logs attached

## License

See main project LICENSE file.
## Signal Inspector

`modules/signal_inspector.py` is universal, device-agnostic signal capture.

The onboarding pain across all IoT devices — Zigbee or Matter, standard ZCL or
vendor-proprietary — is the same: you cannot map what you cannot see. This
module gives one live view of *every raw signal a device emits*, whichever
handler produced it.

The trick is that everything a device says already converges on a small number
of choke points:

| Choke point | Covers |
| --- | --- |
| `ClusterHandler.attribute_updated` | every ZCL / manufacturer attribute report, with its raw address (endpoint, cluster, attribute). Inherited by every handler, so universal for Zigbee. |
| `ClusterHandler.cluster_command` | every cluster command received — button presses, Tuya DP reports, scene recalls. |
| `device.update_state` | the catch-all: Tuya datapoints (`dp_16`), Matter attributes, and any friendly or derived key a handler computes. |

Each of those calls `record()`. Nothing here depends on knowing the device
*type* — a signal is just `(source, address, value)`. That is what makes the
inspector work for a device nobody has written a handler for, and what lets a
future data-driven layer gradually replace the hard-coded handlers: you can see
the raw address a handler derives from and map it yourself.

Recording is always on — it is a dict update per report. Live streaming to the
frontend happens only for devices the user is actively inspecting
(`start(ieee)` / `stop(ieee)`), so idle devices cost nothing on the wire.

The module is intentionally free of device-class knowledge, and never raises
into the handler path: every public entry point swallows its own errors.

### Signal Inspector (frontend)

`static/js/modal/signals.js` is the live table. It is the surface the future
learn-by-demonstration flow plugs into: press a button or turn a knob and watch
which signal reacts — that is the address you map.

Mounted in two places from one implementation: the per-device modal (pinned to
the open device) and the Debug tab's "Signal Inspector" sub-tab (with a device
picker).

```js
const inspector = createSignalInspector(containerEl, { ieee, showPicker });
inspector.setDevice('00:12:...');   // switch device (picker mode)
inspector.destroy();                // stop streaming + detach
```

Data comes from `/api/signals/{ieee}` — where `ieee` may be the literal `all` —
plus live `signal_inspector_update` WebSocket events.

### Browser-console logger

`static/js/log.js` gives every JS module a named logger instead of raw
`console.*` calls:

```js
const log = zmmLog('groups');
log.log('rendering', groups);     // gated
log.warn('slow response');        // gated
log.error('save failed', err);    // ALWAYS printed
```

Output is silent by default. Enable namespaces from the Debug tab's "Console"
button, or from DevTools:

```js
zmmLog.enable('groups')     // one namespace
zmmLog.enable('*')          // everything
zmmLog.disable('groups')
zmmLog.namespaces()         // list known namespaces
```

The selection persists in `localStorage` under `zmm.debug`, as either `*` or a
comma-separated namespace list. It is a classic (non-module) script and must be
loaded before every other app script; ES modules use the `window.zmmLog` global.

### Packet Flow panel

`static/js/packet-flow.js` renders the live widget inside `#debugPacketsModal`:
global rate readout (1 s / 10 s / 60 s), peak 1 s rate over the last hour, the
RX/TX split and tracked-device count, a 60-second inline-SVG sparkline, an
hourly statistical summary (mean, stddev, CV, P50/P95/P99), top-5 peak history
with timestamps and dominant-device attribution, top-talkers and per-cluster
tables, and anomaly badges.

Data arrives via `packet_flow` WebSocket messages every 2 s, routed by
`websocket.js`. One REST snapshot is fetched on first init so the panel is not
empty before the first push lands.

## Full-spectrum cluster introspection

`modules/diag_attributes.py` exhaustively discovers a cluster's attributes and
commands, including manufacturer-specific ones. It closes seven gaps in the
naive approach:

1. Sweeps the full `0x0000`–`0xFFFE` attribute ID space in paginated chunks
   rather than just the first 256 IDs, so manufacturer attributes at `0xF000+`
   are found.
2. Re-runs discovery per known manufacturer code for the device, so devices
   that gate attributes on a manufacturer-coded ZCL header (Aqara `0xFCC0`,
   Philips, Sonoff, Tuya, IKEA, Legrand) expose their full set.
3. Handles the Discover Attributes "complete" flag by re-issuing from
   `last_id + 1` until the device signals done.
4. Prefers Discover Attributes Extended (`0x0E`) where available, for real
   access-control flags; falls back to basic discover plus a heuristic
   write-test only when Extended is unsupported.
5. Treats zero-value writes conservatively — never writes 0 to an unknown
   attribute — to minimise side effects on bitmap and enum fields.
6. Reads the Reporting Configuration for each readable attribute, so the cache
   knows whether and how the device auto-reports.
7. Discovers received *and* generated cluster commands, for a complete picture
   of what the cluster supports.

Output is a dict suitable for direct return from an API handler and for
insertion into the `zigbee_cache.device_attributes` table.

Manufacturer codes are 16-bit and registered in `MANUFACTURER_CODES`, keyed by
cluster ID where the code is cluster-specific, plus a generic list for devices
whose code is set at device level.

## Packet Flow Analyzer

`modules/packet_flow.py` is lightweight in-memory packet-rate tracking for the
Zigbee and Matter network. It records every packet entering
`ZigbeeDebugger.capture_packet`, and every TX command sent through
`device.send_command`, **regardless of whether full debug capture is enabled** —
counting is microseconds per packet where decoding is not, so the 1000-deep
packet ring stays untouched while rate and anomaly data still reach the UI.

It exposes global packets-per-second over 1 s / 10 s / 60 s windows, per-second
history for a 60 s sparkline, the peak 1 s rate over the last hour, a top-N peak
history with timestamps and dominant device, a statistical summary (mean, std
dev, P50, P95, P99 over the last hour), a burst counter of seconds exceeding
mean + 2σ, per-device chattiness ranking, a per-cluster aggregate breakdown, and
per-device EWMA-baseline anomaly detection.

Pure stdlib. No DB writes, and no locks — it is single-thread asyncio.

Cost per `record()` is a dict lookup, three deque appends and a few ints: O(1).
Pruning is amortised on read, and a hard GC drops devices that go silent.
Statistical methods are computed lazily on read and cached for about a second,
to keep the snapshot path cheap when the websocket pushes every 2 s.

## Event-loop responsiveness monitor

A blocked asyncio loop is the worst failure mode this app has: HTTP stops dead —
including `/api/system/health` — so the process looks alive while serving
nothing, and the manager watchdog needs several failed checks (minutes) to act.
`modules/loop_monitor.py` closes that gap from inside the process:

- a heartbeat coroutine bumps a timestamp every second on the loop;
- a daemon **thread**, immune to the stall, watches the heartbeat age.

| Stall reaches | Action |
| --- | --- |
| `warn_after` | logs the loop thread's current stack, so the log names exactly what is blocking |
| `exit_after` | writes `data/last_crash.json` and hard-exits non-zero |

The launcher treats a non-zero exit after a healthy boot as a runtime crash and
restarts `main.py` within seconds — far faster than the manager watchdog's grace
plus streak cycle.

The stack dump matters: the Octopus backfill incident of 2026-07-17 took an hour
to diagnose without it. This is why several call sites in this codebase go out of
their way to keep slow DuckDB work off the loop thread.

Stats are surfaced in `/api/system/health` under `loop`, so past stalls stay
visible after recovery.

Env overrides: `ZMM_LOOP_STALL_WARN_SEC` (default 5, 0 disables the warning and
stack dump) and `ZMM_LOOP_STALL_EXIT_SEC` (default 60, 0 disables the
self-restart).

## Application Alert Center

`modules/app_alerts.py` is the central place for surfacing application problems
to the user, instead of leaving them buried in the logs. Two entry points:

1. **`raise_alert(...)`**, called directly by modules that detect a concrete,
   actionable problem — the automation engine disabling a rule whose target
   group no longer exists, say.
2. **`AlertLogHandler`**, a logging handler attached to the root logger at ERROR
   level. Any module that logs an error automatically produces an alert,
   deduplicated so repeated errors bump a counter instead of flooding the UI.

This is why several places in the codebase deliberately log at `warning` rather
than `error` for expected conditions — a duplicate join event, for instance,
would otherwise toast a scary message for routine network chatter.

Alerts persist to `./data/app_alerts.json` on the bind-mounted volume so they
survive restarts, and are pushed live to the frontend over the existing
WebSocket hub as `app_alert` events.
