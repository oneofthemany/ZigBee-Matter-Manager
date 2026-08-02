#!/usr/bin/env python3
"""
Supervises main.py (stdlib only).

A clean exit stops the supervisor. A crash before HEALTHY_SECONDS, or repeated
crashes inside RUNTIME_CRASH_WINDOW, captures the traceback and enters recovery
standby: a tiny page on :8000 pointing at the ZMM Manager (:8001), which owns
the real recovery UI. Standby waits for data/.recovery_resume, then retries.
"""

import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)

DATA_DIR = os.path.join(APP_DIR, "data")
LOG_DIR = os.path.join(APP_DIR, "logs")
CRASH_FILE = os.path.join(DATA_DIR, "last_crash.json")
LAUNCHER_LOG = os.path.join(LOG_DIR, "launcher.log")
BOOT_GUARD = os.path.join(APP_DIR, "boot_guard.py")
MAIN_PY = os.path.join(APP_DIR, "main.py")
# Recovery standby contract with the ZMM Manager (data/ is host-shared):
RECOVERY_MARKER = os.path.join(DATA_DIR, ".recovery_active")
RESUME_MARKER = os.path.join(DATA_DIR, ".recovery_resume")
MANAGER_PORT = int(os.environ.get("ZMM_MANAGER_PORT", "8001"))

# If main.py dies in under this many seconds it's a "boot crash"
HEALTHY_SECONDS = 25
# A crash after HEALTHY_SECONDS is treated as a one-off and restarted — right
# once, wrong repeatedly. The manager watchdog cannot cover this: it needs
# consecutive health failures, and an app on a crash cycle is healthy for most
# of each cycle. Restart frequency is the only signal, and only the launcher
# sees it.
RUNTIME_CRASH_WINDOW = 900     # seconds to remember a runtime crash for
MAX_RUNTIME_CRASHES = 3        # this many inside the window == a crash loop

# Auto-rollback. The launcher cannot swap its own container image, so it writes
# the same trigger the Settings UI uses and the host-side systemd path unit
# (zmm-upgrade.path -> upgrade.sh) performs the swap.
UPGRADE_TRIGGER_DIR = os.path.join(DATA_DIR, "upgrade")
TRIGGER_FILE = os.path.join(UPGRADE_TRIGGER_DIR, "trigger")
VERSION_STATE_FILE = os.path.join(DATA_DIR, "state", "version.json")
# Records the version we already asked to roll back FROM, so one bad upgrade
# gets exactly one automatic rollback and never ping-pongs.
ROLLBACK_MARKER = os.path.join(DATA_DIR, ".auto_rollback_attempted")
# Only roll back an upgrade this recent. A version that has run happily for
# days and then starts looping has a new problem — data, disk, hardware — and
# reverting the code would hide it rather than fix it.
ROLLBACK_MAX_UPGRADE_AGE = 6 * 3600    # seconds

# Cap on quick-retry loops (avoids tight crashloops when recovery is unavailable)
MAX_QUICK_RETRIES = 3
# Seconds to sleep between runtime-level restarts
RESTART_BACKOFF = 2


# LOGGING

def _log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [launcher] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LAUNCHER_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# CHILD PROCESS HANDLING

_current_child: "subprocess.Popen | None" = None


CONFIG_FILE = os.path.join(APP_DIR, "config", "config.yaml")
# How long to wait for the Zigbee serial device before starting anyway (so a
# missing dongle still reaches the setup wizard rather than blocking forever).
DEVICE_WAIT_SECONDS = 45


def _configured_serial_device() -> "str | None":
    """Read zigbee.port from config.yaml. Return a /dev path to wait for, or
    None for socket:// (MultiPAN) / unset / unreadable — nothing to wait on.
    Mirrors run_container.sh's port resolution (first /dev or socket entry)."""
    try:
        with open(CONFIG_FILE) as f:
            for line in f:
                m = re.search(r'port:\s*([^\s#]+)', line)
                if not m:
                    continue
                val = m.group(1).strip().strip('"\'')
                if val.startswith("/dev/"):
                    return val
                if val.startswith("socket://"):
                    return None      # bridge mode — no local device to await
    except Exception:
        pass
    return None


def _wait_for_device():
    """Block (bounded) until the configured Zigbee dongle is present, so main.py
    doesn't start before the kernel has enumerated it after a reboot — the cause
    of the start/stop/start flap. No-op when no /dev device is configured."""
    dev = _configured_serial_device()
    if not dev:
        return
    if os.path.exists(dev):
        return
    _log(f"Waiting up to {DEVICE_WAIT_SECONDS}s for Zigbee device {dev} ...")
    deadline = time.time() + DEVICE_WAIT_SECONDS
    while time.time() < deadline:
        if os.path.exists(dev):
            _log(f"Zigbee device {dev} present — starting app")
            return
        time.sleep(1)
    _log(f"Zigbee device {dev} still absent after {DEVICE_WAIT_SECONDS}s — "
         f"starting anyway (setup wizard / recovery will handle it)")


import threading

# Set on SIGTERM/SIGINT so the in-process recovery standby also shuts down.
_stop_event = threading.Event()


def _forward_signal(sig, frame):
    """Forward TERM/INT to the current child so shutdown is clean."""
    _stop_event.set()
    child = _current_child
    if child and child.poll() is None:
        try:
            child.send_signal(sig)
        except Exception:
            pass


signal.signal(signal.SIGTERM, _forward_signal)
signal.signal(signal.SIGINT, _forward_signal)


def _run_child(script: str, capture_stderr: bool) -> "tuple[int, str, float]":
    """Run a Python script. Return (returncode, stderr_text, elapsed)."""
    global _current_child
    start = time.time()

    stderr_target = subprocess.PIPE if capture_stderr else None
    proc = subprocess.Popen(
        [sys.executable, "-u", script],
        stdout=None,           # let stdout flow through
        stderr=stderr_target,
        env=os.environ.copy(),
        cwd=APP_DIR,
    )
    _current_child = proc

    stderr_chunks = []
    if capture_stderr and proc.stderr is not None:
        # Stream stderr line-by-line so the user still sees it live
        try:
            for raw in iter(proc.stderr.readline, b""):
                try:
                    sys.stderr.buffer.write(raw)
                    sys.stderr.flush()
                except Exception:
                    pass
                stderr_chunks.append(raw)
        except Exception as e:
            _log(f"stderr stream error: {e}")

    proc.wait()
    _current_child = None
    elapsed = time.time() - start
    stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    return proc.returncode, stderr_text, elapsed


# CRASH CAPTURE

def _parse_crash_from_stderr(stderr_text: str, exit_code: int) -> dict:
    """
    Extract the most useful crash info from a stderr dump.
    Finds the deepest in-app frame so the user sees THEIR broken file,
    not an internal framework frame.
    """
    frames = re.findall(r'File "([^"]+)", line (\d+)', stderr_text)
    suspect_file = None
    suspect_line = None
    for fn, ln in reversed(frames):
        # Prefer frames inside /app and exclude site-packages
        if ("/app/" in fn or fn.startswith("./") or not fn.startswith("/")) \
                and "site-packages" not in fn:
            suspect_file = fn
            suspect_line = int(ln)
            break
    if suspect_file is None and frames:
        suspect_file, suspect_line = frames[-1][0], int(frames[-1][1])

    # Last non-empty line of stderr is usually `ExceptionType: message`
    exc_type = "Unknown"
    exc_value = ""
    for line in reversed(stderr_text.strip().splitlines()):
        line = line.strip()
        if not line or line.startswith("  ") or line.startswith("File "):
            continue
        m = re.match(r'^([A-Za-z_][\w\.]*Error|Exception|SyntaxError|ImportError|'
                     r'NameError|AttributeError|TypeError|ValueError|KeyError|'
                     r'ModuleNotFoundError|IndentationError|RuntimeError)'
                     r'(?::\s*(.*))?$', line)
        if m:
            exc_type = m.group(1)
            exc_value = (m.group(2) or "").strip()
            break

    # Normalise suspect path to a repo-relative path where possible
    suspect_rel = suspect_file
    if suspect_rel:
        if suspect_rel.startswith(APP_DIR + "/"):
            suspect_rel = suspect_rel[len(APP_DIR) + 1:]
        elif suspect_rel.startswith("/app/"):
            suspect_rel = suspect_rel[5:]
        elif suspect_rel.startswith("./"):
            suspect_rel = suspect_rel[2:]

    return {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "exc_type": exc_type,
        "exc_value": exc_value,
        "suspect_file": suspect_file,
        "suspect_file_rel": suspect_rel,
        "suspect_line": suspect_line,
        "traceback": stderr_text[-12000:],
        "exit_code": exit_code,
        "source": "launcher",
    }


def _crash_loop_record(crash_times, exit_code, stderr_text, elapsed) -> dict:
    """Summarise a crash loop for the recovery UI.

    Built here rather than parsed from stderr because these exits usually carry
    no traceback at all — loop_monitor calls os._exit(70), which skips main.py's
    excepthook. The stderr tail is still attached when there is one.
    """
    info = _parse_crash_from_stderr(stderr_text or "", exit_code)
    span = (crash_times[-1] - crash_times[0]) if len(crash_times) > 1 else 0.0
    info["exc_type"] = "CrashLoop"
    info["exc_value"] = (
        f"{len(crash_times)} crashes in {span / 60:.1f} min "
        f"(last exit code {exit_code} after {elapsed:.0f}s). "
        f"Restarting is not resolving it."
    )
    info["source"] = "launcher_crash_loop"
    info["crash_count"] = len(crash_times)
    info["crash_window_seconds"] = int(span)
    return info


def _read_version_state() -> dict:
    try:
        with open(VERSION_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _request_auto_rollback(crash_times) -> bool:
    """Ask the host-side watcher to swap back to the previous image.

    The launcher runs INSIDE the container, so it cannot swap its own image.
    It writes the same trigger the Settings UI uses; the host systemd path unit
    (zmm-upgrade.path) picks it up and runs upgrade.sh's rollback action.

    Guarded deliberately — an automatic version rollback is a big hammer:
      * there must actually be a previous version recorded;
      * at most ONE attempt per version, so a crash loop that is nothing to do
        with the upgrade cannot ping-pong between two images forever;
      * only for a RECENT upgrade. A box that has run the same version happily
        for a week and starts looping has a new problem — data, hardware, a
        dying disk — and rolling back the code would just hide it.
    Returns True if a rollback was requested.
    """
    state = _read_version_state()
    current = (state.get("current_version") or "").strip()
    previous = (state.get("previous_version") or "").strip()

    if not previous:
        _log("Auto-rollback: no previous version recorded — skipping")
        return False
    if previous == current:
        _log("Auto-rollback: previous version equals current — skipping")
        return False

    installed_at = state.get("installed_at") or ""
    try:
        ts = datetime.datetime.strptime(installed_at, "%Y-%m-%dT%H:%M:%SZ")
        now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        age = (now_utc - ts).total_seconds()
    except Exception:
        age = -1.0
    if age < 0:
        _log("Auto-rollback: upgrade time unknown — skipping (manual recovery)")
        return False
    if age > ROLLBACK_MAX_UPGRADE_AGE:
        _log(f"Auto-rollback: current version installed {age / 3600:.1f}h ago, "
             f"older than {ROLLBACK_MAX_UPGRADE_AGE / 3600:.0f}h — not a fresh "
             f"upgrade, so this is unlikely to be the new version's fault")
        return False

    try:
        if os.path.isfile(ROLLBACK_MARKER):
            with open(ROLLBACK_MARKER) as f:
                already = (f.read() or "").strip()
            if already == current:
                _log(f"Auto-rollback: already attempted for {current} — not "
                     f"retrying (recovery standby instead)")
                return False
    except Exception:
        pass

    try:
        os.makedirs(os.path.dirname(TRIGGER_FILE), exist_ok=True)
        payload = {"action": "rollback",
                   "payload": {"previous_version": previous,
                               "reason": f"crash loop: {len(crash_times)} "
                                         f"crashes after upgrade to {current}"}}
        tmp = TRIGGER_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        # PathChanged fires when the file is closed after writing, so write to a
        # temp name and rename — a half-written trigger must never be picked up.
        os.replace(tmp, TRIGGER_FILE)
        with open(ROLLBACK_MARKER, "w") as f:
            f.write(current)
        _log(f"Auto-rollback REQUESTED: {current} -> {previous} "
             f"(crash loop after a {age / 60:.0f} min old upgrade)")
        return True
    except Exception as e:
        _log(f"Auto-rollback: could not write trigger: {e}")
        return False


def _write_crash(info: dict):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CRASH_FILE, "w") as f:
            json.dump(info, f, indent=2)
        _log(f"Crash recorded: {info.get('exc_type')}: {info.get('exc_value')} "
             f"@ {info.get('suspect_file_rel')}:{info.get('suspect_line')}")
    except Exception as e:
        _log(f"Failed to write crash file: {e}")


def _main_wrote_crash_recently(elapsed_since_start: float) -> bool:
    """main.py installs its own excepthook that writes last_crash.json.
    If it succeeded, prefer that over our stderr parse."""
    try:
        if not os.path.isfile(CRASH_FILE):
            return False
        mtime = os.stat(CRASH_FILE).st_mtime
        return (time.time() - mtime) < (elapsed_since_start + 5)
    except Exception:
        return False


# RECOVERY STANDBY (stdlib-only — recovery UI itself lives in the ZMM Manager)

_STANDBY_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZMM — recovery mode</title><style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0a0f1c;color:#e2e8f0;font-family:system-ui,sans-serif;text-align:center}}
.card{{max-width:26rem;padding:2.5rem;background:#111827;border:1px solid #1f2937;
border-radius:16px}}h1{{font-size:1.15rem;margin:.8rem 0}}p{{color:#94a3b8;font-size:.9rem;
line-height:1.5}}a{{display:inline-block;margin-top:1rem;background:#6366f1;color:#fff;
text-decoration:none;padding:.6rem 1.4rem;border-radius:8px;font-weight:600}}
.dot{{font-size:2rem}}img.logo{{width:64px;height:64px;border-radius:50%;
border:2px solid #f59e0b}}</style></head><body><div class="card">
<img class="logo" src="/logo" alt=""
 onerror="this.outerHTML='<div class=\\'dot\\'>&#128736;&#65039;</div>'">
<h1>ZMM is in recovery mode</h1>
<p>The application failed to start. Crash details, backup restore and file
repair are available in the ZMM&nbsp;Manager.</p>
<a href="{manager_url}">Open ZMM Manager</a></div></body></html>"""


def _recovery_standby() -> str:
    """Serve a redirect-to-manager page on :8000 and wait for the manager to
    write RESUME_MARKER (retry main.py) or for SIGTERM (container stopping).
    Returns "resume" or "shutdown"."""
    import http.server
    import json as _json
    import socketserver

    port = int(os.environ.get("ZMM_PORT", 8000))

    # Advertise the manager on the same scheme/host the user came in on.
    page = _STANDBY_HTML.format(manager_url=f"https://{{host}}:{MANAGER_PORT}")

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            if self.path == "/logo":
                logo = os.path.join(APP_DIR, "static", "images",
                                    "zigbee-manager-logo.png")
                try:
                    with open(logo, "rb") as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                except OSError:
                    body = b"not found"
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/api/"):
                body = _json.dumps({"status": "recovery",
                                    "manager_port": MANAGER_PORT}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
            else:
                host = (self.headers.get("Host") or "").split(":")[0] or "localhost"
                body = page.replace("{host}", host).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server(("0.0.0.0", port), Handler)

    # The app serves HTTPS; with HSTS-style browser caching a plain-HTTP
    # standby on the same port would be unreachable. Certs live on the
    # data mount at the default path main.py uses.
    cert = os.path.join(DATA_DIR, "certs", "cert.pem")
    key = os.path.join(DATA_DIR, "certs", "key.pem")
    if os.path.isfile(cert) and os.path.isfile(key):
        try:
            import ssl
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=cert, keyfile=key)
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            _log("Recovery standby: TLS enabled")
        except Exception as e:
            _log(f"Recovery standby: cert load failed ({e}) — serving HTTP")

    # Signal recovery mode to the manager (and its watchdog, which stands
    # down while this marker exists). Clear any stale resume marker first.
    try:
        if os.path.isfile(RESUME_MARKER):
            os.remove(RESUME_MARKER)
        with open(RECOVERY_MARKER, "w") as f:
            _json.dump({"ts": time.time(), "pid": os.getpid(),
                        "manager_port": MANAGER_PORT}, f)
    except Exception as e:
        _log(f"Recovery standby: marker write failed: {e}")

    threading.Thread(target=server.serve_forever, daemon=True).start()
    _log(f"Recovery standby serving on :{port} — fix via the ZMM Manager "
         f"(:{MANAGER_PORT}), then click Resume there")

    result = "shutdown"
    try:
        while not _stop_event.is_set():
            if os.path.isfile(RESUME_MARKER):
                _log("Recovery standby: resume requested by manager")
                result = "resume"
                break
            time.sleep(2)
    finally:
        server.shutdown()
        server.server_close()
        for marker in (RESUME_MARKER, RECOVERY_MARKER):
            try:
                if os.path.isfile(marker):
                    os.remove(marker)
            except Exception:
                pass
    return result


# MAIN LOOP

def main():
    _log(f"Launcher starting (APP_DIR={APP_DIR})")

    # Readiness gate: wait for the Zigbee dongle before the first launch so the
    # frontend comes up once, fully functional — instead of starting too early,
    # crashing on a not-yet-enumerated device, and being restarted by systemd.
    _wait_for_device()

    quick_retries = 0
    # Timestamps of recent runtime crashes, pruned to RUNTIME_CRASH_WINDOW.
    runtime_crashes: list = []
    while True:
        # 1. Boot guard (best effort — never blocks)
        if os.path.isfile(BOOT_GUARD):
            rc, _, _ = _run_child(BOOT_GUARD, capture_stderr=False)
            _log(f"Boot guard exit={rc}")

        # 2. Main app
        if not os.path.isfile(MAIN_PY):
            _log(f"FATAL: {MAIN_PY} not found")
            return 1

        _log("Starting main.py ...")
        code, stderr_text, elapsed = _run_child(MAIN_PY, capture_stderr=True)
        _log(f"main.py exited code={code} after {elapsed:.1f}s")

        if code == 0:
            _log("Clean shutdown — launcher exiting")
            return 0

        # Negative exit codes mean a signal. SIGTERM (-15, `podman stop`, which is
        # what an upgrade swap does) and SIGINT (-2, Ctrl+C) are polite shutdown
        # requests, so exit cleanly rather than recover. SIGKILL (-9) is
        # deliberately not caught — it usually means OOM and is worth recovering.
        if code in (-15, -2):
            sig_name = "SIGTERM" if code == -15 else "SIGINT"
            _log(f"main.py terminated by {sig_name} after {elapsed:.1f}s "
                 f"(graceful shutdown request) — launcher exiting")
            return 0

        if elapsed >= HEALTHY_SECONDS:
            # Ran healthy then died. One of these is bad luck; several in a row
            # is a crash loop that will never resolve itself, so count them.
            now = time.time()
            runtime_crashes.append(now)
            runtime_crashes[:] = [t for t in runtime_crashes
                                  if now - t <= RUNTIME_CRASH_WINDOW]

            if len(runtime_crashes) >= MAX_RUNTIME_CRASHES:
                _log(f"Crash loop detected: {len(runtime_crashes)} runtime "
                     f"crashes within {RUNTIME_CRASH_WINDOW}s — restarting is "
                     f"not fixing it. Entering recovery standby.")
                # main.py's excepthook does not fire here: loop_monitor exits via
                # os._exit(70), so last_crash.json would hold something stale and
                # the recovery UI would explain the wrong thing.
                _write_crash(_crash_loop_record(
                    runtime_crashes, code, stderr_text, elapsed))

                # If this started right after an upgrade, the new version is the
                # prime suspect — ask the host to put the old one back. Standby
                # still runs: the swap happens out here, and until it lands the
                # user needs something on :8000 telling them what is going on.
                if _request_auto_rollback(runtime_crashes):
                    _log("Auto-rollback requested — standing by for the host "
                         "watcher to swap the previous image back in")

                outcome = _recovery_standby()
                if outcome == "resume":
                    runtime_crashes.clear()
                    quick_retries = 0
                    continue
                _log("Recovery standby shut down — launcher exiting")
                return 0

            _log(f"Runtime crash (lived {elapsed:.1f}s) — "
                 f"{len(runtime_crashes)}/{MAX_RUNTIME_CRASHES} in the last "
                 f"{RUNTIME_CRASH_WINDOW}s — restart in {RESTART_BACKOFF}s")
            # NOTE: quick_retries is deliberately NOT reset here any more. It
            # used to be, which meant a run alternating boot crashes with
            # runtime crashes cleared the boot counter every other pass and
            # never escalated either.
            time.sleep(RESTART_BACKOFF)
            continue

        # Boot-time crash — capture and go to recovery
        if not _main_wrote_crash_recently(elapsed):
            _write_crash(_parse_crash_from_stderr(stderr_text, code))

        quick_retries += 1
        if quick_retries > MAX_QUICK_RETRIES:
            _log(f"Too many quick boot failures ({quick_retries}) — entering recovery mode unconditionally")

        _log("Entering recovery standby (repair via the ZMM Manager) ...")
        outcome = _recovery_standby()
        if outcome == "resume":
            quick_retries = 0
            continue
        _log("Recovery standby shut down — launcher exiting")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _log("Interrupted — exiting")
        sys.exit(0)
    except Exception as e:
        _log(f"Launcher crashed: {e}")
        sys.exit(1)