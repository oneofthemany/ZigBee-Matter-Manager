"""
System tab drill-downs — the detail behind each gauge card (CPU, memory,
temperature, disk, process, load) read straight from /proc and /sys on demand.

Nothing here is stored; each call takes a fresh snapshot. CPU figures come
from a short two-point sample, so a call takes roughly SAMPLE_SECS.
"""

import asyncio
import os
import sys
import threading
import time
from collections import Counter
from typing import Any, Dict, List, Optional

SAMPLE_SECS = 0.5
TOP_N = 15

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HZ = os.sysconf("SC_CLK_TCK")
_PROCESS_START = time.time()

# Filesystems worth showing on the disk drill-down
_REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "vfat", "exfat",
            "ntfs", "ntfs3", "zfs", "nfs", "nfs4", "cifs", "fuseblk", "overlay"}


# /PROC READERS

def _read_file(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _read_int(path: str) -> Optional[int]:
    raw = _read_file(path)
    try:
        return int(raw.strip()) if raw is not None else None
    except ValueError:
        return None


def _read_kv(path: str) -> Dict[str, int]:
    """Parse "Key:   123 kB" style files (meminfo, status) into bytes/ints."""
    out: Dict[str, int] = {}
    for line in (_read_file(path) or "").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            val = int(parts[0])
        except ValueError:
            continue
        out[key.strip()] = val * 1024 if len(parts) > 1 and parts[1] == "kB" else val
    return out


def _read_cpu_times() -> Dict[str, List[int]]:
    """Per-CPU jiffies from /proc/stat: {"cpu": [...], "cpu0": [...], ...}."""
    out = {}
    for line in (_read_file("/proc/stat") or "").splitlines():
        if line.startswith("cpu"):
            parts = line.split()
            out[parts[0]] = [int(x) for x in parts[1:]]
    return out


def _stat_ticks(stat_text: str) -> Optional[tuple]:
    """(comm, utime+stime) from a /proc/<pid>/stat line; comm may contain ')'."""
    try:
        head, tail = stat_text.rsplit(")", 1)
        fields = tail.split()
        return head.split("(", 1)[1], int(fields[11]) + int(fields[12])
    except (ValueError, IndexError):
        return None


def _read_proc_ticks() -> Dict[int, tuple]:
    """{pid: (name, cpu_ticks)} for every process on the host."""
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        parsed = _stat_ticks(_read_file(f"/proc/{pid}/stat") or "")
        if parsed:
            out[int(pid)] = parsed
    return out


def _read_task_ticks() -> Dict[int, tuple]:
    """{tid: (os_thread_name, cpu_ticks)} for every thread of this process."""
    out = {}
    try:
        tids = os.listdir("/proc/self/task")
    except OSError:
        return out
    for tid in tids:
        parsed = _stat_ticks(_read_file(f"/proc/self/task/{tid}/stat") or "")
        if parsed:
            out[int(tid)] = parsed
    return out


def _snapshot() -> Dict[str, Any]:
    return {"t": time.monotonic(), "cpu": _read_cpu_times(),
            "procs": _read_proc_ticks(), "tasks": _read_task_ticks()}


async def _sample() -> tuple:
    before = await asyncio.to_thread(_snapshot)
    await asyncio.sleep(SAMPLE_SECS)
    after = await asyncio.to_thread(_snapshot)
    return before, after


def _tick_pct(before: Dict, after: Dict, key: str, elapsed: float) -> Dict[int, float]:
    """CPU % per id between two tick dicts (100% = one full core)."""
    out = {}
    for ident, (_, ticks) in after[key].items():
        prev = before[key].get(ident)
        if prev is not None:
            out[ident] = round((ticks - prev[1]) / _HZ / elapsed * 100, 1)
    return out


def _core_usage(before: Dict, after: Dict) -> List[Dict[str, Any]]:
    rows = []
    for name, now in after["cpu"].items():
        prev = before["cpu"].get(name)
        if not prev:
            continue
        delta = [a - b for a, b in zip(now, prev)]
        total = sum(delta[:8]) or 1          # user..steal; guest is already in user
        idle = delta[3] + delta[4]           # idle + iowait
        rows.append({
            "cpu": "all" if name == "cpu" else name[3:],
            "usage": round((total - idle) / total * 100, 1),
            "user": round((delta[0] + delta[1]) / total * 100, 1),
            "system": round((delta[2] + delta[5] + delta[6]) / total * 100, 1),
            "iowait": round(delta[4] / total * 100, 1),
            "steal": round(delta[7] / total * 100, 1),
        })
    return rows


def _top_processes(before: Dict, after: Dict, limit: int = TOP_N) -> List[Dict[str, Any]]:
    elapsed = max(after["t"] - before["t"], 1e-3)
    pct = _tick_pct(before, after, "procs", elapsed)
    rows = []
    for pid, cpu in pct.items():
        status = _read_kv(f"/proc/{pid}/status")
        rows.append({
            "pid": pid,
            "name": after["procs"][pid][0],
            "cpu_percent": cpu,
            "rss": status.get("VmRSS", 0),
            "swap": status.get("VmSwap", 0),
            "self": pid == os.getpid(),
        })
    rows.sort(key=lambda r: (-r["cpu_percent"], -r["rss"]))
    return rows[:limit]


def _pressure() -> Dict[str, Any]:
    """Pressure stall information (kernel 4.20+): % of time tasks were stalled."""
    out = {}
    for res in ("cpu", "memory", "io"):
        raw = _read_file(f"/proc/pressure/{res}")
        if not raw:
            continue
        entry = {}
        for line in raw.splitlines():
            kind, *pairs = line.split()
            vals = dict(p.split("=") for p in pairs)
            entry[kind] = {k: float(vals[k]) for k in ("avg10", "avg60", "avg300") if k in vals}
        out[res] = entry
    return out


# THREADS

def _short_path(filename: str) -> str:
    if filename.startswith(_PROJECT_ROOT + os.sep):
        return os.path.relpath(filename, _PROJECT_ROOT)
    for marker in ("site-packages" + os.sep, "python%d.%d" % sys.version_info[:2] + os.sep):
        if marker in filename:
            return filename.split(marker, 1)[1]
    return os.path.basename(filename)


def _frame_summary(frame) -> Optional[str]:
    """Innermost frame in project code, falling back to the innermost frame."""
    innermost = None
    while frame is not None:
        code = frame.f_code
        loc = f"{code.co_name} ({_short_path(code.co_filename)}:{frame.f_lineno})"
        innermost = innermost or loc
        if code.co_filename.startswith(_PROJECT_ROOT + os.sep) and "site-packages" not in code.co_filename:
            return loc
        frame = frame.f_back
    return innermost


def _target_name(t: threading.Thread) -> Optional[str]:
    target = getattr(t, "_target", None)
    if target is None:
        return None
    mod = getattr(target, "__module__", None) or ""
    name = getattr(target, "__qualname__", None) or type(target).__name__
    return f"{mod}.{name}" if mod else name


def _threads(before: Dict, after: Dict) -> List[Dict[str, Any]]:
    elapsed = max(after["t"] - before["t"], 1e-3)
    pct = _tick_pct(before, after, "tasks", elapsed)
    py_threads = {t.native_id: t for t in threading.enumerate() if t.native_id}
    frames = sys._current_frames()
    main_tid = threading.main_thread().native_id

    rows = []
    for tid, (os_name, ticks) in after["tasks"].items():
        row = {"tid": tid, "os_name": os_name, "cpu_percent": pct.get(tid),
               "cpu_total_secs": round(ticks / _HZ, 1), "main": tid == main_tid,
               "python": False, "name": None, "daemon": None,
               "target": None, "current": None}
        t = py_threads.get(tid)
        if t is not None:
            row.update(python=True, name=t.name, daemon=t.daemon,
                       target=_target_name(t),
                       current=_frame_summary(frames.get(t.ident)))
        rows.append(row)
    rows.sort(key=lambda r: (-(r["cpu_percent"] or 0), -r["cpu_total_secs"]))
    return rows


def _asyncio_tasks() -> List[Dict[str, Any]]:
    """Event-loop tasks grouped by coroutine name (must run on the loop)."""
    counts: Counter = Counter()
    for task in asyncio.all_tasks():
        coro = task.get_coro()
        name = getattr(coro, "__qualname__", None) or type(coro).__name__
        counts[name] += 1
    return [{"coroutine": k, "count": v} for k, v in counts.most_common()]


# AREA COLLECTORS

async def _cpu() -> Dict[str, Any]:
    before, after = await _sample()
    model = None
    for line in (_read_file("/proc/cpuinfo") or "").splitlines():
        key, _, val = line.partition(":")
        if key.strip() in ("model name", "Model", "Hardware"):
            model = val.strip()
            break

    freqs = []
    for i in range(os.cpu_count() or 0):
        base = f"/sys/devices/system/cpu/cpu{i}/cpufreq"
        cur = _read_int(f"{base}/scaling_cur_freq")
        if cur is None:
            continue
        freqs.append({"cpu": str(i), "cur_mhz": cur // 1000,
                      "max_mhz": (_read_int(f"{base}/scaling_max_freq") or 0) // 1000,
                      "governor": (_read_file(f"{base}/scaling_governor") or "").strip() or None})

    return {"model": model, "cores": os.cpu_count(),
            "usage": _core_usage(before, after), "freq": freqs,
            "processes": _top_processes(before, after),
            "threads": [t for t in _threads(before, after) if t["cpu_percent"]][:TOP_N]}


async def _memory() -> Dict[str, Any]:
    info = _read_kv("/proc/meminfo")
    keys = ("MemTotal", "MemAvailable", "MemFree", "Buffers", "Cached", "Shmem",
            "SReclaimable", "Dirty", "SwapTotal", "SwapFree", "SwapCached")
    status = _read_kv("/proc/self/status")
    procs = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        st = _read_kv(f"/proc/{pid}/status")
        if st.get("VmRSS") or st.get("VmSwap"):
            name = (_read_file(f"/proc/{pid}/comm") or "?").strip()
            procs.append({"pid": int(pid), "name": name, "rss": st.get("VmRSS", 0),
                          "swap": st.get("VmSwap", 0), "self": int(pid) == os.getpid()})
    procs.sort(key=lambda p: -(p["rss"] + p["swap"]))
    return {
        "system": {k: info.get(k, 0) for k in keys},
        "process": {k: status.get(k, 0) for k in
                    ("VmRSS", "VmHWM", "RssAnon", "RssFile", "RssShmem", "VmSwap", "VmSize")},
        "processes": procs[:TOP_N],
        "pressure": _pressure().get("memory"),
    }


async def _temperature() -> Dict[str, Any]:
    zones = []
    base = "/sys/class/thermal"
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        if not name.startswith("thermal_zone"):
            continue
        temp = _read_int(f"{base}/{name}/temp")
        if temp is None:
            continue
        zones.append({"zone": name, "type": (_read_file(f"{base}/{name}/type") or "").strip(),
                      "temp": temp / 1000})

    sensors, fans = [], []
    base = "/sys/class/hwmon"
    for hw in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        path = f"{base}/{hw}"
        chip = (_read_file(f"{path}/name") or hw).strip()
        for fname in sorted(os.listdir(path)):
            if fname.startswith("temp") and fname.endswith("_input"):
                prefix = fname[:-6]
                val = _read_int(f"{path}/{fname}")
                if val is None:
                    continue
                crit = _read_int(f"{path}/{prefix}_crit")
                high = _read_int(f"{path}/{prefix}_max")
                sensors.append({"chip": chip,
                                "label": (_read_file(f"{path}/{prefix}_label") or prefix).strip(),
                                "temp": val / 1000,
                                "high": high / 1000 if high else None,
                                "crit": crit / 1000 if crit else None})
            elif fname.startswith("fan") and fname.endswith("_input"):
                rpm = _read_int(f"{path}/{fname}")
                if rpm is not None:
                    fans.append({"chip": chip, "label": (_read_file(
                        f"{path}/{fname[:-6]}_label") or fname[:-6]).strip(), "rpm": rpm})

    throttle = None
    cur = _read_int("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    mx = _read_int("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq")
    if cur and mx:
        throttle = {"cur_mhz": cur // 1000, "max_mhz": mx // 1000,
                    "throttled": cur < mx * 0.8}
    from modules.system_monitor import temp_sensor_sources
    return {"zones": zones, "sensors": sensors, "fans": fans, "cpu_freq": throttle,
            "card_sources": temp_sensor_sources()}


def _dir_size(path: str, budget: List[int]) -> Optional[int]:
    """Recursive size; stops counting once the shared file budget is spent."""
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            if budget[0] <= 0:
                return None
            budget[0] -= 1
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def _disk_sync() -> Dict[str, Any]:
    mounts, seen = [], set()
    for line in (_read_file("/proc/mounts") or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[2] not in _REAL_FS:
            continue
        device, mountpoint, fstype = parts[0], parts[1].replace("\\040", " "), parts[2]
        if fstype == "overlay" and "ro" in parts[3].split(","):
            continue   # image-based roots (composefs) always read 100% full
        try:
            st = os.statvfs(mountpoint)
        except OSError:
            continue
        key = (device, st.f_blocks)
        if key in seen or st.f_blocks == 0:
            continue
        seen.add(key)
        total = st.f_blocks * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        mounts.append({"device": device, "mount": mountpoint, "fstype": fstype,
                       "total": total, "used": used,
                       "percent": round(used / total * 100, 1)})

    # App directories: the top-level entries of data/, logs/ and backups/
    budget = [200_000]
    app = []
    for top in ("data", "logs", "backups"):
        root = os.path.join(_PROJECT_ROOT, top)
        if not os.path.isdir(root):
            continue
        for entry in os.scandir(root):
            try:
                size = (_dir_size(entry.path, budget) if entry.is_dir(follow_symlinks=False)
                        else entry.stat(follow_symlinks=False).st_size)
            except OSError:
                continue
            app.append({"path": f"{top}/{entry.name}", "dir": entry.is_dir(follow_symlinks=False),
                        "size": size})
    app.sort(key=lambda e: -(e["size"] or 0))

    io = _read_kv("/proc/self/io")
    return {"mounts": mounts, "app": app[:25], "app_truncated": budget[0] <= 0,
            "process_io": {"read_bytes": io.get("read_bytes"), "write_bytes": io.get("write_bytes")},
            "pressure": _pressure().get("io")}


async def _disk() -> Dict[str, Any]:
    return await asyncio.to_thread(_disk_sync)


async def _process() -> Dict[str, Any]:
    before, after = await _sample()
    status = _read_kv("/proc/self/status")
    try:
        fds = len(os.listdir("/proc/self/fd"))
    except OSError:
        fds = None
    threads = _threads(before, after)
    return {
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "uptime_secs": int(time.time() - _PROCESS_START),
        "cpu_percent": _tick_pct(before, after, "procs", max(after["t"] - before["t"], 1e-3)).get(os.getpid()),
        "rss": status.get("VmRSS"),
        "open_fds": fds,
        "threads": threads,
        "thread_groups": [{"name": k, "count": v} for k, v in Counter(
            (t["name"] or t["os_name"]).rstrip("0123456789_-") or t["os_name"]
            for t in threads).most_common()],
        "asyncio_tasks": _asyncio_tasks(),
    }


async def _load() -> Dict[str, Any]:
    before, after = await _sample()
    parts = (_read_file("/proc/loadavg") or "").split()
    running, total = (parts[3].split("/") if len(parts) > 3 else (None, None))
    uptime = float((_read_file("/proc/uptime") or "0").split()[0])
    return {
        "load": [float(x) for x in parts[:3]] if parts else [],
        "cores": os.cpu_count(),
        "running": int(running) if running else None,
        "total_tasks": int(total) if total else None,
        "uptime_secs": int(uptime),
        "boot_time": int(time.time() - uptime),
        "app_uptime_secs": int(time.time() - _PROCESS_START),
        "pressure": _pressure(),
        "processes": _top_processes(before, after, 10),
    }


AREAS = {"cpu": _cpu, "memory": _memory, "temperature": _temperature,
         "disk": _disk, "process": _process, "load": _load}


async def collect_detail(area: str) -> Dict[str, Any]:
    return await AREAS[area]()
