//! zmm_telemetry — fast DuckDB appender for ZigBee-Matter-Manager telemetry.
//!
//! Public API (Python-facing):
//!     Appender(db_path) -> Appender
//!     Appender.append_device_state(ieee, attribute, value, numeric_val)
//!     Appender.append_packet_stats(ieee, rx_p, tx_p, rx_b, tx_b, errors, retries, lqi)
//!     Appender.append_system_metrics(metrics_dict)
//!     Appender.append_spectrum_scan(channel, energy)
//!     Appender.flush()              -> drains all buffers
//!     Appender.pending() -> dict    -> per-table buffer counts (debug)
//!
//! Concurrency: the row buffers and the DuckDB connection live behind
//! *separate* mutexes. Appends only ever touch the buffers mutex (an in-memory
//! push), so they never wait on the DuckDB write — which can take seconds and
//! is what previously stalled the asyncio event loop. `flush()` swaps the
//! buffers out under a brief lock, then does the disk write under the `conn`
//! mutex with the GIL released, so both other Python threads and further
//! appends keep running while it drains.

use chrono::Utc;
use duckdb::{params, Connection};
use parking_lot::Mutex;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

const AUTO_FLUSH_THRESHOLD: usize = 1000;

// ───────────────────────── row buffers ─────────────────────────

struct DeviceStateRow {
    ts: chrono::DateTime<Utc>,
    ieee: String,
    attribute: String,
    value: Option<String>,
    numeric_val: Option<f64>,
}

struct PacketStatRow {
    ts: chrono::DateTime<Utc>,
    ieee: String,
    rx_packets: i64,
    tx_packets: i64,
    rx_bytes: i64,
    tx_bytes: i64,
    errors: i32,
    retries: i32,
    lqi: i32,
}

struct SystemMetricRow {
    ts: chrono::DateTime<Utc>,
    cpu_percent: Option<f32>,
    cpu_freq: Option<f32>,
    mem_total: Option<i64>,
    mem_used: Option<i64>,
    mem_percent: Option<f32>,
    swap_used: Option<i64>,
    swap_percent: Option<f32>,
    disk_total: Option<i64>,
    disk_used: Option<i64>,
    disk_percent: Option<f32>,
    cpu_temp: Option<f32>,
    gpu_temp: Option<f32>,
    load_1m: Option<f32>,
    load_5m: Option<f32>,
    load_15m: Option<f32>,
    uptime_secs: Option<i64>,
    process_rss: Option<i64>,
    process_threads: Option<i32>,
}

struct SpectrumRow {
    ts: chrono::DateTime<Utc>,
    channel: i32,
    energy: i32,
}

struct HeatingRoomRow {
    ts: chrono::DateTime<Utc>,
    circuit_id: String,
    room_id: String,
    classification: Option<String>,
    current_temp_c: Option<f64>,
    setpoint_c: Option<f64>,
    outdoor_temp_c: Option<f64>,
    calling_for_heat: bool,
    trv_setpoint_c: Option<f64>,
    trv_valve_open_pct: Option<f64>,
    dry_run: bool,
    reason: Option<String>,
}

struct HeatingBoilerRow {
    ts: chrono::DateTime<Utc>,
    circuit_id: String,
    boiler_called: bool,
    rooms_cold: i32,
    rooms_ontarget: i32,
    rooms_hot: i32,
    receiver_command: Option<String>,
    dry_run: bool,
}

// ───────────────────────── buffered rows ─────────────────────────

/// In-memory row buffers, guarded by their own mutex. Held only for the
/// microseconds an append (push) or a flush's buffer-swap needs — never for
/// the duration of a DuckDB write.
#[derive(Default)]
struct Buffers {
    device_states: Vec<DeviceStateRow>,
    packet_stats: Vec<PacketStatRow>,
    system_metrics: Vec<SystemMetricRow>,
    spectrum: Vec<SpectrumRow>,
    heating_rooms: Vec<HeatingRoomRow>,
    heating_boiler: Vec<HeatingBoilerRow>,
}

impl Buffers {
    fn is_empty(&self) -> bool {
        self.device_states.is_empty()
            && self.packet_stats.is_empty()
            && self.system_metrics.is_empty()
            && self.spectrum.is_empty()
            && self.heating_rooms.is_empty()
            && self.heating_boiler.is_empty()
    }
}

// ─────────────── DuckDB writers (run with the GIL released) ───────────────
//
// Each consumes the rows it was handed (via drain) and writes them through a
// DuckDB appender. They take `&Connection` and the owned buffer set, so they
// never touch the shared buffers mutex — appends run concurrently.

fn write_device_states(conn: &Connection, rows: &mut Vec<DeviceStateRow>) -> duckdb::Result<()> {
    if rows.is_empty() {
        return Ok(());
    }
    let mut app = conn.appender("device_states")?;
    for r in rows.drain(..) {
        app.append_row(params![r.ts, r.ieee, r.attribute, r.value, r.numeric_val])?;
    }
    app.flush()?;
    Ok(())
}

fn write_packet_stats(conn: &Connection, rows: &mut Vec<PacketStatRow>) -> duckdb::Result<()> {
    if rows.is_empty() {
        return Ok(());
    }
    let mut app = conn.appender("packet_stats")?;
    for r in rows.drain(..) {
        app.append_row(params![
            r.ts, r.ieee,
            r.rx_packets, r.tx_packets, r.rx_bytes, r.tx_bytes,
            r.errors, r.retries, r.lqi,
        ])?;
    }
    app.flush()?;
    Ok(())
}

fn write_system_metrics(conn: &Connection, rows: &mut Vec<SystemMetricRow>) -> duckdb::Result<()> {
    if rows.is_empty() {
        return Ok(());
    }
    let mut app = conn.appender("system_metrics")?;
    for r in rows.drain(..) {
        app.append_row(params![
            r.ts,
            r.cpu_percent, r.cpu_freq,
            r.mem_total, r.mem_used, r.mem_percent,
            r.swap_used, r.swap_percent,
            r.disk_total, r.disk_used, r.disk_percent,
            r.cpu_temp, r.gpu_temp,
            r.load_1m, r.load_5m, r.load_15m,
            r.uptime_secs, r.process_rss, r.process_threads,
        ])?;
    }
    app.flush()?;
    Ok(())
}

fn write_spectrum(conn: &Connection, rows: &mut Vec<SpectrumRow>) -> duckdb::Result<()> {
    if rows.is_empty() {
        return Ok(());
    }
    let mut app = conn.appender("spectrum_scans")?;
    for r in rows.drain(..) {
        app.append_row(params![r.ts, r.channel, r.energy])?;
    }
    app.flush()?;
    Ok(())
}

fn write_heating_rooms(conn: &Connection, rows: &mut Vec<HeatingRoomRow>) -> duckdb::Result<()> {
    if rows.is_empty() {
        return Ok(());
    }
    let mut app = conn.appender("heating_tick_rooms")?;
    for r in rows.drain(..) {
        app.append_row(params![
            r.ts, r.circuit_id, r.room_id, r.classification,
            r.current_temp_c, r.setpoint_c, r.outdoor_temp_c,
            r.calling_for_heat, r.trv_setpoint_c, r.trv_valve_open_pct,
            r.dry_run, r.reason,
        ])?;
    }
    app.flush()?;
    Ok(())
}

fn write_heating_boiler(conn: &Connection, rows: &mut Vec<HeatingBoilerRow>) -> duckdb::Result<()> {
    if rows.is_empty() {
        return Ok(());
    }
    let mut app = conn.appender("heating_tick_boiler")?;
    for r in rows.drain(..) {
        app.append_row(params![
            r.ts, r.circuit_id, r.boiler_called,
            r.rooms_cold, r.rooms_ontarget, r.rooms_hot,
            r.receiver_command, r.dry_run,
        ])?;
    }
    app.flush()?;
    Ok(())
}

fn write_all(conn: &Connection, b: &mut Buffers) -> duckdb::Result<()> {
    write_device_states(conn, &mut b.device_states)?;
    write_packet_stats(conn, &mut b.packet_stats)?;
    write_system_metrics(conn, &mut b.system_metrics)?;
    write_spectrum(conn, &mut b.spectrum)?;
    write_heating_rooms(conn, &mut b.heating_rooms)?;
    write_heating_boiler(conn, &mut b.heating_boiler)?;
    Ok(())
}

// ───────────────────────── PyO3 wrapper ─────────────────────────

#[pyclass]
struct Appender {
    buffers: Mutex<Buffers>,
    conn: Mutex<Connection>,
}

fn db_err(e: duckdb::Error) -> PyErr {
    PyRuntimeError::new_err(format!("duckdb: {e}"))
}

// Non-`#[pymethods]` block: helpers here stay private (not exposed to Python).
impl Appender {
    /// Swap the current buffers out under a brief lock, then write them to
    /// DuckDB with the GIL released. Callable from any thread; while the write
    /// runs, both other Python threads (the asyncio loop) and concurrent
    /// appends make progress.
    fn drain_and_write(&self, py: Python<'_>) -> PyResult<()> {
        let mut taken = {
            let mut b = self.buffers.lock();
            if b.is_empty() {
                return Ok(());
            }
            std::mem::take(&mut *b)
        };
        py.allow_threads(|| {
            let conn = self.conn.lock();
            write_all(&conn, &mut taken)
        })
        .map_err(db_err)
    }
}

#[pymethods]
impl Appender {
    #[new]
    fn new(db_path: &str) -> PyResult<Self> {
        let conn = Connection::open(db_path).map_err(db_err)?;
        Ok(Self {
            buffers: Mutex::new(Buffers::default()),
            conn: Mutex::new(conn),
        })
    }

    #[pyo3(signature = (ieee, attribute, value=None, numeric_val=None))]
    fn append_device_state(
        &self,
        py: Python<'_>,
        ieee: String,
        attribute: String,
        value: Option<String>,
        numeric_val: Option<f64>,
    ) -> PyResult<()> {
        let over = {
            let mut b = self.buffers.lock();
            b.device_states.push(DeviceStateRow {
                ts: Utc::now(),
                ieee,
                attribute,
                value,
                numeric_val,
            });
            b.device_states.len() >= AUTO_FLUSH_THRESHOLD
        };
        if over {
            self.drain_and_write(py)?;
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn append_packet_stats(
        &self,
        py: Python<'_>,
        ieee: String,
        rx_packets: i64,
        tx_packets: i64,
        rx_bytes: i64,
        tx_bytes: i64,
        errors: i32,
        retries: i32,
        lqi: i32,
    ) -> PyResult<()> {
        let over = {
            let mut b = self.buffers.lock();
            b.packet_stats.push(PacketStatRow {
                ts: Utc::now(),
                ieee, rx_packets, tx_packets, rx_bytes, tx_bytes, errors, retries, lqi,
            });
            b.packet_stats.len() >= AUTO_FLUSH_THRESHOLD
        };
        if over {
            self.drain_and_write(py)?;
        }
        Ok(())
    }

    fn append_system_metrics(&self, py: Python<'_>, metrics: &Bound<'_, PyDict>) -> PyResult<()> {
        // Helpers to extract optional typed values from the dict
        fn opt_f32(d: &Bound<'_, PyDict>, k: &str) -> PyResult<Option<f32>> {
            match d.get_item(k)? { Some(v) if !v.is_none() => Ok(Some(v.extract::<f32>()?)), _ => Ok(None) }
        }
        fn opt_i64(d: &Bound<'_, PyDict>, k: &str) -> PyResult<Option<i64>> {
            match d.get_item(k)? { Some(v) if !v.is_none() => Ok(Some(v.extract::<i64>()?)), _ => Ok(None) }
        }
        fn opt_i32(d: &Bound<'_, PyDict>, k: &str) -> PyResult<Option<i32>> {
            match d.get_item(k)? { Some(v) if !v.is_none() => Ok(Some(v.extract::<i32>()?)), _ => Ok(None) }
        }

        let row = SystemMetricRow {
            ts: Utc::now(),
            cpu_percent:  opt_f32(metrics, "cpu_percent")?,
            cpu_freq:     opt_f32(metrics, "cpu_freq")?,
            mem_total:    opt_i64(metrics, "mem_total")?,
            mem_used:     opt_i64(metrics, "mem_used")?,
            mem_percent:  opt_f32(metrics, "mem_percent")?,
            swap_used:    opt_i64(metrics, "swap_used")?,
            swap_percent: opt_f32(metrics, "swap_percent")?,
            disk_total:   opt_i64(metrics, "disk_total")?,
            disk_used:    opt_i64(metrics, "disk_used")?,
            disk_percent: opt_f32(metrics, "disk_percent")?,
            cpu_temp:     opt_f32(metrics, "cpu_temp")?,
            gpu_temp:     opt_f32(metrics, "gpu_temp")?,
            load_1m:      opt_f32(metrics, "load_1m")?,
            load_5m:      opt_f32(metrics, "load_5m")?,
            load_15m:     opt_f32(metrics, "load_15m")?,
            uptime_secs:  opt_i64(metrics, "uptime_secs")?,
            process_rss:  opt_i64(metrics, "process_rss")?,
            process_threads: opt_i32(metrics, "process_threads")?,
        };

        let over = {
            let mut b = self.buffers.lock();
            b.system_metrics.push(row);
            b.system_metrics.len() >= 64
        };
        if over {
            self.drain_and_write(py)?;
        }
        Ok(())
    }

    fn append_spectrum_scan(&self, py: Python<'_>, channel: i32, energy: i32) -> PyResult<()> {
        let over = {
            let mut b = self.buffers.lock();
            b.spectrum.push(SpectrumRow { ts: Utc::now(), channel, energy });
            b.spectrum.len() >= 256
        };
        if over {
            self.drain_and_write(py)?;
        }
        Ok(())
    }


    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (ts_epoch, circuit_id, room_id, classification, current_temp_c, setpoint_c, outdoor_temp_c, calling_for_heat, trv_setpoint_c, trv_valve_open_pct, dry_run, reason))]
    fn append_heating_room(
        &self,
        py: Python<'_>,
        ts_epoch: f64,
        circuit_id: String,
        room_id: String,
        classification: Option<String>,
        current_temp_c: Option<f64>,
        setpoint_c: Option<f64>,
        outdoor_temp_c: Option<f64>,
        calling_for_heat: bool,
        trv_setpoint_c: Option<f64>,
        trv_valve_open_pct: Option<f64>,
        dry_run: bool,
        reason: Option<String>,
    ) -> PyResult<()> {
        let ts = chrono::DateTime::<Utc>::from_timestamp(
            ts_epoch as i64, ((ts_epoch.fract()) * 1e9) as u32,
        ).ok_or_else(|| PyRuntimeError::new_err("invalid ts_epoch"))?;

        let over = {
            let mut b = self.buffers.lock();
            b.heating_rooms.push(HeatingRoomRow {
                ts, circuit_id, room_id, classification,
                current_temp_c, setpoint_c, outdoor_temp_c,
                calling_for_heat, trv_setpoint_c, trv_valve_open_pct,
                dry_run, reason,
            });
            b.heating_rooms.len() >= 256
        };
        if over {
            self.drain_and_write(py)?;
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (ts_epoch, circuit_id, boiler_called, rooms_cold, rooms_ontarget, rooms_hot, receiver_command, dry_run))]
    fn append_heating_boiler(
        &self,
        py: Python<'_>,
        ts_epoch: f64,
        circuit_id: String,
        boiler_called: bool,
        rooms_cold: i32,
        rooms_ontarget: i32,
        rooms_hot: i32,
        receiver_command: Option<String>,
        dry_run: bool,
    ) -> PyResult<()> {
        let ts = chrono::DateTime::<Utc>::from_timestamp(
            ts_epoch as i64, ((ts_epoch.fract()) * 1e9) as u32,
        ).ok_or_else(|| PyRuntimeError::new_err("invalid ts_epoch"))?;

        let over = {
            let mut b = self.buffers.lock();
            b.heating_boiler.push(HeatingBoilerRow {
                ts, circuit_id, boiler_called,
                rooms_cold, rooms_ontarget, rooms_hot,
                receiver_command, dry_run,
            });
            b.heating_boiler.len() >= 64
        };
        if over {
            self.drain_and_write(py)?;
        }
        Ok(())
    }

    fn flush(&self, py: Python<'_>) -> PyResult<()> {
        self.drain_and_write(py)
    }

    fn pending<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let b = self.buffers.lock();
        let d = PyDict::new(py);
        d.set_item("device_states", b.device_states.len())?;
        d.set_item("packet_stats", b.packet_stats.len())?;
        d.set_item("system_metrics", b.system_metrics.len())?;
        d.set_item("spectrum_scans", b.spectrum.len())?;
        d.set_item("heating_rooms", b.heating_rooms.len())?;
        d.set_item("heating_boiler", b.heating_boiler.len())?;
        Ok(d)
    }
}

#[pymodule]
fn zmm_telemetry(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Appender>()?;
    Ok(())
}
