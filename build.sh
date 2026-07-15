#!/bin/bash
# =============================================================================
# Zigbee Matter Manager — Container Build & Deploy Script
# Supports: Podman (preferred) and Docker
#
# Runs as a privileged container (required for OTBR network namespaces,
# ipset, iptables, and Thread border routing).
# Uses --network=host for direct Thread/mDNS/IPv6 access.
# =============================================================================

# =============================================================================
# OTA HOTFIX: Diff Check & Live Patching
# Compare newly cloned scripts against running scripts. If a diff exists,
# invoke install_watcher to update the host orchestrator immediately.
# =============================================================================
if [[ "${BASH_SOURCE[0]:-$0}" != "${0}" ]] && [[ -n "${ZMM_DATA_DIR:-}" || -n "${DATA_DIR:-}" ]]; then
    _SAFE_DIR="${ZMM_DATA_DIR:-${DATA_DIR:-/opt/.zigbee-matter-manager}}/scripts"
    _SRC_DIR="$(dirname "${BASH_SOURCE[0]}")"
    _NEEDS_UPDATE=0

    if [[ -d "$_SAFE_DIR" && -d "$_SRC_DIR/scripts" ]]; then
        # Check standard scripts
        for _script in upgrade.sh run_container.sh install_watcher.sh; do
            if ! cmp -s "$_SRC_DIR/scripts/$_script" "$_SAFE_DIR/$_script" 2>/dev/null; then
                _NEEDS_UPDATE=1
                break
            fi
        done
        # Check build.sh
        if [[ -f "$_SRC_DIR/build.sh" ]] && ! cmp -s "$_SRC_DIR/build.sh" "$_SAFE_DIR/build.sh" 2>/dev/null; then
            _NEEDS_UPDATE=1
        fi

        if [[ "$_NEEDS_UPDATE" -eq 1 ]]; then
            echo -e "\033[0;36m\033[1m[INFO]\033[0m  Scripts diff detected. Running install_watcher.sh to patch host orchestrator..."
            ZMM_DATA_DIR="${ZMM_DATA_DIR:-${DATA_DIR}}" ZMM_APP_DIR="$_SRC_DIR" bash "$_SRC_DIR/scripts/install_watcher.sh" >/dev/null 2>&1 || true
        fi
    fi
fi
# =============================================================================

# This installer runs ENTIRELY as root. Rootful podman is required: the Zigbee
# USB coordinator, OTBR network namespaces, ipset/iptables and the host systemd
# units all need root, and a rootless pod created here would be invisible to the
# root-owned boot units. ensure_root (called from main) re-execs under sudo when
# needed. No user-session XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS is set —
# those are rootless-podman artifacts and do not apply here.

set -euo pipefail

# ── Build host capability detection ──
detect_build_jobs() {
    local cores
    if command -v nproc >/dev/null 2>&1; then
        cores=$(nproc)
    elif [[ -r /proc/cpuinfo ]]; then
        cores=$(grep -c ^processor /proc/cpuinfo)
    else
        cores=2
    fi
    # Cap at 8 — diminishing returns past that, and DuckDB's compile
    # link step occasionally OOMs on -j16+ with only a few GB free.
    (( cores > 8 )) && cores=8
    echo "$cores"
}

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}${BOLD}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}${BOLD}[ OK ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}${BOLD}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}${BOLD}[ERR ]${NC}  $*" >&2; }
die()     { error "$*"; exit 1; }

# ── Progress reporting ──────────────────────────────────────────────────────
# Progress is reported in two layers:
#   1. step_announce() — high-level phases ("Step 3 of 10: USB coordinator")
#   2. build_progress_filter() — parses podman's STEP N/M output and renders
#      an in-place progress bar. Falls back to plain pass-through if stdout
#      isn't a TTY (e.g. when build.sh's output is being captured to a file).
TOTAL_STEPS=10
CURRENT_STEP=0

step_announce() {
    CURRENT_STEP=$((CURRENT_STEP + 1))
    local desc="$1"
    echo
    echo -e "${BOLD}${CYAN}▸ Step ${CURRENT_STEP} of ${TOTAL_STEPS}: ${desc}${NC}"
}

# Render a progress bar to stderr. Caller passes current/total/description.
# Uses \r and \033[K to redraw in place.
_render_bar() {
    local current="$1"
    local total="$2"
    local cached="$3"
    local desc="$4"
    local bar_width=30
    local term_width
    term_width=$(tput cols 2>/dev/null || echo 80)

    local percent=0
    (( total > 0 )) && percent=$(( current * 100 / total ))
    local filled=$(( current * bar_width / (total > 0 ? total : 1) ))

    local bar=""
    local i
    for ((i=0; i<bar_width; i++)); do
        if (( i < filled )); then bar+="█"; else bar+="░"; fi
    done

    # Truncate desc to fit
    local prefix_len=$((bar_width + 25))   # bar + percentages + spacing
    local max_desc=$(( term_width - prefix_len ))
    (( max_desc < 10 )) && max_desc=10
    if (( ${#desc} > max_desc )); then
        desc="${desc:0:$((max_desc - 3))}..."
    fi

    # Cached count appended only if we've seen any
    local cached_suffix=""
    (( cached > 0 )) && cached_suffix=" (${cached} cached)"

    # \r = cursor to column 0; \033[K = clear from cursor to EOL
    printf "\r\033[K  [%s] %3d%% (%2d/%-2d)%s %s" \
        "$bar" "$percent" "$current" "$total" "$cached_suffix" "$desc" >&2
}

# Filter podman build output: parse STEP N/M lines, render bar, save full
# log for debugging on failure. Pass-through when not a TTY.
build_progress_filter() {
    local log_file="$1"
    : > "$log_file"

    if [[ ! -t 2 ]]; then
        # Not a TTY (output redirected) — just tee to log and pass through
        tee -a "$log_file"
        return
    fi

    local current=0
    local total=0
    local cached=0
    local last_op=""

    while IFS= read -r line; do
        # Always log everything
        printf '%s\n' "$line" >> "$log_file"

        if [[ "$line" =~ ^STEP\ ([0-9]+)/([0-9]+):\ (.*)$ ]]; then
            current="${BASH_REMATCH[1]}"
            total="${BASH_REMATCH[2]}"
            last_op="${BASH_REMATCH[3]}"
            # Strip leading verbs we don't need to show
            last_op="${last_op#RUN }"
            last_op="${last_op#COPY }"
            last_op="${last_op#FROM }"
            last_op="${last_op#ENV }"
            _render_bar "$current" "$total" "$cached" "$last_op"
        elif [[ "$line" == *"Using cache"* ]]; then
            cached=$((cached + 1))
            _render_bar "$current" "$total" "$cached" "$last_op"
        elif [[ "$line" == "Successfully tagged"* ]] || [[ "$line" == COMMIT* ]]; then
            _render_bar "$total" "$total" "$cached" "finalising"
        elif [[ "$line" =~ ^Error|^ERROR ]]; then
            # Drop to a fresh line so the error is readable
            printf "\n" >&2
            printf '%s\n' "$line" >&2
        fi
    done

    # Final newline so subsequent output isn't on the bar's line
    printf "\n" >&2
}

# ── Defaults ─────────────────────────────────────────────────────────────────
REPO_URL="https://github.com/oneofthemany/ZigBee-Matter-Manager.git"
REPO_BRANCH="main"
DATA_DIR="${ZMM_DATA_DIR:-/opt/.zigbee-matter-manager}"
APP_DIR="${ZMM_APP_DIR:-${DATA_DIR}/upgrade_build}"
CLONE_DIR="${ZMM_CLONE_DIR:-${APP_DIR}}"
IMAGE_NAME="zigbee-matter-manager"
CONTAINER_NAME="zigbee-matter-manager"
INTERNAL_PORT=8000
MATTER_INTERNAL_PORT=5580

# Pod (podman only). The app — and, from CP2, the manager sidecar — run as members
# of this pod. The pod's infra container owns the published ports for the whole
# pod lifetime, so members join with --pod and never publish ports themselves.
POD_NAME="${ZMM_POD_NAME:-zmm}"
MANAGER_PORT="${ZMM_MANAGER_PORT:-8001}"
MANAGER_CONTAINER_NAME="${CONTAINER_NAME}-manager"   # sidecar; runs the app image as `python -m manager`

# =============================================================================
# PRE-FLIGHT: require root, then ensure the data directory exists
# =============================================================================
# The whole installer must run as root (rootful podman + host units). If it
# isn't, re-exec under sudo when we're a real script file; when piped straight
# from curl there is no file to re-exec, so tell the operator how to fix it.
ensure_root() {
    [[ "$(id -u)" -eq 0 ]] && return 0
    local self="${BASH_SOURCE[0]:-$0}"
    if [[ -f "$self" ]] && command -v sudo &>/dev/null; then
        info "Not root — re-running under sudo (rootful podman is required for USB access)..."
        exec sudo -E bash "$self" "$@"
    fi
    error "This installer must run as root (rootful podman is required for USB access)."
    error "Re-run it as root, e.g.:"
    error "  curl -fsSL <installer-url> | sudo bash -s -- ${*}"
    exit 1
}

# DATA_DIR defaults to /opt/.zigbee-matter-manager — a root-owned location.
# We're root by the time this runs (ensure_root), so a plain mkdir is enough,
# and the tree stays root-owned to match rootful podman.
ensure_data_dir() {
    if [[ -d "$DATA_DIR" ]]; then
        ok "Data directory present: ${BOLD}${DATA_DIR}${NC}"
        return 0
    fi
    info "Creating data directory ${BOLD}${DATA_DIR}${NC} ..."
    mkdir -p "$DATA_DIR" || die "Failed to create ${DATA_DIR}"
    ok "Data directory ready: ${BOLD}${DATA_DIR}${NC}"
}

# =============================================================================
# RUNTIME DETECTION
# =============================================================================
detect_runtime() {
    if [[ -n "${RUNTIME:-}" ]]; then
        command -v "$RUNTIME" &>/dev/null || die "$RUNTIME not found in PATH."
        ok "Container runtime (forced): ${BOLD}$RUNTIME${NC}"
        return
    fi
    if command -v podman &>/dev/null; then
        RUNTIME="podman"
    elif command -v docker &>/dev/null; then
        RUNTIME="docker"
    else
        die "Neither podman nor docker found. Please install one and re-run."
    fi
    ok "Container runtime: ${BOLD}$RUNTIME${NC} ($(${RUNTIME} --version 2>/dev/null | head -1))"
    trap on_interrupt INT TERM
}

# Ctrl+C kills this script and the podman/docker build client, but the
# in-flight RUN step (buildah/crun children compiling the SDK or OTBR)
# survives as orphans and keeps burning CPU. Reap our descendants and
# remove leftover build containers so cancellation actually cancels.
on_interrupt() {
    trap - INT TERM
    echo
    warn "Interrupted — stopping build processes..."
    local kids
    kids=$(pgrep -P $$ | tr '\n' ' ')
    if [[ -n "$kids" ]]; then
        kill -TERM $kids 2>/dev/null || true
        sleep 2
        kill -KILL $kids 2>/dev/null || true
    fi
    # Leftover working containers from the interrupted build (does NOT
    # touch the running app container)
    "$RUNTIME" ps -a --external --format '{{.ID}} {{.Names}}' 2>/dev/null \
        | awk '/working-container|buildah/ {print $1}' \
        | xargs -r "$RUNTIME" rm --force >/dev/null 2>&1 || true
    exit 130
}

# =============================================================================
# PORT HANDLING
# =============================================================================
port_in_use() {
    local port=$1
    if command -v ss &>/dev/null; then
        ss -tlnH "sport = :${port}" 2>/dev/null | grep -q .
    elif command -v netstat &>/dev/null; then
        netstat -tln 2>/dev/null | grep -qE ":${port}\s"
    else
        grep -qE "^\s*[0-9A-Fa-f]+:$(printf '%04X' "${port}")\s" \
            /proc/net/tcp /proc/net/tcp6 2>/dev/null
    fi
}

get_port_process() {
    local port=$1
    local proc=""

    if command -v lsof &>/dev/null; then
        proc=$(sudo lsof -i :"${port}" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1" (PID: "$2")"}')
    fi

    if [[ -z "$proc" ]] && command -v ss &>/dev/null; then
        proc=$(sudo ss -lptn "sport = :${port}" 2>/dev/null | grep -o 'users:((".*"))' | sed 's/users:(("//; s/",pid=/ (PID: /; s/,.*//' | head -n 1)
    fi

    if [[ -n "$proc" ]]; then
        echo "$proc"
    else
        echo "an unknown process (run script with sudo to see details)"
    fi
}

find_free_port() {
    local port=$1
    while port_in_use "$port"; do
        ((port++))
        if [[ $port -gt 65535 ]]; then
            die "No free ports found."
        fi
    done
    echo "$port"
}

check_host_port() {
    # With --network=host the container binds directly to host ports.
    # Verify the port is free; if not, find an alternative and pass via env var.
    local preferred=$1
    if port_in_use "$preferred"; then
        local blocker
        blocker=$(get_port_process "$preferred")
        warn "Port ${preferred} is currently blocked by: ${BOLD}${blocker}${NC}" >&2
        warn "Scanning for the next available port..." >&2
        local found
        found=$(find_free_port "$((preferred + 1))")
        warn "Using port ${BOLD}${found}${NC} instead." >&2
        echo "$found"
    else
        echo "$preferred"
    fi
}

# =============================================================================
# DEPENDENCY CHECKS
# =============================================================================
check_deps() {
    local missing=()
    for cmd in git curl; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        die "Missing required tools: ${missing[*]}"
    fi
}

# =============================================================================
# CLONE / UPDATE REPO
# =============================================================================
fetch_repo() {
    # We deliberately do NOT run 'git pull' here. Pulling would either:
    #   - overwrite a deliberately-checked-out tag with main (wrong for upgrades), or
    #   - abort with "local changes would be overwritten" if anything in the
    #     tree was modified at runtime (which is exactly the swap-failure
    #     symptom we are fixing).
    #
    # If the operator wants to refresh from origin/main, they should:
    #   sudo rm -rf "$CLONE_DIR" && curl -fsSL <installer-url> | sudo bash
    # or use the upgrade flow with target_version=<commit-or-tag>.
    if [[ -d "$CLONE_DIR/.git" ]]; then
        local current_ref
        current_ref=$(git -C "$CLONE_DIR" describe --tags --always --dirty 2>/dev/null || echo "unknown")
        ok "Repository already present at ${CLONE_DIR} (ref: ${current_ref}) — skipping fetch."
    elif [[ -d "$CLONE_DIR" ]] && [[ -n "$(ls -A "$CLONE_DIR" 2>/dev/null || true)" ]]; then
        # Directory exists with content but no .git — could be a tarball
        # extraction or a botched previous install. Leave it alone and let
        # later steps (write_containerfile, build_image) decide whether the
        # contents are usable.
        warn "${CLONE_DIR} exists but is not a git checkout — proceeding with whatever is there."
    else
        info "Cloning ${REPO_URL} → ${CLONE_DIR} ..."
        mkdir -p "$(dirname "$CLONE_DIR")"
        git clone --branch "$REPO_BRANCH" "$REPO_URL" "$CLONE_DIR"
        ok "Repository cloned."
    fi
}

# =============================================================================
# USB COORDINATOR DETECTION
# =============================================================================
detect_usb_coordinator() {
    USB_DEVICE=""

    local -a found_devices=()
    local -a found_labels=()

    # Scan /dev/serial/by-id for known Zigbee coordinator patterns
    if [[ -d /dev/serial/by-id ]]; then
        for dev in /dev/serial/by-id/*; do
            [[ -e "$dev" ]] || continue
            local real_dev
            real_dev=$(readlink -f "$dev")
            local label
            label=$(basename "$dev")
            if echo "$label" | grep -qiE 'cp210|ezsp|zigbee|silabs|ember|ch340|ch341|cc253|cc265|conbee|raspbee|sonoff|tube|slzb|zzh'; then
                found_devices+=("$real_dev")
                found_labels+=("$label → $real_dev")
            fi
        done
    fi

    # Fallback to raw /dev/ttyACM* and /dev/ttyUSB*
    if [[ ${#found_devices[@]} -eq 0 ]]; then
        for dev in /dev/ttyACM0 /dev/ttyACM1 /dev/ttyUSB0 /dev/ttyUSB1; do
            if [[ -c "$dev" ]]; then
                found_devices+=("$dev")
                found_labels+=("$dev")
            fi
        done
    fi

    local count=${#found_devices[@]}

    if [[ $count -eq 0 ]]; then
        warn "No Zigbee USB coordinator detected."
        _prompt_manual_usb
        return
    fi

    if [[ $count -eq 1 ]]; then
        USB_DEVICE="${found_devices[0]}"
        ok "Zigbee coordinator detected: ${BOLD}${found_labels[0]}${NC}"
        return
    fi

    echo
    warn "Multiple potential Zigbee coordinators found:"
    echo
    for i in "${!found_devices[@]}"; do
        echo -e "  ${BOLD}$((i+1))${NC}) ${found_labels[$i]}"
    done
    echo -e "  ${BOLD}$((count+1))${NC}) Enter device path manually"
    echo -e "  ${BOLD}$((count+2))${NC}) Skip (no USB device)"
    echo

    local choice
    while true; do
        read -rp "  Select coordinator [1-$((count+2))]: " choice
        if [[ "$choice" =~ ^[0-9]+$ ]]; then
            if [[ $choice -ge 1 && $choice -le $count ]]; then
                USB_DEVICE="${found_devices[$((choice-1))]}"
                ok "Selected: ${found_labels[$((choice-1))]}"
                break
            elif [[ $choice -eq $((count+1)) ]]; then
                _prompt_manual_usb
                break
            elif [[ $choice -eq $((count+2)) ]]; then
                warn "No USB device selected."
                break
            fi
        fi
        warn "Invalid selection, try again."
    done
}

_prompt_manual_usb() {
    echo
    warn "Available serial devices:"
    local has_devs=false
    for dev in /dev/ttyUSB* /dev/ttyACM*; do
        if [[ -c "$dev" ]]; then
            echo "    $dev"
            has_devs=true
        fi
    done
    $has_devs || echo "    (none found)"
    echo
    read -rp "  Enter device path (blank to skip): " manual_dev
    if [[ -n "$manual_dev" ]]; then
        [[ -c "$manual_dev" ]] || die "Device ${manual_dev} does not exist."
        USB_DEVICE="$manual_dev"
        ok "Using: ${USB_DEVICE}"
    else
        warn "No USB device selected."
    fi
}

# =============================================================================
# CONTAINERFILE
# =============================================================================
write_containerfile() {
    cat > "$CLONE_DIR/Containerfile" << 'DOCKERFILE_TOP'
# Zigbee Matter Manager — Root Container
FROM python:3.11-slim-bookworm

# Force Python to flush logs immediately so the first-run password is visible
ENV PYTHONUNBUFFERED=1

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        lsb-release \
        sudo \
        git \
        ca-certificates \
        cmake \
        ninja-build \
        g++ \
        libffi-dev \
        libmbedtls-dev \
        libssl-dev \
        libdbus-1-dev \
        libavahi-client-dev \
        libreadline-dev \
        libboost-dev \
        libboost-filesystem-dev \
        libboost-system-dev \
        libnetfilter-queue-dev \
        libsystemd-dev \
        ipset \
        iptables \
        dbus \
        avahi-daemon \
        logrotate \
        curl \
        wget \
        unzip \
        jq \
        libglib2.0-0 \
        libnl-3-200 \
        libnl-route-3-200 \
        socat \
        procps \
        strace \
        iproute2 \
        net-tools \
        pkg-config \
        bluez \
    && rm -rf /var/lib/apt/lists/*

# Fetch and install Silicon Labs packages matching Bookworm
RUN DOWNLOAD_URL=$(curl -s https://api.github.com/repos/SiliconLabs/simplicity_sdk/releases/latest | jq -r '.assets[] | select(.name=="debian-bookworm.zip") | .browser_download_url') \
    && wget "$DOWNLOAD_URL" -O debian-bookworm.zip \
    && unzip debian-bookworm.zip -d /tmp/silabs \
    && ARCH=$(dpkg --print-architecture) \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        /tmp/silabs/debian-bookworm/deb/libcpc3_*_${ARCH}.deb \
        /tmp/silabs/debian-bookworm/deb/libcpc-dev_*_${ARCH}.deb \
        /tmp/silabs/debian-bookworm/deb/cpcd_*_${ARCH}.deb \
        /tmp/silabs/debian-bookworm/deb/zigbeed_*_${ARCH}.deb \
    && rm -rf /tmp/silabs debian-bookworm.zip /var/lib/apt/lists/*

# ── OTBR with SiLabs CPC MultiPAN support ──────────────────────────────
ENV SDK_DIR=/tmp/silabs_sdk

# 1. Sparse clone SiLabs SDK just to get the CPC vendor extension files
RUN git clone --depth 1 --filter=blob:none --sparse \
        https://github.com/SiliconLabs/simplicity_sdk.git ${SDK_DIR} && \
    cd ${SDK_DIR} && \
    git sparse-checkout set protocol/openthread/platform-abstraction/posix

# 2. Clone official OTBR, init submodules, clone matching cpc-daemon, then build
RUN echo '#!/bin/sh' > /usr/local/bin/sudo && \
    echo 'if echo "$*" | grep -Eq "/proc/sys|sysctl"; then exit 0; fi' >> /usr/local/bin/sudo && \
    echo 'exec /usr/bin/sudo "$@"' >> /usr/local/bin/sudo && \
    chmod +x /usr/local/bin/sudo && \
    git clone --depth 1 --branch v4.7.1 https://github.com/SiliconLabs/cpc-daemon.git /tmp/cpc-daemon && \
    sed -i 's/VERSION 4\.7\.1\b/VERSION 4.7.1.0/g' /tmp/cpc-daemon/CMakeLists.txt && \
    git clone --depth=1 https://github.com/openthread/ot-br-posix /tmp/otbr && \
    cd /tmp/otbr && \
    git submodule update --init --recursive && \
    cp ${SDK_DIR}/protocol/openthread/platform-abstraction/posix/openthread-core-silabs-posix-config.h \
       /tmp/otbr/third_party/openthread/repo/src/posix/platform/ && \
    ./script/bootstrap && \
    INFRA_IF_NAME=eth0 \
    OTBR_OPTIONS=" \
        -DOT_THREAD_VERSION=1.4 \
        -DOT_MULTIPAN_RCP=ON \
        -DCPCD_SOURCE_DIR=/tmp/cpc-daemon \
        -DOT_POSIX_RCP_VENDOR_BUS=ON \
        -DOT_POSIX_CONFIG_RCP_VENDOR_DEPS_PACKAGE=${SDK_DIR}/protocol/openthread/platform-abstraction/posix/posix_vendor_rcp.cmake \
        -DOT_POSIX_CONFIG_RCP_VENDOR_INTERFACE=${SDK_DIR}/protocol/openthread/platform-abstraction/posix/cpc_interface.cpp \
        -DOT_PLATFORM_CONFIG=openthread-core-silabs-posix-config.h" \
    ./script/setup && \
    rm -f /usr/local/bin/sudo

# 3. Disable systemd service (ZMM manages otbr-agent lifecycle) and clean up
RUN systemctl disable otbr-agent 2>/dev/null || true
RUN rm -rf ${SDK_DIR} /tmp/otbr /tmp/cpc-daemon

WORKDIR /app

# ── Application requirements (layer cache) ──
# Install from the fully-pinned lockfile for reproducible builds/upgrades.
# requirements.lock is generated from requirements.txt via scripts/regen_lock.sh
# (uv pip compile, python 3.11, manylinux_2_31).
# It already includes python-matter-server[server] (and its home-assistant-chip-core
# native runtime), so no separate extras install is needed. The manylinux_2_31
# platform tag matches this bookworm base (glibc 2.36) and resolves for amd64+arm64.
#
# The --mount=type=cache keeps pip's wheel/download cache in a host-side
# buildah volume that survives across builds (it is NOT committed to the
# image). When a release changes requirements, only the new/changed packages
# are downloaded — everything else installs from the local cache. Supported
# natively by podman/buildah; docker needs BuildKit (both fine here).
#
# The follow-up `pip install -r requirements.txt` is a self-heal top-up: when
# the lock is complete it's a no-op, but if a release added a package to
# requirements.txt without regenerating the lock, this installs the missing
# package (at latest resolvable version) instead of shipping an image that
# breaks at import time. The upgrade watcher logs a drift warning when this
# top-up is expected to do real work.
COPY requirements.lock requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip \
 && pip install -r requirements.lock \
 && pip install -r requirements.txt
DOCKERFILE_TOP

    # Part 2 — zmm_telemetry Rust appender (optional)
    if [[ "$WITH_APPENDER" == true ]]; then
        info "Including zmm_telemetry Rust appender in image build"
        cat >> "$CLONE_DIR/Containerfile" << 'DOCKERFILE_APPENDER'

# ── Build zmm_telemetry from source inside the container ──
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-dev \
        pkg-config \
 && rm -rf /var/lib/apt/lists/* \
 && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --default-toolchain stable --profile minimal \
 && pip install --no-cache-dir maturin

ENV PATH="/root/.cargo/bin:${PATH}"

ARG BUILD_JOBS=4
ENV CMAKE_BUILD_PARALLEL_LEVEL=${BUILD_JOBS}
ENV MAKEFLAGS="-j${BUILD_JOBS}"

COPY zmm_telemetry/ /tmp/zmm_telemetry/
RUN cd /tmp/zmm_telemetry \
 && maturin build --release --out /tmp/wheels \
 && pip install --no-cache-dir /tmp/wheels/zmm_telemetry-*.whl \
 && rm -rf /tmp/zmm_telemetry /tmp/wheels /root/.cargo /root/.rustup /root/.cache
DOCKERFILE_APPENDER
    else
        info "Skipping zmm_telemetry Rust appender — Python executemany fallback will be used"
        # Bake an env var into the image so telemetry_db.py forces the Python path
        # even if a stray zmm_telemetry wheel is somehow present at runtime.
        cat >> "$CLONE_DIR/Containerfile" << 'DOCKERFILE_NOAPPENDER'

# Force Python executemany fallback (no Rust appender built into this image)
ENV ZMM_TELEMETRY_BACKEND=python
DOCKERFILE_NOAPPENDER
    fi

    # Part 3 — application source and final image config (always present)
    cat >> "$CLONE_DIR/Containerfile" << 'DOCKERFILE_BOTTOM'

# cloudflared — static Go binary for the managed remote-access tunnel
# (Settings → Security → Remote Access). Arch-aware: amd64/arm64.
# Deliberately placed late in the file: a tiny download layer here keeps
# the heavy SDK/OTBR/pip layers above fully cached.
RUN ARCH=$(dpkg --print-architecture) \
    && curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}" \
         -o /usr/local/bin/cloudflared \
    && chmod +x /usr/local/bin/cloudflared \
    && /usr/local/bin/cloudflared --version

# Application source
COPY . .

# Application version control - used for upgrades
COPY VERSION /app/VERSION

# Required directories
RUN mkdir -p /data /app/data/matter /app/data/backups /app/data/certs /app/logs /app/config /var/lib/thread \
        /usr/local/lib/python3.11/site-packages/credentials/development/paa-root-certs

ENV ZMM_BACKUP_DIR=/app/data/backups
ENV ZMM_APP_DIR=/app

EXPOSE 8000 5580

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsk https://localhost:${ZMM_PORT:-8000}/api/system/health || \
        curl -fs  http://localhost:${ZMM_PORT:-8000}/api/system/health  || exit 1

CMD ["python", "launcher.py"]
DOCKERFILE_BOTTOM
    ok "Containerfile written (appender=${WITH_APPENDER})."
}

# =============================================================================
# BUILD IMAGE
# =============================================================================
build_image() {
    local log_file="${ZMM_BUILD_LOG:-/tmp/zmm-build-$$.log}"
    local rc=0

    info "Building image ${BOLD}${IMAGE_NAME}${NC} with ${BUILD_JOBS} parallel jobs ..."
    info "(progress shown below; full build output saved to ${log_file})"

    # set -o pipefail propagates podman's exit code through the pipe.
    "$RUNTIME" build \
        --format docker \
        --build-arg BUILD_JOBS="${BUILD_JOBS}" \
        --tag "${IMAGE_NAME}:latest" \
        --file "$CLONE_DIR/Containerfile" \
        "$CLONE_DIR" 2>&1 | build_progress_filter "$log_file" || rc=$?

    if (( rc != 0 )); then
        echo
        error "Image build FAILED (exit $rc)."
        error "Last 30 lines of build output:"
        tail -n 30 "$log_file" | sed 's/^/    /' >&2
        error "Full log: $log_file"
        exit "$rc"
    fi

    ok "Image built: ${IMAGE_NAME}:latest"
    info "Full build log: $log_file"
}

# =============================================================================
# PREPARE DATA DIRECTORIES
# =============================================================================
prepare_data_dirs() {
    local dirs=(
        "$DATA_DIR/config"
        "$DATA_DIR/data"
        "$DATA_DIR/data/certs"
        "$DATA_DIR/data/matter"
        "$DATA_DIR/logs"
    )
    for d in "${dirs[@]}"; do
        mkdir -p "$d"
    done

    # Seed config.yaml from the clone (the only place a template config exists)
    if [[ ! -f "$DATA_DIR/config/config.yaml" ]] && [[ -f "$CLONE_DIR/config/config.yaml" ]]; then
        cp "$CLONE_DIR/config/config.yaml" "$DATA_DIR/config/config.yaml"
        ok "Default config.yaml seeded."
    fi

    # Patch USB device into config.yaml
    if [[ -n "${USB_DEVICE:-}" && -f "$DATA_DIR/config/config.yaml" ]]; then
        sed -i "s|port:.*\/dev\/tty[A-Za-z]*[0-9]*|port: ${USB_DEVICE}|g" \
            "$DATA_DIR/config/config.yaml"
        ok "config.yaml updated with device: ${USB_DEVICE}"
    fi

    ok "Data directories ready at ${DATA_DIR}"
}

# =============================================================================
# HOST DBUS POLICY FOR OTBR
# =============================================================================
prepare_otbr_dbus_policy() {
    local policy_file="/etc/dbus-1/system.d/otbr-agent.conf"

    if [[ -f "$policy_file" ]] && grep -q "context=\"default\"" "$policy_file" 2>/dev/null; then
        ok "OTBR D-Bus policy already configured"
        return
    fi

    info "Installing D-Bus policy for Thread border router..."
    sudo tee "$policy_file" > /dev/null << 'DBUS_POLICY'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy context="default">
    <allow own_prefix="io.openthread.BorderRouter"/>
    <allow send_destination="io.openthread.BorderRouter.wpan0"/>
    <allow send_interface="io.openthread.BorderRouter"/>
    <allow send_interface="org.freedesktop.DBus.Properties"/>
    <allow send_interface="org.freedesktop.DBus.Introspectable"/>
  </policy>
</busconfig>
DBUS_POLICY
    sudo systemctl reload dbus 2>/dev/null || true
    ok "OTBR D-Bus policy installed"
}

# =============================================================================
# RUN CONTAINER
# =============================================================================
# Create the pod the app (and later the manager sidecar) join. Idempotent: a
# pre-existing pod is reused. Podman only.
#
# Host networking — NOT bridge — is required: Cast (pychromecast/zeroconf),
# Thread/OTBR, and Matter all rely on LAN multicast/mDNS, which podman's default
# bridge NAT does not pass. On the host network the pod members bind ports
# directly (no --publish), and the manager sidecar (CP2) reaches the app on
# 127.0.0.1 because they share the host netns.
ensure_pod() {
    "$RUNTIME" pod exists "$POD_NAME" 2>/dev/null && return 0
    info "Creating pod '${POD_NAME}' on the host network (mDNS/Cast/Thread/Matter)..."
    "$RUNTIME" pod create --name "$POD_NAME" --network=host
}

run_container() {
    local host_port=$1
    local host_matter_port=$2
    # Optional 3rd arg: full image ref to run. Defaults to ${IMAGE_NAME}:latest
    # for the build-and-deploy flow. The upgrade swap flow passes a specific
    # versioned tag like zigbee-matter-manager:2.0.1-amd64.
    local image_tag="${3:-${IMAGE_NAME}:latest}"

    # Remove existing container
    if "$RUNTIME" inspect "$CONTAINER_NAME" &>/dev/null 2>&1; then
        warn "Removing existing '${CONTAINER_NAME}' container..."
        "$RUNTIME" rm -f "$CONTAINER_NAME"
    fi

    # Note: the net.* forwarding sysctls are NOT set here. They live in the network
    # namespace, so podman rejects them on a --network=host container (and they
    # must instead be applied to the host). They're added per-mode below.
    local run_args=(
        --detach
        --name "$CONTAINER_NAME"
        --security-opt label=disable
        --cap-add=NET_ADMIN
        --cap-add=NET_RAW
        --cap-add=SYS_ADMIN
        --device /dev/net/tun:/dev/net/tun
        --volume /dev/shm:/dev/shm
        --volume /run/dbus:/run/dbus
        --volume "${DATA_DIR}/config:/app/config"
        --volume "${DATA_DIR}/data:/app/data"
        --volume "${DATA_DIR}/data/certs:/app/data/certs"
        --volume "${DATA_DIR}/logs:/app/logs"
    )

    # ── Host timezone passthrough ──
    # The app uses naive datetime.now() for time-based automations; without this
    # the container runs on UTC and timed rules fire offset from wall-clock time.
    if [[ -e /etc/localtime ]]; then
        run_args+=(--volume /etc/localtime:/etc/localtime:ro)
        ok "Timezone: mounted host /etc/localtime into container"
    fi

    # ── Networking: host-net pod (podman) vs standalone slirp4netns (docker) ──
    if [[ "$RUNTIME" == "podman" ]]; then
        ensure_pod
        run_args+=(--pod "$POD_NAME")
        # Host netns: per-container net.* sysctls are rejected by podman, so apply
        # the Thread/OTBR forwarding sysctls to the HOST instead (live + persisted
        # so they survive reboot). MQTT/Matter/Cast don't need these — only Thread
        # border routing does — so failure here is non-fatal.
        sysctl -w net.ipv6.conf.all.disable_ipv6=0 >/dev/null 2>&1 || true
        sysctl -w net.ipv6.conf.all.forwarding=1   >/dev/null 2>&1 || true
        sysctl -w net.ipv4.conf.all.forwarding=1   >/dev/null 2>&1 || true
        sudo tee /etc/sysctl.d/99-zmm-thread.conf >/dev/null 2>&1 <<'SYSCTL' || true
net.ipv6.conf.all.disable_ipv6 = 0
net.ipv6.conf.all.forwarding = 1
net.ipv4.conf.all.forwarding = 1
SYSCTL
        ok "Networking: pod '${POD_NAME}' on host net (forwarding sysctls applied to host)"
    else
        run_args+=(
            --network=slirp4netns
            --publish "${host_port}:${INTERNAL_PORT}"
            --publish "${host_matter_port}:${MATTER_INTERNAL_PORT}"
            --sysctl net.ipv6.conf.all.disable_ipv6=0
            --sysctl net.ipv6.conf.all.forwarding=1
            --sysctl net.ipv4.conf.all.forwarding=1
        )
        ok "Networking: host (ZMM: ${host_port}, Matter: ${host_matter_port})"
    fi

    # ── Bluetooth for Matter commissioning ──
    if [[ -e /dev/hci0 ]]; then
        run_args+=(--device /dev/hci0:/dev/hci0)
        ok "Bluetooth adapter available for Matter commissioning"
    fi

    # ── USB device passthrough (direct — root container has full access) ──
    if [[ -n "${USB_DEVICE:-}" ]]; then
        local real_dev
        real_dev=$(readlink -f "$USB_DEVICE")
        run_args+=(--device "${real_dev}:${real_dev}")

        # If the original path was a symlink, also map that
        if [[ "$USB_DEVICE" != "$real_dev" ]]; then
            run_args+=(--device "${USB_DEVICE}:${USB_DEVICE}")
        fi
    fi

    # ── USB bus access for USBDEVFS_RESET (MultiPAN CPC state cleanup) ──
    if [[ -d /dev/bus/usb ]]; then
        run_args+=(-v /dev/bus/usb:/dev/bus/usb)
        ok "Mounted /dev/bus/usb for USB device reset support"
    fi

    # ── Container-runtime socket passthrough (for local AI / Ollama) ──
    # ZMM is a root container with no podman/docker CLI inside it; to manage a
    # sibling Ollama container it talks to the runtime's Docker-compatible REST
    # API over this socket. Docker always exposes one. Podman's API socket
    # usually isn't enabled (the CLI doesn't need it) — but we already run
    # privileged and set up host state, so just enable it here (idempotent,
    # non-fatal) rather than making it a manual prerequisite.
    local _rt_sock=""
    if [[ "$RUNTIME" == "podman" ]]; then
        _rt_sock="/run/podman/podman.sock"
        if [[ ! -S "$_rt_sock" ]] && command -v systemctl &>/dev/null; then
            info "Enabling podman API socket (for local AI / Ollama management)..."
            sudo systemctl enable --now podman.socket >/dev/null 2>&1 || true
        fi
    else
        _rt_sock="/var/run/docker.sock"
    fi
    if [[ -S "$_rt_sock" ]]; then
        run_args+=(--volume "${_rt_sock}:${_rt_sock}")
        run_args+=(--env "ZMM_CONTAINER_SOCK=${_rt_sock}")
        ok "Mounted ${RUNTIME} socket (${_rt_sock}) for local AI container management"
    else
        info "No ${RUNTIME} socket available — local AI (Ollama) install stays disabled"
        info "  (the rest of ZMM is unaffected)."
    fi

    info "Starting container '${CONTAINER_NAME}' from ${image_tag} ..."
    "$RUNTIME" run "${run_args[@]}" "$image_tag"
    ok "Container started."

    # Verify device access
    if [[ -n "${USB_DEVICE:-}" ]]; then
        sleep 2
        local real_dev
        real_dev=$(readlink -f "$USB_DEVICE")
        info "Verifying device access inside container..."
        if "$RUNTIME" exec "$CONTAINER_NAME" test -r "$real_dev" 2>/dev/null; then
            ok "Device ${real_dev} is readable inside container."
        else
            warn "Device ${real_dev} may not be accessible. Check logs:"
            warn "  ${RUNTIME} logs ${CONTAINER_NAME}"
        fi
    fi
}

# =============================================================================
# MANAGER SIDECAR (decoupled)
# =============================================================================
# Run the always-on manager as a SEPARATE container — NOT a pod member. It reuses
# the app image (which has the manager/ package + uvicorn/httpx) run as
# `python -m manager`, mounts the runtime socket so it can inspect containers, and
# sits on its OWN bridge network so the app (a Thread border router that churns
# the host netns on every start) can't knock it offline. It reaches the app via
# host.containers.internal:8000 instead of 127.0.0.1, publishes :8001 itself, and
# has its own systemd unit for reboot (the pod unit no longer covers it).
run_manager_container() {
    [[ "$RUNTIME" == "podman" ]] || { info "Manager sidecar needs podman — skipping."; return 0; }

    local app_image="${1:-${IMAGE_NAME}:latest}"

    # Resolve the runtime socket (enabled by run_container) for read-only inspection.
    local sock=""
    for s in "${ZMM_CONTAINER_SOCK:-}" /run/podman/podman.sock /var/run/podman/podman.sock; do
        [[ -n "$s" && -S "$s" ]] && { sock="$s"; break; }
    done

    if "$RUNTIME" inspect "$MANAGER_CONTAINER_NAME" &>/dev/null 2>&1; then
        "$RUNTIME" rm -f "$MANAGER_CONTAINER_NAME" >/dev/null 2>&1 || true
    fi

    info "Starting manager sidecar '${MANAGER_CONTAINER_NAME}' on :${MANAGER_PORT} (off-pod, own bridge)..."
    local margs=(
        --name "$MANAGER_CONTAINER_NAME"
        --security-opt label=disable
        --no-healthcheck
        --publish "${MANAGER_PORT}:${MANAGER_PORT}"
        --add-host "host.containers.internal:host-gateway"
        --volume "${DATA_DIR}:${DATA_DIR}"
        --env "ZMM_POD_NAME=${POD_NAME}"
        --env "ZMM_CONTAINER_NAME=${CONTAINER_NAME}"
        --env "ZMM_MANAGER_PORT=${MANAGER_PORT}"
        --env "ZMM_DATA_DIR=${DATA_DIR}"
        --env "ZMM_APP_HEALTH_URL=https://host.containers.internal:${INTERNAL_PORT}/api/system/health"
    )
    if [[ -n "$sock" ]]; then
        margs+=(--volume "${sock}:${sock}" --env "ZMM_CONTAINER_SOCK=${sock}")
        ok "Manager: mounted runtime socket ${sock}"
    else
        warn "Manager: no runtime socket found — container list will be unavailable"
    fi

    if command -v systemctl >/dev/null 2>&1; then
        "$RUNTIME" create "${margs[@]}" "$app_image" python -m manager
        install_manager_autostart
        if sudo systemctl restart "${MANAGER_CONTAINER_NAME}.service"; then
            ok "Manager sidecar running under ${MANAGER_CONTAINER_NAME}.service (off-pod, Restart=always)."
        else
            warn "systemctl start failed — starting manager container directly"
            "$RUNTIME" start "$MANAGER_CONTAINER_NAME"
        fi
    else
        "$RUNTIME" run --detach "${margs[@]}" "$app_image" python -m manager
        ok "Manager sidecar started off-pod (matches app scheme on :${MANAGER_PORT})."
        install_manager_autostart
    fi
}

# Tiny systemd unit so the off-pod manager comes back on reboot (the pod unit
# only covers pod members, which the manager no longer is). Podman-only; best-
# effort. Mirrors the app unit's start/stop-a-named-container pattern.
install_manager_autostart() {
    command -v systemctl >/dev/null 2>&1 || return 0
    [[ "$RUNTIME" == "podman" ]] || return 0
    local runtime_bin; runtime_bin=$(command -v "$RUNTIME")
    local unit_file="/etc/systemd/system/${MANAGER_CONTAINER_NAME}.service"
    sudo tee "$unit_file" > /dev/null << UNIT
[Unit]
Description=ZMM Manager sidecar (${MANAGER_CONTAINER_NAME})
After=network-online.target ${CONTAINER_NAME}.service
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Restart=always
RestartSec=10
# The container may already be running outside this unit (started by build.sh,
# or left behind when a previous ExecStop timed out). 'start -a' on a running
# container fails with 125 and the unit flaps forever — always stop first so
# this unit takes ownership. The '-' prefix means: ignore failure when
# nothing to stop. (NB: no backticks in this heredoc — unquoted delimiter,
# so backticks would execute as command substitution.)
ExecStartPre=-${runtime_bin} stop -t 10 ${MANAGER_CONTAINER_NAME}
ExecStart=${runtime_bin} start -a ${MANAGER_CONTAINER_NAME}
ExecStop=${runtime_bin} stop -t 10 ${MANAGER_CONTAINER_NAME}
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload >/dev/null 2>&1 || true
    sudo systemctl enable "${MANAGER_CONTAINER_NAME}.service" >/dev/null 2>&1 || true
    ok "Manager autostart unit installed: ${unit_file}"
}

# =============================================================================
# SYSTEMD AUTO-START
# =============================================================================
install_autostart() {
    if ! command -v systemctl &>/dev/null; then
        warn "systemd not found — skipping auto-start."
        return
    fi

    local runtime_bin
    runtime_bin=$(which "$RUNTIME")

    # Wait for the Zigbee dongle before `podman start` so the boot doesn't race
    # USB enumeration — a missing --device makes `podman start` itself fail and
    # systemd restart it (the "frontend up → down → up" flap). Skipped for
    # socket:// MultiPAN (no local device). USB_DEVICE is resolved by run_container.
    local device_pre=""
    if [[ -n "${USB_DEVICE:-}" ]]; then
        device_pre="ExecStartPre=/bin/bash -c 'for i in \$(seq 1 45); do [ -e \"${USB_DEVICE}\" ] && exit 0; sleep 1; done; echo \"device ${USB_DEVICE} absent after 45s; starting anyway\" >&2; exit 0'"
    fi

    local unit_file="/etc/systemd/system/${CONTAINER_NAME}.service"

    if [[ "$RUNTIME" == "podman" ]] && "$RUNTIME" pod exists "$POD_NAME" 2>/dev/null; then
        # Pod deployment: start/stop the WHOLE pod (infra + members). `pod start`
        # returns once members are up, so this is a oneshot + RemainAfterExit unit.
        sudo tee "$unit_file" > /dev/null << UNIT
[Unit]
Description=Zigbee Matter Manager Pod (${POD_NAME})
After=network-online.target time-sync.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=300
${device_pre}
ExecStart=${runtime_bin} pod start ${POD_NAME}
ExecStop=${runtime_bin} pod stop -t 15 ${POD_NAME}

[Install]
WantedBy=multi-user.target
UNIT
    else
        # Standalone (docker / non-pod): start/stop the single container.
        sudo tee "$unit_file" > /dev/null << UNIT
[Unit]
Description=Zigbee Matter Manager Container
# Order after the network AND the clock is set — TLS/token checks fail if the
# app starts before time sync. Don't let repeated early-boot retries trip the
# systemd start limiter into giving up.
After=network-online.target time-sync.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Restart=always
RestartSec=10
# MultiPAN/CPC bring-up can take 40-70s; don't let systemd kill a slow-but-fine
# start. The launcher also waits for the device inside the container.
TimeoutStartSec=300
${device_pre}
ExecStart=${runtime_bin} start -a ${CONTAINER_NAME}
ExecStop=${runtime_bin} stop -t 15 ${CONTAINER_NAME}

[Install]
WantedBy=multi-user.target
UNIT
    fi

    sudo systemctl daemon-reload
    sudo systemctl enable "${CONTAINER_NAME}.service"
    ok "Systemd unit installed and enabled: ${unit_file}"
}

# =============================================================================
# USAGE
# =============================================================================
usage() {
    cat << EOF
${BOLD}Usage:${NC} $0 [OPTIONS]

${BOLD}Must run as root${NC} — rootful podman is required for the Zigbee USB
coordinator, OTBR and the host systemd units. When piping from curl, pipe into
${BOLD}sudo bash${NC} (NOT sudo curl — that only elevates curl, leaving bash
unprivileged so it can't create ${DATA_DIR}):

  curl -fsSL <installer-url> | sudo bash -s -- [OPTIONS]

Run from a local checkout as a normal user and it re-execs itself under sudo.

${BOLD}Options:${NC}
  --port   PORT      Preferred host port  (default: ${INTERNAL_PORT})
  --usb    DEVICE    Zigbee USB device    (default: auto-detect)
  --dir    PATH      App clone directory  (default: ${APP_DIR})
  --data   PATH      Persistent data dir  (default: ${DATA_DIR})
  --branch NAME      Git branch           (default: ${REPO_BRANCH})
  --runtime NAME     docker or podman     (default: auto-detect)
  --no-autostart     Skip systemd unit installation
  --rebuild          Force image rebuild
  --with-appender    Build the Rust zmm_telemetry appender into the image
                     (default: off — Python executemany fallback is used)
  --no-appender      Explicitly skip the Rust appender (default)
  --help             Show this message

${BOLD}Environment:${NC}
  ZMM_APP_DIR        Override app directory
  ZMM_DATA_DIR       Override data directory
EOF
    exit 0
}

# =============================================================================
# ARGUMENT PARSING
# =============================================================================
main() {
ensure_root "$@"
PREFERRED_PORT=$INTERNAL_PORT
INSTALL_AUTOSTART=true
FORCE_REBUILD=false
WITH_APPENDER=false   # Build the Rust zmm_telemetry wheel into the image.
                      # Default off — the Python executemany fallback in
                      # telemetry_db.py is sufficient for small/medium
                      # networks. Enable for large/enterprise debug captures.

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)         PREFERRED_PORT="$2";    shift 2 ;;
        --usb)          USB_DEVICE="$2";        shift 2 ;;
        --dir)          APP_DIR="$2";           shift 2 ;;
        --data)         DATA_DIR="$2";          shift 2 ;;
        --branch)       REPO_BRANCH="$2";       shift 2 ;;
        --runtime)      RUNTIME="$2";           shift 2 ;;
        --no-autostart) INSTALL_AUTOSTART=false; shift ;;
        --rebuild)      FORCE_REBUILD=true;     shift ;;
        --with-appender)    WITH_APPENDER=true;  shift ;;
        --no-appender)      WITH_APPENDER=false; shift ;;
        --help|-h)      usage ;;
        *) die "Unknown argument: $1  (use --help)" ;;
    esac
done

# =============================================================================
# MAIN
# =============================================================================
echo
echo -e "${BOLD}=====================================================${NC}"
echo -e "${BOLD}   Zigbee Matter Manager — Container Build & Deploy  ${NC}"
echo -e "${BOLD}=====================================================${NC}"
echo
echo -e "${BOLD}This install will run 10 steps:${NC}"
echo "   1. Pre-flight checks"
echo "   2. Fetch repository"
echo "   3. USB coordinator detection"
echo "   4. Verify host ports are free"
echo -e "   5. Build container image  ${BOLD}(longest step — 2-25 min)${NC}"
echo "   6. Prepare data directories"
echo "   7. OTBR D-Bus policy"
echo "   8. Start container"
echo "   9. Install systemd auto-start unit"
echo "  10. Confirm app code location and install upgrade watcher"
echo

step_announce "Pre-flight checks"
check_deps
detect_runtime
ensure_data_dir

step_announce "Fetch repository"
fetch_repo

step_announce "USB coordinator detection"
if [[ -z "${USB_DEVICE:-}" ]]; then
    detect_usb_coordinator
else
    info "Using --usb override: ${USB_DEVICE}"
fi

step_announce "Verify host ports are free"
HOST_PORT=$(check_host_port "$PREFERRED_PORT")
HOST_MATTER_PORT=$(check_host_port "$MATTER_INTERNAL_PORT")

step_announce "Build container image"
write_containerfile

BUILD_JOBS=$(detect_build_jobs)
info "Detected ${BUILD_JOBS} build jobs for parallel compile"

if "$FORCE_REBUILD" || ! "$RUNTIME" image inspect "${IMAGE_NAME}:latest" &>/dev/null 2>&1; then
    build_image
else
    info "Image exists — skipping build (use --rebuild to force)."
fi

step_announce "Prepare data directories"
prepare_data_dirs

step_announce "OTBR D-Bus policy"
prepare_otbr_dbus_policy

step_announce "Start container"
run_container "$HOST_PORT" "$HOST_MATTER_PORT"
run_manager_container "${IMAGE_NAME}:latest"

step_announce "Install systemd auto-start unit"
if [[ "$INSTALL_AUTOSTART" == true ]]; then
    install_autostart
else
    info "Skipped (--no-autostart)"
fi

step_announce "Confirm app code location"
# In the new single-source-dir layout CLONE_DIR == APP_DIR, so there is no
# copy step. Verify the canonical files are present and executable, then
# carry on. The 'Populate APP_DIR' phase used to live here; it's retained
# as a sanity check so the step count stays at 10.
mkdir -p "${APP_DIR}/scripts" "${DATA_DIR}/data/upgrade" "${DATA_DIR}/data/state"

if [[ -f "${APP_DIR}/build.sh" ]]; then
    chmod +x "${APP_DIR}/build.sh" 2>/dev/null || true
    ok "build.sh present at ${APP_DIR}/build.sh"
else
    warn "build.sh missing at ${APP_DIR}/build.sh — upgrade flow may break"
fi

if compgen -G "${APP_DIR}/scripts/*.sh" >/dev/null 2>&1; then
    chmod +x "${APP_DIR}/scripts/"*.sh 2>/dev/null || true
    ok "Helper scripts present at ${APP_DIR}/scripts/"
else
    warn "No helper scripts found in ${APP_DIR}/scripts/"
    warn "Upgrade flow will not work until install_watcher.sh repopulates them."
fi

# ── Persist appender choice for upgrades ─────────────────────────────────────
# The user passed --with-appender (or didn't) at install time. Record that
# decision in DATA_DIR/data/state/ so upgrade.sh's do_build can read it back
# when it re-runs write_containerfile against the new tag — without needing
# the operator to remember and re-pass the flag.
#
# DATA_DIR survives APP_DIR wipes during upgrades; APP_DIR does not. So this
# is the right place for the marker.
APPENDER_MARKER="${DATA_DIR}/data/state/appender.enabled"
if [[ "$WITH_APPENDER" == true ]]; then
    echo "true"  > "$APPENDER_MARKER"
    ok "Appender marker written: ${APPENDER_MARKER} = true"
else
    echo "false" > "$APPENDER_MARKER"
    ok "Appender marker written: ${APPENDER_MARKER} = false"
fi

step_announce "Install upgrade watcher"
if [[ ! -f "${DATA_DIR}/data/upgrade/.watcher_installed" ]]; then
    if [[ -f "${APP_DIR}/scripts/upgrade.sh" && -f "${APP_DIR}/scripts/install_watcher.sh" ]]; then
        ZMM_DATA_DIR="$DATA_DIR" ZMM_APP_DIR="$APP_DIR" \
            bash "${APP_DIR}/scripts/install_watcher.sh" || \
            warn "Watcher install encountered issues — you can re-run it later from the Settings tab"
    else
        warn "scripts/upgrade.sh or install_watcher.sh missing in ${APP_DIR}/scripts/"
        warn "Upgrade feature will not be available until install_watcher.sh is re-run."
    fi
else
    ok "Upgrade watcher already installed"
fi

# =============================================================================
# SUMMARY
# =============================================================================
echo
echo -e "${BOLD}=====================================================${NC}"
echo -e "${GREEN}${BOLD}   Deployment Complete!${NC}"
echo -e "${BOLD}=====================================================${NC}"
echo
echo -e "  ${BOLD}Web Interface:${NC}  https://$(hostname -I 2>/dev/null | awk '{print $1}'):${HOST_PORT}"
echo -e "  ${BOLD}Matter Port:${NC}    ${HOST_MATTER_PORT}"
if [[ -n "${USB_DEVICE:-}" ]]; then
    echo -e "  ${BOLD}Zigbee USB:${NC}     ${USB_DEVICE}"
fi
echo -e "  ${BOLD}Config:${NC}         ${DATA_DIR}/config/config.yaml"
echo -e "  ${BOLD}Logs:${NC}           ${RUNTIME} logs -f ${CONTAINER_NAME}"
echo -e "  ${BOLD}Data:${NC}           ${DATA_DIR}/"
echo -e "  ${BOLD}Runtime:${NC}        ${RUNTIME}"
echo -e "  ${BOLD}Network:${NC}        host"
echo
echo -e "  ${BOLD}Commands:${NC}"
echo -e "    ${RUNTIME} logs -f ${CONTAINER_NAME}        # Follow logs"
echo -e "    ${RUNTIME} exec -it ${CONTAINER_NAME} bash  # Shell"
echo -e "    ${RUNTIME} stop ${CONTAINER_NAME}           # Stop"
echo -e "    ${RUNTIME} start ${CONTAINER_NAME}          # Start"
echo -e "    ${RUNTIME} rm -f ${CONTAINER_NAME}          # Remove"
echo
echo
echo -e "${RED}${BOLD}=====================================================${NC}"
echo -e "${RED}${BOLD}  !!! NOTICE !!! ${NC}"
echo -e "${RED}${BOLD}=====================================================${NC}"
echo
echo -e "Should you wish to rebuild the container please use the teardown script"
echo -e "  ${BOLD}Data:${NC}           ${DATA_DIR}/teardown.sh"
echo
}
# =============================================================================
# ENTRY POINT GUARD
# =============================================================================
# Only run main() when this script is executed directly. When sourced (e.g.
# by run_container.sh) the function definitions are loaded but no orchestration
# runs.
if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
    # CP4: refresh ONLY the manager sidecar to match the running app — no app or
    # pod changes. Called by the host watcher (upgrade.sh:do_swap) after a
    # successful swap so the manager ships manager-side changes through upgrades.
    # The manager is the app image run as `python -m manager`, so we recreate it
    # from whatever image the app container currently runs.
    if [[ " $* " == *" --refresh-manager "* ]]; then
        detect_runtime || { error "No container runtime"; exit 1; }
        _base=$("$RUNTIME" inspect -f '{{.ImageName}}' "$CONTAINER_NAME" 2>/dev/null || echo "")
        if [[ -z "$_base" || "$_base" == "<nil>" ]]; then
            _cimg=$("$RUNTIME" inspect -f '{{.Image}}' "$CONTAINER_NAME" 2>/dev/null || echo "")
            _base=$("$RUNTIME" image inspect --format '{{index .RepoTags 0}}' "$_cimg" 2>/dev/null || echo "${IMAGE_NAME}:latest")
        fi
        run_manager_container "$_base"
        exit $?
    fi
    main "$@"
fi