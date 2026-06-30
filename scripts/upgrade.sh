#!/bin/bash
# =============================================================================
# ZMM Upgrade — Host-Side Orchestrator
#
# Reads trigger files written by the running container and performs:
#   build     — clone target tag, build new image tagged with version
#   swap      — stop current container, rename, run new image, health-check
#   rollback  — swap back to previous image
#   cancel    — best-effort kill of in-progress build
#   gc        — prune old images per retention count
#
# Runs on the host (user or root systemd, or fallback polling wrapper).
# NEVER runs inside the container. Has full access to podman/docker.
#
# Works with Podman (preferred) and Docker, rootless or root.
# =============================================================================
set -u  # NOTE: not -e; we want to catch errors and report them cleanly.
set -o pipefail

# ── CONFIG ───────────────────────────────────────────────────────────────────
DATA_DIR="${ZMM_DATA_DIR:-/opt/.zigbee-matter-manager}"
APP_DIR="${ZMM_APP_DIR:-/opt/.zigbee-matter-manager/upgrade_build}"
IMAGE_NAME="${ZMM_IMAGE_NAME:-zigbee-matter-manager}"
CONTAINER_NAME="${ZMM_CONTAINER_NAME:-zigbee-matter-manager}"
REPO_URL="${ZMM_REPO_URL:-https://github.com/oneofthemany/ZigBee-Matter-Manager.git}"
HEALTH_TIMEOUT="${ZMM_HEALTH_TIMEOUT:-60}"  # seconds to wait for new container to become healthy

# Health check URL is auto-detected from config.yaml at health-check time —
# see detect_health_url(). Override with $ZMM_HEALTH_URL if needed.
HEALTH_URL="${ZMM_HEALTH_URL:-}"
# The port published by the previous container — discovered from inspect if possible.

# ── IPC paths (shared with container via volume mount) ───────────────────────
UPGRADE_DIR="${DATA_DIR}/data/upgrade"
TRIGGER_FILE="${UPGRADE_DIR}/trigger"
STATUS_FILE="${UPGRADE_DIR}/status.json"
BUILD_LOG="${UPGRADE_DIR}/build.log"
LOCK_FILE="${UPGRADE_DIR}/lock"
WATCHER_MARKER="${UPGRADE_DIR}/.watcher_installed"

# State file used by the app (read-only for us, but we update current/previous on swap)
STATE_DIR="${DATA_DIR}/data/state"
VERSION_STATE_FILE="${STATE_DIR}/version.json"

# Log for the watcher itself (separate from build.log)
WATCHER_LOG="${DATA_DIR}/logs/upgrade_watcher.log"

mkdir -p "$UPGRADE_DIR" "$STATE_DIR" "$(dirname "$WATCHER_LOG")"

# ── LOGGING ──────────────────────────────────────────────────────────────────
log() {
    local ts
    ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    echo "[$ts] $*" | tee -a "$WATCHER_LOG" >&2
}

log_to_build() {
    local ts
    ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    echo "[$ts] $*" | tee -a "$BUILD_LOG" >&2
}

# ── RUNTIME DETECTION (rootless podman / root podman / docker) ───────────────
detect_runtime() {
    if [[ -n "${RUNTIME:-}" ]]; then
        command -v "$RUNTIME" &>/dev/null || { log "RUNTIME $RUNTIME not found"; return 1; }
    elif command -v podman &>/dev/null; then
        RUNTIME="podman"
    elif command -v docker &>/dev/null; then
        RUNTIME="docker"
    else
        log "ERROR: Neither podman nor docker found in PATH"
        return 1
    fi
    log "Using container runtime: $RUNTIME"
}

# ── STATUS WRITER ────────────────────────────────────────────────────────────
write_status() {
    local state="$1"
    local target_version="${2:-null}"
    local progress="${3:-0}"
    local step="${4:-}"
    local err="${5:-}"
    local started_at="${6:-$(date -u +"%Y-%m-%dT%H:%M:%SZ")}"

    # Quote target_version correctly
    local tv_json
    if [[ "$target_version" == "null" || -z "$target_version" ]]; then
        tv_json="null"
    else
        tv_json="\"${target_version}\""
    fi

    local err_json
    if [[ -z "$err" ]]; then
        err_json="null"
    else
        # escape quotes
        err_json="\"$(echo "$err" | sed 's/\\/\\\\/g; s/"/\\"/g')\""
    fi

    local step_json
    step_json="\"$(echo "$step" | sed 's/\\/\\\\/g; s/"/\\"/g')\""

    local tmp="${STATUS_FILE}.tmp"
    cat > "$tmp" <<JSON
{
  "state": "${state}",
  "target_version": ${tv_json},
  "started_at": "${started_at}",
  "updated_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "progress_percent": ${progress},
  "current_step": ${step_json},
  "error": ${err_json}
}
JSON
    mv "$tmp" "$STATUS_FILE"
}

# ── LOCKING ──────────────────────────────────────────────────────────────────
# A stale lock from a killed/crashed previous run will block ALL future runs
# unless we detect-and-clear it. Lock file format: "PID TIMESTAMP ACTION"
acquire_lock() {
    if [[ -f "$LOCK_FILE" ]]; then
        local held_pid
        held_pid=$(awk '{print $1}' "$LOCK_FILE" 2>/dev/null || echo "")
        local held
        held=$(cat "$LOCK_FILE" 2>/dev/null || echo "unknown")

        # Is the holder still alive?
        if [[ -n "$held_pid" ]] && kill -0 "$held_pid" 2>/dev/null; then
            log "Lock held by live PID $held_pid: $held"
            return 1
        fi

        # Stale lock — clear it
        log "Removing stale lock (PID $held_pid not running): $held"
        rm -f "$LOCK_FILE"
    fi
    echo "$$ $(date -u +"%Y-%m-%dT%H:%M:%SZ") $1" > "$LOCK_FILE"
    return 0
}

release_lock() {
    rm -f "$LOCK_FILE"
}

# ── TRIGGER CONSUMPTION ──────────────────────────────────────────────────────
# CRITICAL: we MUST delete the trigger file before doing anything else.
# Otherwise the systemd-path unit re-fires us in a tight loop.
consume_trigger() {
    if [[ ! -f "$TRIGGER_FILE" ]]; then
        return 1
    fi

    # Read contents into memory FIRST, then delete the file.
    # If we crash after this, the path unit won't re-fire because the file is gone.
    local trigger_content
    trigger_content=$(cat "$TRIGGER_FILE" 2>/dev/null || echo "")
    rm -f "$TRIGGER_FILE"

    if [[ -z "$trigger_content" ]]; then
        log "Empty trigger file; ignoring"
        return 1
    fi

    if ! command -v jq &>/dev/null; then
        log "ERROR: jq is required for the upgrade watcher"
        write_status "failed" "" 0 "" "jq not installed on host"
        return 1
    fi

    TRIGGER_ACTION=$(echo "$trigger_content" | jq -r '.action // empty' 2>/dev/null || echo "")
    TRIGGER_PAYLOAD=$(echo "$trigger_content" | jq -c '.payload // {}' 2>/dev/null || echo "{}")

    if [[ -z "$TRIGGER_ACTION" ]]; then
        log "Malformed trigger (no action)"
        write_status "failed" "" 0 "" "Malformed trigger file"
        return 1
    fi

    log "Consumed trigger: action=$TRIGGER_ACTION payload=$TRIGGER_PAYLOAD"
    return 0
}

# ── ARCHITECTURE DETECTION ───────────────────────────────────────────────────
detect_arch() {
    local m
    m=$(uname -m)
    case "$m" in
        x86_64|amd64)           echo "amd64" ;;
        aarch64|arm64)          echo "arm64" ;;
        armv7l|armv7)           echo "armv7" ;;
        *)                       echo "$m" ;;
    esac
}

# ── HEALTH CHECK URL DETECTION ──────────────────────────────────────────────
# The new container may be running plain HTTP, or HTTPS (with a self-signed
# cert). config.yaml tells us which — but we read it defensively because
# config.yaml shape can vary across versions.
#
# We build a list of candidate URLs to try, in priority order:
#   1. $ZMM_HEALTH_URL (if set explicitly — overrides everything)
#   2. https://127.0.0.1:${port}/api/system/health   (if web.ssl.enabled is true)
#   3. http://127.0.0.1:${port}/api/system/health    (fallback for non-SSL setups)
#
# is_app_healthy() returns 0 if ANY of the candidates returns 200.
detect_health_urls() {
    local config="${DATA_DIR}/config/config.yaml"
    local port="8000"
    local ssl_enabled="false"

    # If user has set ZMM_HEALTH_URL, use only that
    if [[ -n "${HEALTH_URL:-}" ]]; then
        echo "$HEALTH_URL"
        return 0
    fi

    # Best-effort YAML parsing without yq dependency. Look for:
    #   web:
    #     port: 8000
    #     ssl:
    #       enabled: true            (or "enabled", "yes", etc.)
    #       certfile: ./...          (or cert_file: with underscore)
    #       keyfile: ./...           (or key_file: with underscore)
    #
    # Accept both naming conventions because real configs use either.
    # Treat "enabled" as truthy unless the value is explicitly falsy.
    if [[ -f "$config" ]]; then
        # Extract port (anywhere under "web:" stanza). Keep simple — first hit wins.
        local p
        p=$(awk '
            /^web:/         { in_web=1; next }
            /^[a-zA-Z]/     { in_web=0 }
            in_web && /^  port:/ { gsub(/[^0-9]/,"",$2); print $2; exit }
        ' "$config" 2>/dev/null)
        [[ -n "$p" && "$p" =~ ^[0-9]+$ ]] && port="$p"

        # Extract ssl.enabled. Look for the nested key.
        local s
        s=$(awk '
            /^web:/         { in_web=1; next }
            /^[a-zA-Z]/     { in_web=0; in_ssl=0 }
            in_web && /^  ssl:/ { in_ssl=1; next }
            in_web && /^  [a-zA-Z]/ { in_ssl=0 }
            in_web && in_ssl && /^    enabled:/ { print $2; exit }
        ' "$config" 2>/dev/null | tr -d '"' | tr -d "'" | tr '[:upper:]' '[:lower:]')
        # Truthy unless explicitly falsy. "enabled" itself is truthy.
        case "$s" in
            ""|false|no|0|off|disabled|none|null)
                ssl_enabled="false"
                ;;
            *)
                ssl_enabled="true"
                ;;
        esac
    fi

    # Output candidate URLs, one per line, in priority order.
    # We only check /api/system/health — the canonical health endpoint.
    if [[ "$ssl_enabled" == "true" ]]; then
        echo "https://127.0.0.1:${port}/api/system/health"
    fi
    echo "http://127.0.0.1:${port}/api/system/health"
}

# Try each candidate URL once. Returns 0 if any succeeds, prints the URL that
# worked to stdout (so caller can log it).
is_app_healthy() {
    local urls=("$@")
    for url in "${urls[@]}"; do
        # -k: accept self-signed certs. Most home setups use them.
        # --max-time 3: don't wait more than 3s per URL per attempt.
        # -fsS: silent, fail-on-non-2xx, but show error if curl itself fails.
        if curl -fsS -k --max-time 3 "$url" >/dev/null 2>&1; then
            echo "$url"
            return 0
        fi
    done
    return 1
}

# ── HELPER LOCATION ─────────────────────────────────────────────────────────
# Locate run_container.sh in the canonical location ${APP_DIR}/scripts/.
# Falls back to legacy ${DATA_DIR}/scripts/ for older installs that ran
# install_watcher.sh before the single-source-of-truth migration.
find_run_helper() {
    for candidate in \
        "${APP_DIR}/scripts/run_container.sh" \
        "${DATA_DIR}/upgrade_build/scripts/run_container.sh" \
        "${DATA_DIR}/scripts/run_container.sh" \
        "/opt/zigbee-matter-manager/scripts/run_container.sh"; do
        if [[ -n "$candidate" && -f "$candidate" ]]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

# ── PORT FREE WAITER ────────────────────────────────────────────────────────
# After podman stop, the host TCP sockets can stay in TIME_WAIT for up to ~60s,
# AND rogue child processes (or rootlessport itself) may still be holding the
# port. We poll until the ports are actually bindable, with a timeout.
#
# Returns 0 if all ports become free within $timeout seconds, 1 otherwise.
wait_for_ports_free() {
    local timeout="${1:-90}"
    local elapsed=0
    local sleep_interval=2
    local ports=("8000" "5580")
    local stable_required=2  # require N consecutive checks to pass before declaring free
    local stable_count=0

    # Initial settle delay — after SIGKILL, rootlessport needs ~1-2s to fully
    # release its sockets before they appear truly free. The kernel may
    # report no-LISTEN before the socket is actually bindable.
    sleep 1

    while (( elapsed < timeout )); do
        local all_free=1

        # Check 1: Are any sockets listening or in active states on these ports?
        # We check ANY state (not just LISTEN) because TIME_WAIT/CLOSE_WAIT also
        # block bind. ss with -a includes all states.
        for port in "${ports[@]}"; do
            if command -v ss >/dev/null 2>&1; then
                # Look for any non-empty result on either IPv4 or IPv6
                if ss -tan "( sport = :$port or dport = :$port )" 2>/dev/null | \
                   awk 'NR>1 && $1!="LISTEN" {found=1} END {exit !found}' >/dev/null 2>&1; then
                    all_free=0
                    break
                fi
                if ss -ltn "( sport = :$port )" 2>/dev/null | grep -q LISTEN; then
                    all_free=0
                    break
                fi
            elif command -v netstat >/dev/null 2>&1; then
                if netstat -tan 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$"; then
                    all_free=0
                    break
                fi
            fi
        done

        # Check 2: Definitive test — try to actually bind to each port
        # Only do this if Check 1 says ports look free, otherwise it's wasted effort
        if (( all_free == 1 )); then
            for port in "${ports[@]}"; do
                # Use python or perl to attempt a real bind. Falls through to
                # a /dev/tcp probe if neither is available.
                if command -v python3 >/dev/null 2>&1; then
                    if ! python3 -c "
import socket, sys
for af in (socket.AF_INET, socket.AF_INET6):
    try:
        s = socket.socket(af, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('::' if af == socket.AF_INET6 else '0.0.0.0', $port))
        s.close()
    except OSError:
        sys.exit(1)
" 2>/dev/null; then
                        all_free=0
                        break
                    fi
                fi
            done
        fi

        if (( all_free == 1 )); then
            stable_count=$((stable_count + 1))
            if (( stable_count >= stable_required )); then
                log "Ports ${ports[*]} are free and bindable (waited ${elapsed}s, ${stable_count} stable checks)"
                return 0
            fi
        else
            stable_count=0
        fi

        sleep "$sleep_interval"
        elapsed=$((elapsed + sleep_interval))
    done

    log "WARN: Ports still in use after ${timeout}s — proceeding anyway"
    return 1
}

# Best-effort kill of any host processes holding the ports we need.
# Used as a last resort if wait_for_ports_free times out.
kill_port_squatters() {
    local ports=("$@")
    if [[ ${#ports[@]} -eq 0 ]]; then
        ports=("8000" "5580")
    fi
    for port in "${ports[@]}"; do
        local pids
        if command -v ss >/dev/null 2>&1; then
            pids=$(ss -ltnp "( sport = :$port )" 2>/dev/null | \
                   grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)
        elif command -v fuser >/dev/null 2>&1; then
            pids=$(fuser -n tcp "$port" 2>/dev/null | tr -d ' ')
        else
            pids=""
        fi

        for pid in $pids; do
            # Sanity: never kill PID 1 or systemd
            if [[ "$pid" == "1" ]] || [[ "$pid" == "$$" ]]; then
                continue
            fi
            log "Killing port-squatter PID $pid on port $port"
            kill -TERM "$pid" 2>/dev/null || true
            sleep 1
            kill -KILL "$pid" 2>/dev/null || true
        done
    done
}

# ── HEALTH WAITER (stable + version-aware) ──────────────────────────────────
# Poll the candidate URLs until the app answers 200 for N CONSECUTIVE checks
# (a single 200 isn't enough — a container can answer once then crash-loop).
#
# If $expect_version is non-empty AND the health response includes a "version"
# field, the version must match before a check counts. This catches the case
# where the OLD image somehow ended up serving the port after the swap. It is
# best-effort: a health response WITHOUT a version field (older image being
# rolled back to) still passes, so we never break rollbacks to old tags.
#
# Args: timeout_seconds expect_version url...
# Returns 0 once stable-healthy, 1 on timeout.
wait_until_healthy() {
    local timeout="$1"; shift
    local expect_version="$1"; shift
    local urls=("$@")
    local elapsed=0 stable=0 working=""
    local need=2  # consecutive passes required

    [[ ${#urls[@]} -eq 0 ]] && return 1

    while (( elapsed < timeout )); do
        if working=$(is_app_healthy "${urls[@]}"); then
            if [[ -n "$expect_version" ]]; then
                local got
                got=$(curl -fsS -k --max-time 3 "$working" 2>/dev/null \
                      | grep -oE '"version"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 \
                      | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')
                if [[ -n "$got" && "$got" != "$expect_version" ]]; then
                    log "Health: responding but version=$got (want $expect_version) — not counting"
                    stable=0
                    sleep 3; elapsed=$((elapsed + 3)); continue
                fi
            fi
            stable=$((stable + 1))
            if (( stable >= need )); then
                log "Health: stable ($stable consecutive passes) via $working"
                return 0
            fi
        else
            stable=0
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    return 1
}

# ── HARDENED ROLLBACK ────────────────────────────────────────────────────────
# Single rollback path used by every do_swap failure branch. Relies on do_swap's
# locals ($previous_name, $target_version) via bash dynamic scope.
#
# Unlike the old inline rollback, this one:
#   - waits for the dying container to release :8000/:5580 before restarting the
#     previous one (otherwise the restore fails to bind and we end up with
#     NOTHING running),
#   - VERIFIES the restored container actually serves (not just "started"),
#   - retries once with a port-squatter kill, and
#   - reports an honest CRITICAL status if it genuinely cannot restore service.
rollback_to_previous() {
    local reason="${1:-unknown failure}"
    log "Rollback: $reason — restoring $previous_name"
    write_status "rolling_back" "$target_version" 85 "Rolling back: $reason"

    # Tear down the failed new container.
    "$RUNTIME" stop -t 10 "$CONTAINER_NAME" >/dev/null 2>&1 || true
    "$RUNTIME" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

    # Let the ports come free before we try to bind them again.
    if ! wait_for_ports_free 60; then
        log "Rollback: ports still busy — killing squatters"
        kill_port_squatters 8000 5580
    fi

    if ! "$RUNTIME" inspect "$previous_name" >/dev/null 2>&1; then
        log "Rollback: CRITICAL — previous container $previous_name not found"
        unmask_unit_if_needed
        container_unit_start
        write_status "failed" "$target_version" 100 "Rollback failed" \
            "Upgrade failed ($reason) AND no previous container was available to restore. Manual intervention required — see build.log / upgrade_watcher.log."
        return 1
    fi

    "$RUNTIME" rename "$previous_name" "$CONTAINER_NAME" >/dev/null 2>&1 || true
    "$RUNTIME" start "$CONTAINER_NAME" >>"$WATCHER_LOG" 2>&1 || true
    unmask_unit_if_needed
    container_unit_start

    # Build health candidates from config and CONFIRM the restored app serves.
    local health_candidates=()
    while IFS= read -r url; do [[ -n "$url" ]] && health_candidates+=("$url"); done < <(detect_health_urls)

    if wait_until_healthy 45 "" "${health_candidates[@]}"; then
        log "Rollback: previous container restored and verified healthy"
        write_status "failed" "$target_version" 100 "Rolled back" \
            "Upgrade failed ($reason); previous version restored and verified healthy. See build.log."
        return 1
    fi

    # Restored container didn't come up — last-resort kill + restart.
    log "Rollback: restored container not healthy — last-resort restart"
    kill_port_squatters 8000 5580
    "$RUNTIME" restart "$CONTAINER_NAME" >>"$WATCHER_LOG" 2>&1 || true
    if wait_until_healthy 45 "" "${health_candidates[@]}"; then
        log "Rollback: recovered on retry"
        write_status "failed" "$target_version" 100 "Rolled back (after retry)" \
            "Upgrade failed ($reason); previous version restored after a retry. See build.log."
        return 1
    fi

    log "Rollback: CRITICAL — could not restore a healthy service"
    write_status "failed" "$target_version" 100 "Rollback could not restore service" \
        "CRITICAL: upgrade failed ($reason) and the previous version did NOT come back healthy. The app may be DOWN — check '${RUNTIME} ps -a' and ${WATCHER_LOG}."
    return 1
}

# ── BUILD: clone target tag, build image, tag with version ──────────────────
do_build() {
    local target_version
    target_version=$(echo "$TRIGGER_PAYLOAD" | jq -r '.target_version // empty')
    if [[ -z "$target_version" ]]; then
        log "Build: no target_version in payload"
        write_status "failed" "" 0 "" "No target_version specified"
        return 1
    fi

    local arch
    arch=$(detect_arch)
    local new_tag="${IMAGE_NAME}:${target_version}-${arch}"
    local started_at
    started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    # Reset build log
    : > "$BUILD_LOG"
    log_to_build "=== ZMM Upgrade Build ==="
    log_to_build "Target version: $target_version"
    log_to_build "Architecture:   $arch"
    log_to_build "Target tag:     $new_tag"
    log_to_build "Runtime:        $RUNTIME"
    log_to_build ""

    write_status "building" "$target_version" 5 "Preparing" "" "$started_at"

    # ── Sanity check: is APP_DIR in the expected state? ──────────────────────
    # Before wiping APP_DIR, log what's there so the build log shows whether
    # we found a sane prior install. This is informational only — we always
    # proceed with the wipe-and-reclone because the new clone is authoritative,
    # but if APP_DIR was in an unexpected state it goes in the log.
    if [[ -d "$APP_DIR" ]]; then
        local existing_version="(no VERSION file)"
        [[ -f "$APP_DIR/VERSION" ]] && existing_version=$(tr -d '[:space:]' < "$APP_DIR/VERSION")
        log_to_build "APP_DIR exists at $APP_DIR (VERSION=${existing_version}) — will be wiped and re-cloned"

        # Cross-check against the running container's reported version. A
        # mismatch means APP_DIR drifted from the image at some point — not
        # fatal, but worth recording.
        local running_version
        running_version=$("$RUNTIME" exec "$CONTAINER_NAME" cat /app/VERSION 2>/dev/null | tr -d '[:space:]' || echo "")
        if [[ -n "$running_version" && "$existing_version" != "$running_version" && "$existing_version" != "(no VERSION file)" ]]; then
            log_to_build "WARN: APP_DIR VERSION (${existing_version}) does not match running container VERSION (${running_version})"
        fi
    else
        log_to_build "APP_DIR does not exist at $APP_DIR — fresh clone"
    fi

    # APP_DIR is the canonical app-code location and also the build workspace.
    # Wiping and re-cloning here means the post-build state of APP_DIR exactly
    # matches the image we're about to ship — including scripts/run_container.sh,
    # which do_swap will invoke from this path. No drift between the helper
    # script and the image possible.
    local work_dir="$APP_DIR"
    rm -rf "$work_dir"
    mkdir -p "$work_dir"

    log_to_build "Cloning $REPO_URL at tag v${target_version}..."
    write_status "building" "$target_version" 10 "Cloning repository" "" "$started_at"

    if ! git clone --depth 1 --branch "v${target_version}" "$REPO_URL" "$work_dir" >>"$BUILD_LOG" 2>&1; then
        log_to_build "ERROR: git clone failed for tag v${target_version}"
        # Try without the 'v' prefix as a fallback
        rm -rf "$work_dir"
        mkdir -p "$work_dir"
        if ! git clone --depth 1 --branch "${target_version}" "$REPO_URL" "$work_dir" >>"$BUILD_LOG" 2>&1; then
            log_to_build "ERROR: git clone failed for tag ${target_version} as well"
            write_status "failed" "$target_version" 10 "Clone failed" "git clone failed for tag v${target_version}" "$started_at"
            return 1
        fi
    fi

    # Stamp VERSION file into the clone so the image knows its own version
    echo "$target_version" > "$work_dir/VERSION"

    # ── Appender choice: read the persisted marker from previous install ─────
    # build.sh writes ${DATA_DIR}/data/state/appender.enabled at install time
    # (containing 'true' or 'false') to record whether the user passed
    # --with-appender. We read it here and propagate the choice to the new
    # tag's write_containerfile via the WITH_APPENDER variable. If the marker
    # is missing (e.g. install pre-dates this mechanism) we default to false,
    # which is build.sh's default and matches the small/home-network use case.
    local appender_marker="${DATA_DIR}/data/state/appender.enabled"
    local with_appender="false"
    if [[ -f "$appender_marker" ]]; then
        with_appender=$(tr -d '[:space:]' < "$appender_marker")
        # Coerce anything other than 'true' to 'false' to keep it strict.
        [[ "$with_appender" == "true" ]] || with_appender="false"
        log_to_build "Appender marker found: ${appender_marker} = ${with_appender}"
    else
        log_to_build "Appender marker missing — defaulting WITH_APPENDER=false (matches build.sh default)"
    fi

    # Containerfile generation. With the new layout work_dir IS APP_DIR, so
    # we can't copy "an existing Containerfile from APP_DIR" — they're the
    # same path. The cloned tag's build.sh has write_containerfile() which
    # produces the Containerfile expected by that version.
    if [[ ! -f "$work_dir/Containerfile" ]]; then
        if [[ -f "$work_dir/build.sh" ]]; then
            log_to_build "Generating Containerfile via target tag's build.sh write_containerfile() (WITH_APPENDER=${with_appender})"
            (
                set +u
                # shellcheck disable=SC1090
                source "$work_dir/build.sh" >/dev/null 2>&1 || true
                if type write_containerfile >/dev/null 2>&1; then
                    # write_containerfile reads CLONE_DIR for the output path
                    # and WITH_APPENDER for the appender stanza toggle.
                    CLONE_DIR="$work_dir" APP_DIR="$work_dir" \
                    WITH_APPENDER="$with_appender" \
                        write_containerfile
                fi
            ) >>"$BUILD_LOG" 2>&1 || true
        fi
    fi

    if [[ ! -f "$work_dir/Containerfile" ]]; then
        log_to_build "ERROR: No Containerfile found or generatable"
        write_status "failed" "$target_version" 15 "Containerfile missing" "No Containerfile in tag v${target_version} and none to reuse" "$started_at"
        return 1
    fi

    # Detect build jobs
    local build_jobs
    if command -v nproc >/dev/null 2>&1; then
        build_jobs=$(nproc)
    else
        build_jobs=2
    fi
    (( build_jobs > 8 )) && build_jobs=8

    log_to_build ""
    log_to_build "Building image with $build_jobs parallel jobs..."
    log_to_build "Build time depends on host hardware (typically ~2-25 minutes)."
    log_to_build ""

    write_status "building" "$target_version" 20 "Compiling image (varies by host hardware)" "" "$started_at"

    # Run the build
    if ! "$RUNTIME" build \
            --format docker \
            --build-arg BUILD_JOBS="$build_jobs" \
            --tag "$new_tag" \
            --file "$work_dir/Containerfile" \
            "$work_dir" >>"$BUILD_LOG" 2>&1
    then
        log_to_build ""
        log_to_build "ERROR: Image build failed. See log above."
        write_status "failed" "$target_version" 50 "Build failed" "$RUNTIME build returned non-zero; see build.log" "$started_at"
        return 1
    fi

    # Also tag as :latest-<arch> for convenience
    "$RUNTIME" tag "$new_tag" "${IMAGE_NAME}:latest-${arch}" >>"$BUILD_LOG" 2>&1 || true

    log_to_build ""
    log_to_build "Build complete. Image tagged as $new_tag"
    log_to_build "Container swap has NOT happened yet. Swap is a separate action."

    write_status "ready_to_swap" "$target_version" 100 "Image ready — awaiting swap" "" "$started_at"
    return 0
}

# ── SUPERVISOR UNIT HANDLING ─────────────────────────────────────────────────
ZMM_CONTAINER_UNIT="${ZMM_CONTAINER_UNIT:-}"
UNIT_WAS_MASKED=0
UNIT_OVERRIDE_DIR=""

detect_container_unit() {
    if [[ -n "$ZMM_CONTAINER_UNIT" ]]; then
        echo "$ZMM_CONTAINER_UNIT"
        return 0
    fi
    local candidates=(
        "container-${CONTAINER_NAME}.service"
        "${CONTAINER_NAME}.service"
    )
    # System scope first (we run as root). User scope only if explicitly non-root.
    for unit in "${candidates[@]}"; do
        if systemctl --system cat "$unit" >/dev/null 2>&1; then
            echo "--system $unit"
            return 0
        fi
    done
    if [[ "$(id -u)" -ne 0 ]]; then
        for unit in "${candidates[@]}"; do
            if systemctl --user cat "$unit" >/dev/null 2>&1; then
                echo "--user $unit"
                return 0
            fi
        done
    fi
    return 1
}

# Drop a runtime override that disables Restart= for the swap window, then
# stop the unit. Pair with unmask_unit_if_needed.
container_unit_mask_and_stop() {
    local unit_desc; unit_desc=$(detect_container_unit) || {
        log "Supervisor: no unit detected (continuing without override)"
        return 0
    }
    local scope unit
    read -r scope unit <<< "$unit_desc"
    log "Supervisor: disabling auto-restart on $scope $unit (runtime drop-in)"

    local override_dir
    if [[ "$scope" == "--system" ]]; then
        override_dir="/run/systemd/system/${unit}.d"
    else
        override_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/systemd/user/${unit}.d"
    fi
    mkdir -p "$override_dir"
    cat > "${override_dir}/zzz-zmm-upgrade-norestart.conf" <<EOF
[Service]
Restart=no
EOF
    systemctl "$scope" daemon-reload >>"$WATCHER_LOG" 2>&1 || true
    UNIT_WAS_MASKED=1
    UNIT_OVERRIDE_DIR="$override_dir"

    systemctl "$scope" stop "$unit" >>"$WATCHER_LOG" 2>&1 || true
}

# Remove the runtime override if we placed one. Idempotent.
unmask_unit_if_needed() {
    if [[ "${UNIT_WAS_MASKED:-0}" == "1" ]]; then
        local unit_desc; unit_desc=$(detect_container_unit) || { UNIT_WAS_MASKED=0; return 0; }
        local scope unit
        read -r scope unit <<< "$unit_desc"
        log "Supervisor: removing auto-restart override on $scope $unit"

        if [[ -n "${UNIT_OVERRIDE_DIR:-}" && -d "$UNIT_OVERRIDE_DIR" ]]; then
            rm -f "${UNIT_OVERRIDE_DIR}/zzz-zmm-upgrade-norestart.conf"
            rmdir "$UNIT_OVERRIDE_DIR" 2>/dev/null || true
        fi
        systemctl "$scope" daemon-reload >>"$WATCHER_LOG" 2>&1 || true
        systemctl "$scope" reset-failed "$unit" >/dev/null 2>&1 || true
        UNIT_WAS_MASKED=0
        UNIT_OVERRIDE_DIR=""
    fi
}

# Start the supervisor unit. Call only after unmask_unit_if_needed.
container_unit_start() {
    local unit_desc; unit_desc=$(detect_container_unit) || return 0
    local scope unit
    read -r scope unit <<< "$unit_desc"
    log "Supervisor: starting $scope $unit"
    systemctl "$scope" start "$unit" >>"$WATCHER_LOG" 2>&1 || \
        log "Supervisor: warn — start failed for $scope $unit"
}

# ── HELPER SELF-HEAL ─────────────────────────────────────────────────────────
# Compares the WATCHER_SCHEMA_VERSION baked into the currently-installed
# systemd unit against the version bundled in the new image. If the new image
# is strictly newer, copy the four helper scripts (build.sh, upgrade.sh,
# run_container.sh, install_watcher.sh) out of the new image and re-run
# install_watcher.sh from the freshly-extracted location. This rewrites the
# systemd unit, reloads the daemon, and leaves $SCRIPTS_DIR + $APP_DIR/build.sh
# matching the new image's expectations.
#
# Called from do_swap() AFTER a successful swap+healthcheck. Failure here
# logs a warning but does not fail the upgrade — the user's system is up
# and running the new image; only the host-side helpers are stale, which
# blocks future upgrades but doesn't break what they have right now.
#
# Strict forward-only: never downgrades helpers.
self_heal_helpers() {
    local new_tag="$1"

    # Read the schema version baked into the currently-installed unit. We
    # check the most likely unit file paths in order of likelihood. Default
    # 0 if missing — this is correct: a unit without the comment line was
    # installed by an old install_watcher.sh, so any schema-aware image
    # should self-heal it.
    local current_schema=0
    local unit_candidates=(
        "/etc/systemd/system/zmm-upgrade.service"
        "/opt/.config/systemd/user/zmm-upgrade.service"
        "/etc/systemd/system/zmm-upgrade-poll.service"
    )
    local found_unit=""
    for unit in "${unit_candidates[@]}"; do
        if [[ -f "$unit" ]]; then
            found_unit="$unit"
            local v
            v=$(grep -oP '^# WATCHER_SCHEMA_VERSION=\K[0-9]+' "$unit" 2>/dev/null | head -1)
            if [[ -n "$v" ]]; then
                current_schema="$v"
            fi
            break
        fi
    done

    if [[ -z "$found_unit" ]]; then
        log "self_heal: no watcher unit found on disk — skipping (clean install state)"
        return 0
    fi

    # Stage a stopped container from the new image so we can copy out files.
    local stage_name="zmm-self-heal-$$-$RANDOM"
    if ! "$RUNTIME" create --name "$stage_name" "$new_tag" >/dev/null 2>&1; then
        log "self_heal: WARN failed to create staging container from $new_tag — skipping"
        return 0
    fi

    local stage_dir
    stage_dir=$(mktemp -d -t zmm-self-heal-XXXXXX) || {
        log "self_heal: WARN failed to mktemp — skipping"
        "$RUNTIME" rm -f "$stage_name" >/dev/null 2>&1 || true
        return 0
    }
    # shellcheck disable=SC2064
    trap "\"$RUNTIME\" rm -f $stage_name >/dev/null 2>&1 || true; rm -rf $stage_dir" RETURN

    # Copy the four helper files out of the staging container.
    local cp_failed=0
    "$RUNTIME" cp "$stage_name:/app/scripts/install_watcher.sh" "$stage_dir/install_watcher.sh" 2>/dev/null || cp_failed=1
    "$RUNTIME" cp "$stage_name:/app/scripts/upgrade.sh"         "$stage_dir/upgrade.sh"         2>/dev/null || cp_failed=1
    "$RUNTIME" cp "$stage_name:/app/scripts/run_container.sh"   "$stage_dir/run_container.sh"   2>/dev/null || cp_failed=1
    "$RUNTIME" cp "$stage_name:/app/build.sh"                   "$stage_dir/build.sh"           2>/dev/null || cp_failed=1

    if (( cp_failed )); then
        log "self_heal: WARN one or more files missing from $new_tag (image may pre-date schema mechanism) — skipping"
        return 0
    fi

    # Read the new image's schema version from the freshly-copied file.
    local new_schema
    new_schema=$(grep -oP '^WATCHER_SCHEMA_VERSION=\K[0-9]+' "$stage_dir/install_watcher.sh" 2>/dev/null | head -1)
    if [[ -z "$new_schema" ]]; then
        log "self_heal: new image $new_tag has no WATCHER_SCHEMA_VERSION constant — skipping"
        return 0
    fi

    log "self_heal: current=${current_schema}, new=${new_schema}, unit=${found_unit}"

    # STRICT forward-only comparison.
    if (( new_schema <= current_schema )); then
        log "self_heal: nothing to do (new schema is not greater than current)"
        return 0
    fi

    log "self_heal: schema bump detected (${current_schema} -> ${new_schema}) — refreshing helpers"

    # Place build.sh at $APP_DIR/build.sh (run_container.sh sources it from there).
    # Place install_watcher.sh, upgrade.sh, run_container.sh at $APP_DIR/scripts/
    # so install_watcher.sh's find_script() picks them up when we re-invoke it.
    mkdir -p "${APP_DIR}/scripts" 2>/dev/null || true
    install -m 755 "$stage_dir/build.sh"           "${APP_DIR}/build.sh"                     || { log "self_heal: WARN failed to install build.sh"; return 0; }
    install -m 755 "$stage_dir/install_watcher.sh" "${APP_DIR}/scripts/install_watcher.sh"   || { log "self_heal: WARN failed to install install_watcher.sh"; return 0; }
    install -m 755 "$stage_dir/upgrade.sh"         "${APP_DIR}/scripts/upgrade.sh"           || { log "self_heal: WARN failed to install upgrade.sh"; return 0; }
    install -m 755 "$stage_dir/run_container.sh"   "${APP_DIR}/scripts/run_container.sh"     || { log "self_heal: WARN failed to install run_container.sh"; return 0; }

    # Re-run install_watcher.sh from the freshly-installed location. It will
    # rewrite the systemd unit (including the new WATCHER_SCHEMA_VERSION
    # comment line), copy the scripts to $SCRIPTS_DIR, daemon-reload, and
    # restart the watcher.
    log "self_heal: invoking ${APP_DIR}/scripts/install_watcher.sh"
    if ZMM_DATA_DIR="$DATA_DIR" ZMM_APP_DIR="$APP_DIR" \
            bash "${APP_DIR}/scripts/install_watcher.sh" >>"$BUILD_LOG" 2>&1; then
        log "self_heal: helpers refreshed to schema ${new_schema}"
    else
        log "self_heal: WARN install_watcher.sh exited non-zero — see build.log"
    fi

    return 0
}

# ── SWAP: stop old container, rename, run new ────────────────────────────────
# Linear flow:
#   1. Mask + stop supervisor (so it can't auto-restart the old container)
#   2. Stop the old container, rename to -previous
#   3. Start new container
#   4. Health check
#   5. On failure → rollback. On success → unmask + start supervisor.
do_swap() {
    local target_version
    target_version=$(echo "$TRIGGER_PAYLOAD" | jq -r '.target_version // empty')
    if [[ -z "$target_version" ]]; then
        write_status "failed" "" 0 "Swap failed" "No target_version in swap payload"
        return 1
    fi

    local arch new_tag
    arch=$(detect_arch)
    new_tag="${IMAGE_NAME}:${target_version}-${arch}"

    if ! "$RUNTIME" image inspect "$new_tag" >/dev/null 2>&1; then
        write_status "failed" "$target_version" 0 "Swap failed" "Image $new_tag not found — build first"
        return 1
    fi

    if ! "$RUNTIME" inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
        write_status "failed" "$target_version" 0 "Swap failed" "Current container $CONTAINER_NAME not found"
        return 1
    fi

    log "Swap: starting for v$target_version"

    # Capture current state for rollback
    local current_image_tag current_version previous_name
    current_image_tag=$("$RUNTIME" inspect -f '{{.ImageName}}' "$CONTAINER_NAME" 2>/dev/null || echo "")
    if [[ -z "$current_image_tag" || "$current_image_tag" == "<nil>" ]]; then
        local current_image
        current_image=$("$RUNTIME" inspect -f '{{.Image}}' "$CONTAINER_NAME" 2>/dev/null)
        current_image_tag=$("$RUNTIME" image inspect --format '{{index .RepoTags 0}}' "$current_image" 2>/dev/null || echo "${IMAGE_NAME}:latest")
    fi
    current_version=$("$RUNTIME" exec "$CONTAINER_NAME" cat /app/VERSION 2>/dev/null | tr -d '[:space:]' || echo "unknown")
    previous_name="${CONTAINER_NAME}-previous"

    log "Swap: current image = $current_image_tag (version $current_version)"
    log "Swap: new image     = $new_tag (version $target_version)"

    # ── STEP 1: Mask the supervisor for the entire swap window ───────────────
    write_status "swapping" "$target_version" 20 "Disabling supervisor auto-restart"
    container_unit_mask_and_stop

    # ── STEP 2: Stop and rename the old container ────────────────────────────
    write_status "swapping" "$target_version" 35 "Stopping current container"
    log "Swap: stopping $CONTAINER_NAME (45s graceful)"
    if ! "$RUNTIME" stop -t 45 "$CONTAINER_NAME" >>"$WATCHER_LOG" 2>&1; then
        log "Swap: stop returned non-zero (continuing)"
    fi

    write_status "swapping" "$target_version" 45 "Renaming old container to -previous"
    "$RUNTIME" rm -f "$previous_name" >/dev/null 2>&1 || true
    if ! "$RUNTIME" rename "$CONTAINER_NAME" "$previous_name" >>"$WATCHER_LOG" 2>&1; then
        log "Swap: rename failed — removing old container instead"
        "$RUNTIME" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    fi

    # ── STEP 3: Start the new container ──────────────────────────────────────
    local run_helper
    if ! run_helper=$(find_run_helper); then
        log "Swap: run_container.sh not found in any known location"
        rollback_to_previous "run_container.sh helper not installed"
        return 1
    fi

    # The old container has stopped but the host ports may still be in TIME_WAIT
    # (or rootlessport may not have released them yet). Wait for them to be
    # bindable before starting the new container, otherwise it fails to bind
    # :8000/:5580 and we'd needlessly roll back a perfectly good image.
    write_status "swapping" "$target_version" 55 "Waiting for ports to free"
    if ! wait_for_ports_free 60; then
        log "Swap: ports still busy after wait — killing squatters before start"
        kill_port_squatters 8000 5580
    fi

    write_status "swapping" "$target_version" 60 "Starting new container"
    log "Swap: starting new container from $new_tag via $run_helper"

    log_to_build ""
    log_to_build "=== Starting new container ==="
    log_to_build "Image: $new_tag"
    log_to_build "Helper: $run_helper"
    log_to_build ""

    # Bound the start so a hung run_container.sh can't wedge the swap forever
    # (only if coreutils `timeout` exists; otherwise run unbounded).
    local run_timeout=""
    command -v timeout >/dev/null 2>&1 && run_timeout="timeout 180"

    if ! RUNTIME="$RUNTIME" \
         IMAGE_TAG="$new_tag" \
         CONTAINER_NAME="$CONTAINER_NAME" \
         DATA_DIR="$DATA_DIR" \
         $run_timeout bash "$run_helper" 2>&1 | tee -a "$BUILD_LOG" >>"$WATCHER_LOG"
    then
        log "Swap: new container failed to start (or timed out) — rolling back"

        log_to_build ""
        log_to_build "=== NEW CONTAINER FAILED TO START — capturing logs ==="
        "$RUNTIME" logs --tail=100 "$CONTAINER_NAME" >>"$BUILD_LOG" 2>&1 || \
            log_to_build "(no logs available — container did not exist or runtime returned error)"
        log_to_build ""
        log_to_build "=== Failed container inspect ==="
        "$RUNTIME" inspect "$CONTAINER_NAME" 2>>"$BUILD_LOG" | head -100 >>"$BUILD_LOG" || true

        rollback_to_previous "new container failed to start"
        return 1
    fi

    # ── STEP 4: Health check (stable + version-aware) ─────────────────────────
    write_status "swapping" "$target_version" 80 "Health-checking new container"

    local health_candidates=()
    while IFS= read -r url; do
        [[ -n "$url" ]] && health_candidates+=("$url")
    done < <(detect_health_urls)

    log "Swap: health-check candidates: ${health_candidates[*]}"
    log "Swap: waiting up to ${HEALTH_TIMEOUT}s for v${target_version} to become stable-healthy"

    if ! wait_until_healthy "$HEALTH_TIMEOUT" "$target_version" "${health_candidates[@]}"; then
        log "Swap: health check failed — rolling back"

        log_to_build ""
        log_to_build "=== HEALTH CHECK FAILED — capturing failed container logs ==="
        log_to_build "Tried URLs: ${health_candidates[*]}"
        "$RUNTIME" logs --tail=100 "$CONTAINER_NAME" >>"$BUILD_LOG" 2>&1 || \
            log_to_build "(no container logs available)"

        rollback_to_previous "new container did not become stable-healthy within ${HEALTH_TIMEOUT}s"
        return 1
    fi

    # ── STEP 5: Success ──────────────────────────────────────────────────────
    log "Swap: SUCCESS. New container healthy."
    update_version_state "$target_version" "$current_version" "$new_tag" "$current_image_tag"
    unmask_unit_if_needed
    container_unit_start

    # POD-SPECIFIC: the stopped -previous container is still a pod member, so a
    # reboot's `podman pod start ${POD_NAME}` would start it alongside the new app
    # and the two would fight for the host ports (the dual-container bug). Remove
    # it. Rollback re-runs the previous IMAGE instead — it's recorded in
    # version.json (previous_image_tag, set above), protected from GC by do_gc,
    # and restarted through the same pod-aware run_container.sh. Standalone keeps
    # the container as before.
    if "$RUNTIME" pod exists "${ZMM_POD_NAME:-zmm}" 2>/dev/null; then
        "$RUNTIME" rm -f "$previous_name" >/dev/null 2>&1 || true
        log "Swap: removed pod member $previous_name (rollback uses the retained previous image)"
    else
        log "Swap: keeping $previous_name for rollback (standalone)"
    fi

    # Self-heal host-side helpers if the new image bumped WATCHER_SCHEMA_VERSION.
    self_heal_helpers "$new_tag" || log "self_heal: returned non-zero (treated as non-fatal)"

    # Clean up the build workspace to save disk space, but preserve the orchestration scripts
    # that run_container.sh requires for future swaps and rollbacks.
    log "Swap: Cleaning up clone directory..."
    find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name 'build.sh' ! -name 'scripts' -exec rm -rf {} + 2>/dev/null || true

    # Prune stale images now that the new version is healthy. Keeps the running
    # and rollback (-previous) images; honours the configured retention. Runs in
    # "auto" mode so it never writes status or fails the (already-successful) swap.
    log "Swap: running image GC (retention-based + dangling)"
    do_gc auto || log "Swap: GC returned non-zero (non-fatal)"

    write_status "idle" "$target_version" 100 "Upgrade complete" ""
    return 0
}

# ── ROLLBACK: swap back to previous image ────────────────────────────────────
do_rollback() {
    local previous_image_tag
    previous_image_tag=$(echo "$TRIGGER_PAYLOAD" | jq -r '.previous_image_tag // empty')
    local previous_version
    previous_version=$(echo "$TRIGGER_PAYLOAD" | jq -r '.previous_version // empty')

    if [[ -z "$previous_image_tag" ]]; then
        # Fallback: look for -previous container
        if "$RUNTIME" inspect "${CONTAINER_NAME}-previous" >/dev/null 2>&1; then
            log "Rollback: using ${CONTAINER_NAME}-previous"
            write_status "rolling_back" "$previous_version" 20 "Stopping current container"
            container_unit_mask_and_stop
            "$RUNTIME" stop -t 15 "$CONTAINER_NAME" >/dev/null 2>&1 || true
            "$RUNTIME" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
            "$RUNTIME" rename "${CONTAINER_NAME}-previous" "$CONTAINER_NAME" >/dev/null 2>&1 || true
            write_status "rolling_back" "$previous_version" 60 "Starting previous container"
            "$RUNTIME" start "$CONTAINER_NAME" >>"$WATCHER_LOG" 2>&1
            unmask_unit_if_needed
            container_unit_start
            write_status "idle" "$previous_version" 100 "Rollback complete" ""
            return 0
        fi
        write_status "failed" "$previous_version" 0 "Rollback failed" "No previous image tag or container available"
        return 1
    fi

    if ! "$RUNTIME" image inspect "$previous_image_tag" >/dev/null 2>&1; then
        write_status "failed" "$previous_version" 0 "Rollback failed" "Previous image $previous_image_tag not found"
        return 1
    fi

    log "Rollback: swapping to $previous_image_tag"
    write_status "rolling_back" "$previous_version" 30 "Stopping current"
    container_unit_mask_and_stop

    local failed_name="${CONTAINER_NAME}-failed-$(date +%s)"
    "$RUNTIME" stop -t 15 "$CONTAINER_NAME" >/dev/null 2>&1 || true
    "$RUNTIME" rename "$CONTAINER_NAME" "$failed_name" >/dev/null 2>&1 || \
        "$RUNTIME" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

    local run_helper
    if ! run_helper=$(find_run_helper); then
        write_status "failed" "$previous_version" 50 "Rollback failed" "run_container.sh missing"
        unmask_unit_if_needed
        container_unit_start
        return 1
    fi

    write_status "rolling_back" "$previous_version" 60 "Starting previous image"
    RUNTIME="$RUNTIME" \
    IMAGE_TAG="$previous_image_tag" \
    CONTAINER_NAME="$CONTAINER_NAME" \
    DATA_DIR="$DATA_DIR" \
    bash "$run_helper" >>"$WATCHER_LOG" 2>&1

    # Clean up failed container
    "$RUNTIME" rm -f "$failed_name" >/dev/null 2>&1 || true

    update_version_state "$previous_version" "" "$previous_image_tag" ""
    unmask_unit_if_needed
    container_unit_start
    write_status "idle" "$previous_version" 100 "Rollback complete" ""
    return 0
}

# ── CANCEL: kill in-progress build ───────────────────────────────────────────
do_cancel() {
    log "Cancel requested"
    # Kill any running podman build for our image name
    pkill -f "$RUNTIME build.*$IMAGE_NAME" 2>/dev/null || true
    write_status "idle" "" 0 "Cancelled by user" ""
}

# ── GC: prune old images beyond retention + dangling layers ──────────────────
# Bugs this replaces:
#   - `grep "^${IMAGE_NAME}:"` never matched: podman prints repositories with a
#     registry prefix (localhost/zigbee-matter-manager), so the anchored match
#     found nothing and GC silently removed nothing.
#   - dangling <none> images (old build layers from each rebuild) were never
#     pruned, so they piled up at ~2GB each.
#   - the running and rollback (-previous) images weren't explicitly protected.
#
# `mode` = "auto" when called internally from do_swap: suppress status writes
# and never fail the caller.
do_gc() {
    local mode="${1:-manual}"

    # Retention: trigger payload first, then persisted state, default 2, min 1.
    local keep
    keep=$(echo "$TRIGGER_PAYLOAD" | jq -r '.retention_count // empty' 2>/dev/null || echo "")
    if [[ -z "$keep" || ! "$keep" =~ ^[0-9]+$ ]]; then
        keep=$(jq -r '.retention_count // empty' "$VERSION_STATE_FILE" 2>/dev/null || echo "")
    fi
    [[ "$keep" =~ ^[0-9]+$ ]] || keep=2
    (( keep < 1 )) && keep=1
    log "GC: keeping $keep most recent version image(s) (mode=$mode)"

    # Normalise any image id to a comparable 12-char form (strips sha256:).
    norm_id() { local x="${1#sha256:}"; echo "${x:0:12}"; }

    # ── Build the protected set: images we must never remove ─────────────────
    local protected=""
    local c iid
    for c in "$CONTAINER_NAME" "${CONTAINER_NAME}-previous"; do
        iid=$("$RUNTIME" inspect -f '{{.Image}}' "$c" 2>/dev/null || echo "")
        [[ -n "$iid" ]] && protected+="$(norm_id "$iid")"$'\n'
    done
    # The recorded rollback target tag, resolved to an id.
    local prev_tag prev_id
    prev_tag=$(jq -r '.previous_image_tag // empty' "$VERSION_STATE_FILE" 2>/dev/null || echo "")
    if [[ -n "$prev_tag" ]]; then
        prev_id=$("$RUNTIME" image inspect -f '{{.Id}}' "$prev_tag" 2>/dev/null || echo "")
        [[ -n "$prev_id" ]] && protected+="$(norm_id "$prev_id")"$'\n'
    fi

    is_protected() { grep -qxF "$(norm_id "$1")" <<< "$protected"; }

    # ── Version-tagged candidates, newest first ──────────────────────────────
    # Format: ID|REPO:TAG|CREATED_AT. Match the repo with an OPTIONAL registry
    # prefix (localhost/, registry.example.com/ns/, …). Exclude latest* tags.
    # Sort by the CreatedAt field (third '|' column) descending — robust to the
    # spaces inside the timestamp.
    local listing
    listing=$("$RUNTIME" images --format '{{.ID}}|{{.Repository}}:{{.Tag}}|{{.CreatedAt}}' 2>/dev/null \
        | grep -E "\|([^|]*/)?${IMAGE_NAME}:" \
        | grep -vE ":(latest|latest-[a-z0-9_]+)\|" \
        | sort -t'|' -k3 -r)

    local removed=0 kept=0 idx=0 id reftag created
    while IFS='|' read -r id reftag created; do
        [[ -z "$reftag" ]] && continue
        idx=$((idx + 1))
        if (( idx <= keep )); then
            kept=$((kept + 1))
            continue
        fi
        if is_protected "$id"; then
            log "GC: keeping in-use/rollback image $reftag"
            kept=$((kept + 1))
            continue
        fi
        log "GC: removing old version $reftag"
        if "$RUNTIME" rmi "$reftag" >>"$WATCHER_LOG" 2>&1; then
            removed=$((removed + 1))
        else
            log "GC: failed to remove $reftag (in use?)"
        fi
    done <<< "$listing"

    # ── Dangling <none> layers from past rebuilds ────────────────────────────
    local dangling d
    dangling=$("$RUNTIME" images --filter dangling=true --quiet 2>/dev/null | sort -u)
    for d in $dangling; do
        is_protected "$d" && continue
        log "GC: removing dangling image $d"
        if "$RUNTIME" rmi "$d" >>"$WATCHER_LOG" 2>&1; then
            removed=$((removed + 1))
        else
            log "GC: dangling $d in use — skipped"
        fi
    done

    log "GC: done — kept $kept, removed $removed image(s)"
    if [[ "$mode" != "auto" ]]; then
        write_status "idle" "" 0 "GC complete — removed ${removed} image(s)" ""
    fi
    return 0
}

# ── INSTALL WATCHER: drop marker so the app knows we're alive ────────────────
do_install_watcher() {
    touch "$WATCHER_MARKER"
    log "Watcher install marker written"
    write_status "idle" "" 0 "Watcher installed" ""
}

# ── VERSION STATE UPDATE ─────────────────────────────────────────────────────
update_version_state() {
    local new_version="$1"
    local old_version="$2"
    local new_image_tag="$3"
    local old_image_tag="$4"

    # Use jq to update the version.json file atomically
    if [[ ! -f "$VERSION_STATE_FILE" ]]; then
        echo '{}' > "$VERSION_STATE_FILE"
    fi

    local now
    now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    local updated
    updated=$(jq --arg cv "$new_version" \
                 --arg ct "$new_image_tag" \
                 --arg pv "$old_version" \
                 --arg pt "$old_image_tag" \
                 --arg at "$now" \
                 '.current_version = $cv
                  | .current_image_tag = $ct
                  | .previous_version = (if $pv == "" then .previous_version else $pv end)
                  | .previous_image_tag = (if $pt == "" then .previous_image_tag else $pt end)
                  | .installed_at = $at
                  | .upgrade_state = "idle"
                  | .upgrade_error = null
                  | .latest_available = null' \
                 "$VERSION_STATE_FILE")

    echo "$updated" > "$VERSION_STATE_FILE"
}

# ── MAIN DISPATCH ────────────────────────────────────────────────────────────
# Cleanup handler — runs on EXIT (any reason), and explicitly on SIGTERM.
# Without explicit signal traps, a SIGKILL'd process won't run cleanup, but
# SIGTERM (the polite signal systemd sends first) will.
cleanup_on_exit() {
    local exit_code=$?
    release_lock
    if (( exit_code != 0 )); then
        log "upgrade.sh exiting with code $exit_code"
    fi
    exit $exit_code
}

main() {
    detect_runtime || exit 1

    if ! consume_trigger; then
        # No trigger present
        exit 0
    fi

    if ! acquire_lock "$TRIGGER_ACTION"; then
        log "Trigger dropped: lock held"
        exit 2
    fi

    # Install traps AFTER acquiring the lock — we want cleanup even on signal
    trap cleanup_on_exit EXIT
    trap 'log "Received SIGTERM — cleaning up"; exit 130' TERM
    trap 'log "Received SIGINT — cleaning up";  exit 130' INT
    trap 'log "Received SIGHUP — cleaning up";  exit 130' HUP

    case "$TRIGGER_ACTION" in
        build)              do_build ;;
        swap)               do_swap ;;
        rollback)           do_rollback ;;
        cancel)             do_cancel ;;
        gc)                 do_gc ;;
        install_watcher)    do_install_watcher ;;
        *)
            log "Unknown action: $TRIGGER_ACTION"
            write_status "failed" "" 0 "" "Unknown action: $TRIGGER_ACTION"
            exit 3
            ;;
    esac
}

main "$@"