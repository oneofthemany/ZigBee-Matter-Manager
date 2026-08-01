#!/bin/bash
# =============================================================================
# ZMM Upgrade — Host-Side Orchestrator
# =============================================================================
set -u  # NOTE: not -e; we want to catch errors and report them cleanly.
set -o pipefail

# ── CONFIG ───────────────────────────────────────────────────────────────────
DATA_DIR="${ZMM_DATA_DIR:-/opt/.zigbee-matter-manager}"
APP_DIR="${ZMM_APP_DIR:-/opt/.zigbee-matter-manager/upgrade_build}"
IMAGE_NAME="${ZMM_IMAGE_NAME:-zigbee-matter-manager}"
CONTAINER_NAME="${ZMM_CONTAINER_NAME:-zigbee-matter-manager}"
REPO_URL="${ZMM_REPO_URL:-https://github.com/oneofthemany/ZigBee-Matter-Manager.git}"
HEALTH_TIMEOUT="${ZMM_HEALTH_TIMEOUT:-300}"
STABILITY_SOAK="${ZMM_STABILITY_SOAK:-180}"

HEALTH_URL="${ZMM_HEALTH_URL:-}"

# ── IPC paths (shared with container via volume mount) ───────────────────────
UPGRADE_DIR="${DATA_DIR}/data/upgrade"
TRIGGER_FILE="${UPGRADE_DIR}/trigger"
STATUS_FILE="${UPGRADE_DIR}/status.json"
BUILD_LOG="${UPGRADE_DIR}/build.log"
LOCK_FILE="${UPGRADE_DIR}/lock"
WATCHER_MARKER="${UPGRADE_DIR}/.watcher_installed"

STATE_DIR="${DATA_DIR}/data/state"
VERSION_STATE_FILE="${STATE_DIR}/version.json"

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

# ── RUNTIME DETECTION (root podman / docker) ─────────────────────────────────
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
acquire_lock() {
    if [[ -f "$LOCK_FILE" ]]; then
        local held_pid
        held_pid=$(awk '{print $1}' "$LOCK_FILE" 2>/dev/null || echo "")
        local held
        held=$(cat "$LOCK_FILE" 2>/dev/null || echo "unknown")

        if [[ -n "$held_pid" ]] && kill -0 "$held_pid" 2>/dev/null; then
            log "Lock held by live PID $held_pid: $held"
            return 1
        fi

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
consume_trigger() {
    if [[ ! -f "$TRIGGER_FILE" ]]; then
        return 1
    fi

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
detect_health_urls() {
    local config="${DATA_DIR}/config/config.yaml"
    local port="8000"

    if [[ -n "${HEALTH_URL:-}" ]]; then
        echo "$HEALTH_URL"
        return 0
    fi

    if [[ -f "$config" ]]; then
        local p
        p=$(awk '
            /^web:/         { in_web=1; next }
            /^[a-zA-Z]/     { in_web=0 }
            in_web && /^  port:/ { gsub(/[^0-9]/,"",$2); print $2; exit }
        ' "$config" 2>/dev/null)
        [[ -n "$p" && "$p" =~ ^[0-9]+$ ]] && port="$p"
    fi

    echo "https://127.0.0.1:${port}/api/system/health"
    echo "http://127.0.0.1:${port}/api/system/health"
}

# ── SSL CERT ASSURANCE ──────────────────────────────────────────────────────
ensure_ssl_cert() {
    local cert_dir="${DATA_DIR}/data/certs"
    local cert="${cert_dir}/cert.pem"
    local key="${cert_dir}/key.pem"

    if [[ -f "$cert" && -f "$key" ]]; then
        return 0
    fi

    mkdir -p "$cert_dir" 2>/dev/null || true

    if ! command -v openssl >/dev/null 2>&1; then
        log "Cert: openssl not on host — app will self-generate on first boot"
        return 0
    fi

    local host_name lan_ip san
    host_name="$(hostname 2>/dev/null || true)"
    host_name="${host_name:-zigbee-manager}"
    lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    san="subjectAltName=DNS:localhost,DNS:${host_name},IP:127.0.0.1"
    [[ -n "$lan_ip" ]] && san+=",IP:${lan_ip}"

    log "Cert: none found — generating self-signed pair at ${cert_dir}"
    if openssl req -x509 -newkey rsa:2048 \
            -keyout "$key" -out "$cert" \
            -days 3650 -nodes \
            -subj "/CN=${host_name}" \
            -addext "$san" >/dev/null 2>&1; then
        chmod 600 "$key"
        log "Cert: self-signed pair written (valid 10 years)"
    else
        rm -f "$cert" "$key"
        log "Cert: openssl generation failed — app will self-generate on first boot"
    fi
}

is_app_healthy() {
    local urls=("$@")
    for url in "${urls[@]}"; do
        if curl -fsS -k --max-time 3 "$url" >/dev/null 2>&1; then
            echo "$url"
            return 0
        fi
    done
    return 1
}

# ── HELPER LOCATION ─────────────────────────────────────────────────────────
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
wait_for_ports_free() {
    local timeout="${1:-90}"
    local elapsed=0
    local sleep_interval=2
    local ports=("8000" "5580")
    local stable_required=2  # require N consecutive checks to pass before declaring free
    local stable_count=0

    sleep 1

    while (( elapsed < timeout )); do
        local all_free=1

        for port in "${ports[@]}"; do
            if command -v ss >/dev/null 2>&1; then
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

        if (( all_free == 1 )); then
            for port in "${ports[@]}"; do
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
wait_until_healthy() {
    local timeout="$1"; shift
    local expect_version="$1"; shift
    local urls=("$@")
    local elapsed=0 stable=0 working=""
    local need=2  # consecutive passes required

    [[ ${#urls[@]} -eq 0 ]] && return 1

    while (( elapsed < timeout )); do
        if working=$(is_app_healthy "${urls[@]}"); then
            local body
            body=$(curl -fsS -k --max-time 3 "$working" 2>/dev/null || true)
            if [[ -n "$expect_version" ]]; then
                local got
                got=$(printf '%s' "$body" \
                      | grep -oE '"version"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 \
                      | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')
                if [[ -n "$got" && "$got" != "$expect_version" ]]; then
                    log "Health: responding but version=$got (want $expect_version) — not counting"
                    stable=0
                    sleep 3; elapsed=$((elapsed + 3)); continue
                fi
            fi
            local bring
            bring=$(printf '%s' "$body" \
                    | grep -oE '"bringup"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 \
                    | sed -E 's/.*"bringup"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')
            if [[ -n "$bring" && "$bring" != "ready" ]]; then
                log "Health: responding but bringup=$bring — not counting"
                stable=0
                sleep 3; elapsed=$((elapsed + 3)); continue
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

soak_until_stable() {
    local soak="$1"; shift
    local expect_version="$1"; shift
    local urls=("$@")
    local elapsed=0 working="" failures=0
    local tolerate=1   # one transient blip is not a failed upgrade

    log "Soak: watching v${expect_version} for ${soak}s before accepting the swap"
    while (( elapsed < soak )); do
        sleep 5
        elapsed=$((elapsed + 5))

        if ! working=$(is_app_healthy "${urls[@]}"); then
            failures=$((failures + 1))
            log "Soak: no healthy response at ${elapsed}s (${failures}/$((tolerate + 1)))"
            if (( failures > tolerate )); then
                log "Soak: app stopped answering — treating the swap as failed"
                return 1
            fi
            continue
        fi

        local body bring
        body=$(curl -fsS -k --max-time 3 "$working" 2>/dev/null || true)
        bring=$(printf '%s' "$body" \
                | grep -oE '"bringup"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 \
                | sed -E 's/.*"bringup"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')
        if [[ -n "$bring" && "$bring" != "ready" ]]; then
            log "Soak: bringup=${bring} after already being ready — the app restarted"
            return 1
        fi
        failures=0
    done
    log "Soak: v${expect_version} stayed healthy for ${soak}s — accepting the swap"
    return 0
}

# ── HARDENED ROLLBACK ────────────────────────────────────────────────────────
rollback_to_previous() {
    local reason="${1:-unknown failure}"
    log "Rollback: $reason — restoring $previous_name"
    write_status "rolling_back" "$target_version" 85 "Rolling back: $reason"

    "$RUNTIME" stop -t 10 "$CONTAINER_NAME" >/dev/null 2>&1 || true
    "$RUNTIME" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

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

    local health_candidates=()
    while IFS= read -r url; do [[ -n "$url" ]] && health_candidates+=("$url"); done < <(detect_health_urls)

    if wait_until_healthy 45 "" "${health_candidates[@]}"; then
        log "Rollback: previous container restored and verified healthy"
        write_status "failed" "$target_version" 100 "Rolled back" \
            "Upgrade failed ($reason); previous version restored and verified healthy. See build.log."
        return 1
    fi

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

    : > "$BUILD_LOG"
    log_to_build "=== ZMM Upgrade Build ==="
    log_to_build "Target version: $target_version"
    log_to_build "Architecture:   $arch"
    log_to_build "Target tag:     $new_tag"
    log_to_build "Runtime:        $RUNTIME"
    log_to_build ""

    write_status "building" "$target_version" 5 "Preparing" "" "$started_at"

    # ── Sanity check: is APP_DIR in the expected state? ──────────────────────
    if [[ -d "$APP_DIR" ]]; then
        local existing_version="(no VERSION file)"
        [[ -f "$APP_DIR/VERSION" ]] && existing_version=$(tr -d '[:space:]' < "$APP_DIR/VERSION")
        log_to_build "APP_DIR exists at $APP_DIR (VERSION=${existing_version}) — will be wiped and re-cloned"

        local running_version
        local exec_timeout=""
        command -v timeout >/dev/null 2>&1 && exec_timeout="timeout 10"
        running_version=$($exec_timeout "$RUNTIME" exec "$CONTAINER_NAME" cat /app/VERSION 2>/dev/null | tr -d '[:space:]' || echo "")
        if [[ -n "$running_version" && "$existing_version" != "$running_version" && "$existing_version" != "(no VERSION file)" ]]; then
            log_to_build "WARN: APP_DIR VERSION (${existing_version}) does not match running container VERSION (${running_version})"
        fi
    else
        log_to_build "APP_DIR does not exist at $APP_DIR — fresh clone"
    fi

    local work_dir="$APP_DIR"
    rm -rf "$work_dir"
    mkdir -p "$work_dir"

    log_to_build "Cloning $REPO_URL at tag v${target_version}..."
    write_status "building" "$target_version" 10 "Cloning repository" "" "$started_at"

    if ! git clone --depth 1 --branch "v${target_version}" "$REPO_URL" "$work_dir" >>"$BUILD_LOG" 2>&1; then
        log_to_build "ERROR: git clone failed for tag v${target_version}"
        rm -rf "$work_dir"
        mkdir -p "$work_dir"
        if ! git clone --depth 1 --branch "${target_version}" "$REPO_URL" "$work_dir" >>"$BUILD_LOG" 2>&1; then
            log_to_build "ERROR: git clone failed for tag ${target_version} as well"
            write_status "failed" "$target_version" 10 "Clone failed" "git clone failed for tag v${target_version}" "$started_at"
            return 1
        fi
    fi

    echo "$target_version" > "$work_dir/VERSION"

    # ── requirements drift check ─────────────────────────────────────────────
    if [[ -f "$work_dir/requirements.txt" && -f "$work_dir/requirements.lock" ]]; then
        local drift=""
        local pkg pkg_re
        while IFS= read -r pkg; do
            [[ -z "$pkg" ]] && continue
            pkg_re=$(printf '%s' "$pkg" | sed 's/[-_]/[-_]/g')
            grep -qiE "^${pkg_re}==" "$work_dir/requirements.lock" || drift="${drift} ${pkg}"
        done < <(grep -vE '^[[:space:]]*#|^[[:space:]]*$' "$work_dir/requirements.txt" \
                 | sed -E 's/\[[^]]*\]//; s/[<>=!~;].*//; s/[[:space:]\r]+//g' | sort -u)
        if [[ -n "$drift" ]]; then
            log_to_build "WARN: requirements.lock does not pin:${drift}"
            log_to_build "WARN: the build will top-up these from requirements.txt at latest versions — regenerate requirements.lock for reproducible builds"
        fi
    fi

    # ── Appender choice: read the persisted marker from previous install ─────
    local appender_marker="${DATA_DIR}/data/state/appender.enabled"
    local with_appender="false"
    if [[ -f "$appender_marker" ]]; then
        with_appender=$(tr -d '[:space:]' < "$appender_marker")
        [[ "$with_appender" == "true" ]] || with_appender="false"
        log_to_build "Appender marker found: ${appender_marker} = ${with_appender}"
    else
        log_to_build "Appender marker missing — defaulting WITH_APPENDER=false (matches build.sh default)"
    fi

    # ── EQ choice: separate marker (zmm_eq is an independent crate). ─────────
    local eq_marker="${DATA_DIR}/data/state/eq.enabled"
    local with_eq="$with_appender"
    if [[ -f "$eq_marker" ]]; then
        with_eq=$(tr -d '[:space:]' < "$eq_marker")
        [[ "$with_eq" == "true" ]] || with_eq="false"
        log_to_build "EQ marker found: ${eq_marker} = ${with_eq}"
    else
        log_to_build "EQ marker missing — inheriting WITH_EQ=${with_eq} from appender marker (legacy combined build)"
    fi

    if [[ ! -f "$work_dir/Containerfile" ]]; then
        if [[ -f "$work_dir/build.sh" ]]; then
            log_to_build "Generating Containerfile via target tag's build.sh write_containerfile() (WITH_APPENDER=${with_appender}, WITH_EQ=${with_eq})"
            (
                set +u
                source "$work_dir/build.sh" >/dev/null 2>&1 || true
                if type write_containerfile >/dev/null 2>&1; then
                    CLONE_DIR="$work_dir" APP_DIR="$work_dir" \
                    WITH_APPENDER="$with_appender" \
                    WITH_EQ="$with_eq" \
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

    rm -f "$CANCEL_MARKER"
    local pull_args=()
    [[ "$RUNTIME" == "podman" ]] && pull_args=(--pull=missing)
    "$RUNTIME" build \
            "${pull_args[@]}" \
            --format docker \
            --build-arg BUILD_JOBS="$build_jobs" \
            --tag "$new_tag" \
            --file "$work_dir/Containerfile" \
            "$work_dir" >>"$BUILD_LOG" 2>&1 &
    local build_pid=$!
    local cancelled=0
    while kill -0 "$build_pid" 2>/dev/null; do
        if [[ -f "$CANCEL_MARKER" ]]; then
            cancelled=1
        elif [[ -f "$TRIGGER_FILE" ]] && [[ "$(jq -r '.action // empty' \
                "$TRIGGER_FILE" 2>/dev/null)" == "cancel" ]]; then
            rm -f "$TRIGGER_FILE"
            cancelled=1
        fi
        if (( cancelled )); then
            log_to_build ""
            log_to_build "Cancel requested — stopping build."
            kill_build_tree "$build_pid"
            cleanup_build_containers
            break
        fi
        sleep 2
    done
    local build_rc=0
    wait "$build_pid" 2>/dev/null || build_rc=$?
    if [[ -f "$CANCEL_MARKER" ]]; then
        cancelled=1
    fi
    rm -f "$CANCEL_MARKER"
    if (( cancelled )); then
        log_to_build "Build cancelled by user."
        write_status "idle" "" 0 "Cancelled by user" ""
        return 1
    fi
    if (( build_rc != 0 )); then
        log_to_build ""
        log_to_build "ERROR: Image build failed. See log above."
        write_status "failed" "$target_version" 50 "Build failed" "$RUNTIME build returned non-zero; see build.log" "$started_at"
        return 1
    fi

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
    if "$RUNTIME" pod exists "${ZMM_POD_NAME:-zmm}" 2>/dev/null; then
        return 1
    fi
    if [[ -n "$ZMM_CONTAINER_UNIT" ]]; then
        echo "$ZMM_CONTAINER_UNIT"
        return 0
    fi
    local candidates=(
        "container-${CONTAINER_NAME}.service"
        "${CONTAINER_NAME}.service"
    )
    for unit in "${candidates[@]}"; do
        if systemctl --system cat "$unit" >/dev/null 2>&1; then
            echo "--system $unit"
            return 0
        fi
    done
    return 1
}

container_unit_mask_and_stop() {
    local unit_desc; unit_desc=$(detect_container_unit) || {
        log "Supervisor: no unit detected (continuing without override)"
        return 0
    }
    local scope unit
    read -r scope unit <<< "$unit_desc"
    log "Supervisor: disabling auto-restart on $scope $unit (runtime drop-in)"

    local override_dir="/run/systemd/system/${unit}.d"
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

container_unit_start() {
    local unit_desc; unit_desc=$(detect_container_unit) || return 0
    local scope unit
    read -r scope unit <<< "$unit_desc"
    log "Supervisor: starting $scope $unit"
    systemctl "$scope" start "$unit" >>"$WATCHER_LOG" 2>&1 || \
        log "Supervisor: warn — start failed for $scope $unit"
}

# ── HELPER SELF-HEAL ─────────────────────────────────────────────────────────
self_heal_helpers() {
    local new_tag="$1"

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
    trap "\"$RUNTIME\" rm -f $stage_name >/dev/null 2>&1 || true; rm -rf $stage_dir" RETURN

    local cp_failed=0
    "$RUNTIME" cp "$stage_name:/app/scripts/install_watcher.sh" "$stage_dir/install_watcher.sh" 2>/dev/null || cp_failed=1
    "$RUNTIME" cp "$stage_name:/app/scripts/upgrade.sh"         "$stage_dir/upgrade.sh"         2>/dev/null || cp_failed=1
    "$RUNTIME" cp "$stage_name:/app/scripts/run_container.sh"   "$stage_dir/run_container.sh"   2>/dev/null || cp_failed=1
    "$RUNTIME" cp "$stage_name:/app/build.sh"                   "$stage_dir/build.sh"           2>/dev/null || cp_failed=1

    if (( cp_failed )); then
        log "self_heal: WARN one or more files missing from $new_tag (image may pre-date schema mechanism) — skipping"
        return 0
    fi

    local new_schema
    new_schema=$(grep -oP '^WATCHER_SCHEMA_VERSION=\K[0-9]+' "$stage_dir/install_watcher.sh" 2>/dev/null | head -1)
    if [[ -z "$new_schema" ]]; then
        log "self_heal: new image $new_tag has no WATCHER_SCHEMA_VERSION constant — skipping"
        return 0
    fi

    log "self_heal: current=${current_schema}, new=${new_schema}, unit=${found_unit}"

    if (( new_schema <= current_schema )); then
        log "self_heal: nothing to do (new schema is not greater than current)"
        return 0
    fi

    log "self_heal: schema bump detected (${current_schema} -> ${new_schema}) — refreshing helpers"

    mkdir -p "${APP_DIR}/scripts" 2>/dev/null || true
    install -m 755 "$stage_dir/build.sh"           "${APP_DIR}/build.sh"                     || { log "self_heal: WARN failed to install build.sh"; return 0; }
    install -m 755 "$stage_dir/install_watcher.sh" "${APP_DIR}/scripts/install_watcher.sh"   || { log "self_heal: WARN failed to install install_watcher.sh"; return 0; }
    install -m 755 "$stage_dir/upgrade.sh"         "${APP_DIR}/scripts/upgrade.sh"           || { log "self_heal: WARN failed to install upgrade.sh"; return 0; }
    install -m 755 "$stage_dir/run_container.sh"   "${APP_DIR}/scripts/run_container.sh"     || { log "self_heal: WARN failed to install run_container.sh"; return 0; }

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

    write_status "swapping" "$target_version" 55 "Waiting for ports to free"
    if ! wait_for_ports_free 60; then
        log "Swap: ports still busy after wait — killing squatters before start"
        kill_port_squatters 8000 5580
    fi

    ensure_ssl_cert

    write_status "swapping" "$target_version" 60 "Starting new container"
    log "Swap: starting new container from $new_tag via $run_helper"

    log_to_build ""
    log_to_build "=== Starting new container ==="
    log_to_build "Image: $new_tag"
    log_to_build "Helper: $run_helper"
    log_to_build ""

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

    # ── STEP 4b: Stability soak ──────────────────────────────────────────────
    if ! soak_until_stable "$STABILITY_SOAK" "$target_version" "${health_candidates[@]}"; then
        log "Swap: new version did not stay healthy — rolling back"

        log_to_build ""
        log_to_build "=== STABILITY SOAK FAILED — capturing container logs ==="
        "$RUNTIME" logs --tail=200 "$CONTAINER_NAME" >>"$BUILD_LOG" 2>&1 || \
            log_to_build "(no container logs available)"

        rollback_to_previous "new container became healthy but did not stay healthy for ${STABILITY_SOAK}s"
        return 1
    fi

    # ── STEP 5: Success ──────────────────────────────────────────────────────
    log "Swap: SUCCESS. New container healthy and stable."
    update_version_state "$target_version" "$current_version" "$new_tag" "$current_image_tag"
    unmask_unit_if_needed
    container_unit_start

    if "$RUNTIME" pod exists "${ZMM_POD_NAME:-zmm}" 2>/dev/null; then
        "$RUNTIME" rm -f "$previous_name" >/dev/null 2>&1 || true
        log "Swap: removed pod member $previous_name (rollback uses the retained previous image)"
    else
        log "Swap: keeping $previous_name for rollback (standalone)"
    fi

    self_heal_helpers "$new_tag" || log "self_heal: returned non-zero (treated as non-fatal)"

    if "$RUNTIME" pod exists "${ZMM_POD_NAME:-zmm}" 2>/dev/null \
       && "$RUNTIME" inspect "${CONTAINER_NAME}-manager" >/dev/null 2>&1; then
        _bsh=""
        for _c in "${APP_DIR}/build.sh" "${DATA_DIR}/scripts/build.sh"; do
            [[ -f "$_c" ]] && { _bsh="$_c"; break; }
        done
        if [[ -n "$_bsh" ]]; then
            log "Swap: refreshing manager sidecar to the new image"
            ZMM_DATA_DIR="$DATA_DIR" RUNTIME="$RUNTIME" bash "$_bsh" --refresh-manager >>"$WATCHER_LOG" 2>&1 \
                || log "Swap: manager sidecar refresh failed (non-fatal)"
        fi
    fi

    log "Swap: Cleaning up clone directory..."
    find "$APP_DIR" -mindepth 1 -maxdepth 1 ! -name 'build.sh' ! -name 'scripts' -exec rm -rf {} + 2>/dev/null || true

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

    "$RUNTIME" rm -f "$failed_name" >/dev/null 2>&1 || true

    update_version_state "$previous_version" "" "$previous_image_tag" ""
    unmask_unit_if_needed
    container_unit_start
    write_status "idle" "$previous_version" 100 "Rollback complete" ""
    return 0
}

# ── CANCEL: kill in-progress build ───────────────────────────────────────────
CANCEL_MARKER="${UPGRADE_DIR}/.cancel_requested"

list_descendants() {
    local kids k
    kids=$(pgrep -P "$1" 2>/dev/null || true)
    for k in $kids; do
        echo "$k"
        list_descendants "$k"
    done
}

kill_build_tree() {
    local all="" p
    for p in $*; do
        all+="$p $(list_descendants "$p" | tr '\n' ' ') "
    done
    [[ -z "${all// }" ]] && return 0
    log "Killing build process tree: $all"
    kill -TERM $all 2>/dev/null || true
    sleep 3
    kill -KILL $all 2>/dev/null || true
}

cleanup_build_containers() {
    "$RUNTIME" ps -a --external --format '{{.ID}} {{.Names}}' 2>/dev/null \
        | awk '/working-container|buildah/ {print $1}' \
        | xargs -r "$RUNTIME" rm --force >/dev/null 2>&1 || true
}

do_cancel() {
    log "Cancel requested"
    local pids
    pids=$(pgrep -f "$RUNTIME build.*$IMAGE_NAME" 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        touch "$CANCEL_MARKER"
        kill_build_tree $pids
        cleanup_build_containers
    fi
    write_status "idle" "" 0 "Cancelled by user" ""
}

# ── GC: prune old images beyond retention + dangling layers ──────────────────
do_gc() {
    local mode="${1:-manual}"

    local keep
    keep=$(echo "$TRIGGER_PAYLOAD" | jq -r '.retention_count // empty' 2>/dev/null || echo "")
    if [[ -z "$keep" || ! "$keep" =~ ^[0-9]+$ ]]; then
        keep=$(jq -r '.retention_count // empty' "$VERSION_STATE_FILE" 2>/dev/null || echo "")
    fi
    [[ "$keep" =~ ^[0-9]+$ ]] || keep=2
    (( keep < 1 )) && keep=1
    log "GC: keeping $keep most recent version image(s) (mode=$mode)"

    norm_id() { local x="${1#sha256:}"; echo "${x:0:12}"; }

    # ── Build the protected set: images we must never remove ─────────────────
    local protected=""
    local c iid
    for c in "$CONTAINER_NAME" "${CONTAINER_NAME}-previous"; do
        iid=$("$RUNTIME" inspect -f '{{.Image}}' "$c" 2>/dev/null || echo "")
        [[ -n "$iid" ]] && protected+="$(norm_id "$iid")"$'\n'
    done
    local prev_tag prev_id
    prev_tag=$(jq -r '.previous_image_tag // empty' "$VERSION_STATE_FILE" 2>/dev/null || echo "")
    if [[ -n "$prev_tag" ]]; then
        prev_id=$("$RUNTIME" image inspect -f '{{.Id}}' "$prev_tag" 2>/dev/null || echo "")
        [[ -n "$prev_id" ]] && protected+="$(norm_id "$prev_id")"$'\n'
    fi

    is_protected() { grep -qxF "$(norm_id "$1")" <<< "$protected"; }

    # ── Version-tagged candidates, newest first ──────────────────────────────
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
        exit 0
    fi

    if [[ "$TRIGGER_ACTION" == "cancel" ]]; then
        do_cancel
        exit 0
    fi

    if ! acquire_lock "$TRIGGER_ACTION"; then
        log "Trigger dropped: lock held"
        exit 2
    fi

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