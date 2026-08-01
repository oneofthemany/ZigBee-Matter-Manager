#!/bin/bash
# =============================================================================
# ZMM Upgrade Watcher Installer
# =============================================================================
set -euo pipefail

# =============================================================================
# WATCHER SCHEMA VERSION
# =============================================================================
WATCHER_SCHEMA_VERSION=7

CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${CYAN}${BOLD}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}${BOLD}[ OK ]${NC} $*"; }
warn()  { echo -e "${YELLOW}${BOLD}[WARN]${NC} $*"; }
err()   { echo -e "${RED}${BOLD}[ERR ]${NC} $*" >&2; }

DATA_DIR="${ZMM_DATA_DIR:-/opt/.zigbee-matter-manager}"
APP_DIR="${ZMM_APP_DIR:-/opt/.zigbee-matter-manager/upgrade_build}"

SCRIPTS_DIR="${ZMM_SCRIPTS_DIR:-${DATA_DIR}/scripts}"

UPGRADE_DIR="${DATA_DIR}/data/upgrade"
STATE_DIR="${DATA_DIR}/data/state"
LOG_DIR="${DATA_DIR}/logs"

if [[ ! -d "$SCRIPTS_DIR" ]]; then
    if [[ "$(id -u)" -eq 0 ]]; then
        mkdir -p "$SCRIPTS_DIR"
    else
        sudo mkdir -p "$SCRIPTS_DIR"
        sudo chown "$USER:$USER" "$SCRIPTS_DIR"
    fi
fi

mkdir -p "$UPGRADE_DIR" "$STATE_DIR" "$LOG_DIR"

if command -v restorecon >/dev/null 2>&1 && [[ -e /sys/fs/selinux/enforce ]]; then
    if [[ "$(id -u)" -eq 0 ]]; then
        restorecon -R "$SCRIPTS_DIR" >/dev/null 2>&1 || true
    else
        sudo restorecon -R "$SCRIPTS_DIR" >/dev/null 2>&1 || true
    fi
fi

# ── Prerequisites ────────────────────────────────────────────────────────────
info "Checking prerequisites..."
MISSING=()
for cmd in jq curl git; do
    command -v "$cmd" >/dev/null 2>&1 || MISSING+=("$cmd")
done

if ! command -v podman >/dev/null 2>&1 && ! command -v docker >/dev/null 2>&1; then
    err "Neither podman nor docker found. Install one and re-run."
    exit 1
fi

if (( ${#MISSING[@]} > 0 )); then
    err "Missing required tools: ${MISSING[*]}"
    warn "Install on Debian/Ubuntu:  sudo apt install ${MISSING[*]}"
    warn "Install on Fedora:         sudo dnf install ${MISSING[*]}"
    warn "Install on Alpine:         sudo apk add ${MISSING[*]}"
    exit 1
fi

ok "Prerequisites OK"

# ── Copy scripts from repo clone or current dir ──────────────────────────────
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install_file() {
    local src="$1" dest="$2"
    if [[ ! -e "$dest" ]] || [[ "$(realpath "$src")" != "$(realpath "$dest")" ]]; then
        cp "$src" "$dest"
    fi
    chmod +x "$dest"
}

find_script() {
    local name="$1"
    for candidate in \
        "${SRC_DIR}/${name}" \
        "${APP_DIR}/scripts/${name}" \
        "./scripts/${name}" \
        "./${name}"; do
        if [[ -f "$candidate" ]]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

find_build_sh() {
    for candidate in \
        "${SRC_DIR}/../build.sh" \
        "${APP_DIR}/build.sh" \
        "./build.sh"; do
        if [[ -f "$candidate" ]]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

for script in upgrade.sh run_container.sh install_watcher.sh; do
    if src=$(find_script "$script"); then
        install_file "$src" "${SCRIPTS_DIR}/${script}"
        ok "Installed ${script} -> ${SCRIPTS_DIR}/${script}"
    else
        err "Could not locate ${script} — clone the repo first:"
        err "  git clone https://github.com/oneofthemany/ZigBee-Matter-Manager.git $APP_DIR"
        exit 1
    fi
done

HAVE_OS_UPDATES=false
if src=$(find_script "os_updates.sh"); then
    install_file "$src" "${SCRIPTS_DIR}/os_updates.sh"
    HAVE_OS_UPDATES=true
    ok "Installed os_updates.sh -> ${SCRIPTS_DIR}/os_updates.sh"
else
    warn "os_updates.sh not found — host OS update checks will be unavailable."
fi

HAVE_OS_APPLY=false
if src=$(find_script "os_apply.sh"); then
    install_file "$src" "${SCRIPTS_DIR}/os_apply.sh"
    HAVE_OS_APPLY=true
    ok "Installed os_apply.sh -> ${SCRIPTS_DIR}/os_apply.sh"
else
    warn "os_apply.sh not found — host OS updates stay view-only."
fi

HAVE_BK_FIREWALL=false
if src=$(find_script "beekeeper_firewall.sh"); then
    install_file "$src" "${SCRIPTS_DIR}/beekeeper_firewall.sh"
    HAVE_BK_FIREWALL=true
    ok "Installed beekeeper_firewall.sh -> ${SCRIPTS_DIR}/beekeeper_firewall.sh"
else
    warn "beekeeper_firewall.sh not found — Beekeeper firewall button will be a no-op."
fi

mkdir -p "${DATA_DIR}/data/os_updates"
mkdir -p "${DATA_DIR}/data/beekeeper"

if build_src=$(find_build_sh); then
    mkdir -p "$APP_DIR"
    install_file "$build_src" "${APP_DIR}/build.sh"
    install_file "$build_src" "${SCRIPTS_DIR}/build.sh"
    ok "Installed build.sh -> ${APP_DIR}/build.sh and ${SCRIPTS_DIR}/build.sh"
else
    warn "build.sh not found — run_container.sh sources it at runtime, upgrades may fail."
fi

# ── Mechanism selection ──────────────────────────────────────────────────────
USE_SYSTEMD_SYSTEM=false
USE_POLLING=false

if command -v systemctl >/dev/null 2>&1; then
    USE_SYSTEMD_SYSTEM=true
else
    USE_POLLING=true
fi

# ── systemd system: same pattern but as root ─────────────────────────────────
install_systemd_system() {
    local unit_dir="/etc/systemd/system"

    sudo tee "$unit_dir/zmm-upgrade.service" >/dev/null <<SERVICE
[Unit]
Description=ZMM Upgrade Worker (oneshot)
# WATCHER_SCHEMA_VERSION=${WATCHER_SCHEMA_VERSION}
# (read by upgrade.sh's self_heal_helpers; bump install_watcher.sh's
#  WATCHER_SCHEMA_VERSION constant when changing helper-script behaviour)
After=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=20

[Service]
Type=oneshot
# No User= — runs as root. Rootful podman (USB coordinator + OTBR) needs it.
# CRITICAL: the watcher starts the app + manager containers as its children.
# With the default KillMode=control-group, systemd kills every process left in
# this service's cgroup — including the containers' conmon — the instant the
# oneshot exits, so both containers die (exit 0) seconds after a successful
# swap. KillMode=process makes systemd reap only the main upgrade.sh process on
# exit and leave the containers (conmon reparented) running.
KillMode=process
ExecStart=${SCRIPTS_DIR}/upgrade.sh
Environment=ZMM_DATA_DIR=${DATA_DIR}
Environment=ZMM_APP_DIR=${APP_DIR}
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
SuccessExitStatus=0 1 2 3
# A full image build takes 15-25 min on ARM. Disable the oneshot timeout
# so systemd doesn't SIGKILL us mid-build and leave a stale lock.
TimeoutStartSec=infinity

[Install]
WantedBy=multi-user.target
SERVICE

    sudo tee "$unit_dir/zmm-upgrade.path" >/dev/null <<PATHUNIT
[Unit]
Description=Watch for ZMM upgrade triggers

[Path]
# PathChanged fires once when the trigger file is closed after writing.
# Note: do NOT add MakeDirectory=true here — that would cause systemd to
# create the trigger path as a directory, breaking everything.
PathChanged=${UPGRADE_DIR}/trigger
Unit=zmm-upgrade.service

[Install]
WantedBy=multi-user.target
PATHUNIT

    if $HAVE_OS_UPDATES; then
        sudo tee "$unit_dir/zmm-os-updates.service" >/dev/null <<SERVICE
[Unit]
Description=ZMM OS Updates Collector (oneshot, read-only)
# WATCHER_SCHEMA_VERSION=${WATCHER_SCHEMA_VERSION}
After=network-online.target

[Service]
Type=oneshot
# No User= — runs as root. Rootful podman (USB coordinator + OTBR) needs it.
ExecStart=${SCRIPTS_DIR}/os_updates.sh
Environment=ZMM_DATA_DIR=${DATA_DIR}
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
SuccessExitStatus=0 1 2 3
# Metadata refresh can be slow on tiny boards; don't let a hang pile up.
TimeoutStartSec=900
SERVICE

        sudo tee "$unit_dir/zmm-os-updates.timer" >/dev/null <<TIMER
[Unit]
Description=Periodic ZMM OS update check

[Timer]
OnBootSec=5min
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
TIMER

        sudo tee "$unit_dir/zmm-os-updates.path" >/dev/null <<PATHUNIT
[Unit]
Description=Watch for ZMM on-demand OS update checks

[Path]
# The :8001 manager writes this file when the user clicks "Check now".
PathChanged=${DATA_DIR}/data/os_updates/refresh
Unit=zmm-os-updates.service

[Install]
WantedBy=multi-user.target
PATHUNIT
    fi

    if $HAVE_OS_APPLY; then
        sudo tee "$unit_dir/zmm-os-apply.service" >/dev/null <<SERVICE
[Unit]
Description=ZMM OS Apply Worker (oneshot — package updates / release upgrade)
# WATCHER_SCHEMA_VERSION=${WATCHER_SCHEMA_VERSION}
After=network-online.target

[Service]
Type=oneshot
ExecStart=${SCRIPTS_DIR}/os_apply.sh
Environment=ZMM_DATA_DIR=${DATA_DIR}
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
SuccessExitStatus=0 1 2 3
# A release upgrade downloads a whole distro — no timeout.
TimeoutStartSec=infinity
SERVICE

        sudo tee "$unit_dir/zmm-os-apply.path" >/dev/null <<PATHUNIT
[Unit]
Description=Watch for ZMM OS apply / release-upgrade triggers

[Path]
# The :8001 manager writes these to apply updates / upgrade the OS release.
PathChanged=${DATA_DIR}/data/os_updates/apply
PathChanged=${DATA_DIR}/data/os_updates/release_upgrade
Unit=zmm-os-apply.service

[Install]
WantedBy=multi-user.target
PATHUNIT
    fi

    if $HAVE_BK_FIREWALL; then
        sudo tee "$unit_dir/zmm-beekeeper-firewall.service" >/dev/null <<SERVICE
[Unit]
Description=ZMM Beekeeper firewall helper (oneshot — open/check DNS :53)
# WATCHER_SCHEMA_VERSION=${WATCHER_SCHEMA_VERSION}
After=network-online.target

[Service]
Type=oneshot
ExecStart=${SCRIPTS_DIR}/beekeeper_firewall.sh
Environment=ZMM_DATA_DIR=${DATA_DIR}
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
SuccessExitStatus=0 1
TimeoutStartSec=120
SERVICE

        sudo tee "$unit_dir/zmm-beekeeper-firewall.path" >/dev/null <<PATHUNIT
[Unit]
Description=Watch for ZMM Beekeeper firewall triggers

[Path]
# The :8001 manager writes this to open/re-check DNS port 53.
PathChanged=${DATA_DIR}/data/beekeeper/firewall_action
Unit=zmm-beekeeper-firewall.service

[Install]
WantedBy=multi-user.target
PATHUNIT
    fi

    sudo systemctl daemon-reload
    sudo systemctl enable --now zmm-upgrade.path
    ok "systemd system path unit enabled (event-driven)"
    if $HAVE_OS_UPDATES; then
        sudo systemctl enable --now zmm-os-updates.timer zmm-os-updates.path
        ok "OS updates collector enabled (6h timer + on-demand path unit)"
    fi
    if $HAVE_OS_APPLY; then
        sudo systemctl enable --now zmm-os-apply.path
        ok "OS apply worker enabled (runs as root via system unit)"
    fi
    if $HAVE_BK_FIREWALL; then
        sudo systemctl enable --now zmm-beekeeper-firewall.path
        ok "Beekeeper firewall helper enabled (runs as root via system unit)"
    fi
}

# ── Polling fallback: simple systemd-free watcher ────────────────────────────
install_polling() {
    local poll_script="${SCRIPTS_DIR}/zmm-upgrade-poll.sh"
    cat > "$poll_script" <<'POLL'
#!/bin/bash
# Polling watcher — runs upgrade.sh every N seconds if a trigger exists.
set -u
DATA_DIR="${ZMM_DATA_DIR:-/opt/.zigbee-matter-manager}"
APP_DIR="${ZMM_APP_DIR:-/opt/.zigbee-matter-manager/upgrade_build}"
UPGRADE_SH="${DATA_DIR}/scripts/upgrade.sh"
TRIGGER="${DATA_DIR}/data/upgrade/trigger"
OS_UPDATES_SH="${DATA_DIR}/scripts/os_updates.sh"
OS_APPLY_SH="${DATA_DIR}/scripts/os_apply.sh"
OS_JSON="${DATA_DIR}/data/os_updates.json"
OS_TRIGGER="${DATA_DIR}/data/os_updates/refresh"
OS_APPLY_TRIGGER="${DATA_DIR}/data/os_updates/apply"
OS_RELEASE_TRIGGER="${DATA_DIR}/data/os_updates/release_upgrade"
OS_INTERVAL=21600   # re-check the OS for updates every 6h
INTERVAL=5

while true; do
    if [[ -f "$TRIGGER" ]]; then
        ZMM_DATA_DIR="$DATA_DIR" ZMM_APP_DIR="$APP_DIR" bash "$UPGRADE_SH" || true
    fi
    # OS updates: on demand (manager wrote the refresh trigger) or every 6h.
    if [[ -x "$OS_UPDATES_SH" ]]; then
        last=$(stat -c %Y "$OS_JSON" 2>/dev/null || echo 0)
        if [[ -f "$OS_TRIGGER" ]] || (( $(date +%s) - last > OS_INTERVAL )); then
            ZMM_DATA_DIR="$DATA_DIR" bash "$OS_UPDATES_SH" || true
        fi
    fi
    # OS apply / release upgrade: on demand only.
    if [[ -x "$OS_APPLY_SH" ]] \
       && { [[ -f "$OS_APPLY_TRIGGER" ]] || [[ -f "$OS_RELEASE_TRIGGER" ]]; }; then
        ZMM_DATA_DIR="$DATA_DIR" bash "$OS_APPLY_SH" || true
    fi
    sleep "$INTERVAL"
done
POLL
    chmod +x "$poll_script"

    if [[ "$(id -u)" -eq 0 ]] && command -v systemctl >/dev/null 2>&1; then
        cat > /etc/systemd/system/zmm-upgrade-poll.service <<SVC
[Unit]
Description=ZMM Upgrade Polling Watcher
# WATCHER_SCHEMA_VERSION=${WATCHER_SCHEMA_VERSION}
# (read by upgrade.sh's self_heal_helpers)
After=network-online.target

[Service]
ExecStart=${poll_script}
Restart=always
RestartSec=5
# No User= — runs as root. Rootful podman (USB coordinator + OTBR) needs it.

[Install]
WantedBy=multi-user.target
SVC
        systemctl daemon-reload
        systemctl enable --now zmm-upgrade-poll.service
        ok "Polling watcher enabled via systemd (system)"
        return
    fi

    local pidfile="${DATA_DIR}/upgrade-poll.pid"
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        ok "Polling watcher already running (PID $(cat "$pidfile"))"
    else
        nohup "$poll_script" >"${LOG_DIR}/upgrade-poll.log" 2>&1 &
        echo $! > "$pidfile"
        ok "Polling watcher started (PID $!)"
    fi

    if command -v crontab >/dev/null 2>&1; then
        local current_cron
        current_cron=$(crontab -l 2>/dev/null || true)
        if ! echo "$current_cron" | grep -q "zmm-upgrade-poll.sh"; then
            (echo "$current_cron"; echo "@reboot $poll_script >> ${LOG_DIR}/upgrade-poll.log 2>&1") | crontab -
            ok "Added @reboot cron entry for polling watcher"
        fi
    fi
}

# ── Install based on detected mechanism ──────────────────────────────────────
if $USE_SYSTEMD_SYSTEM; then
    info "Detected: root systemd → using path-based watcher"
    install_systemd_system
elif $USE_POLLING; then
    info "Detected: no systemd available → using polling watcher"
    install_polling
else
    err "Unable to determine watcher mechanism"
    exit 1
fi

# ── Drop marker so the app knows watcher is ready ────────────────────────────
touch "${UPGRADE_DIR}/.watcher_installed"

# ── Seed VERSION state if missing ────────────────────────────────────────────
if [[ ! -f "${STATE_DIR}/version.json" ]]; then
    RUNTIME=""
    if command -v podman >/dev/null 2>&1; then RUNTIME=podman; fi
    if [[ -z "$RUNTIME" ]] && command -v docker >/dev/null 2>&1; then RUNTIME=docker; fi

    CUR_VER="unknown"
    if [[ -n "$RUNTIME" ]]; then
        CUR_VER=$("$RUNTIME" exec zigbee-matter-manager cat /app/VERSION 2>/dev/null | tr -d '[:space:]' || echo "unknown")
        [[ -z "$CUR_VER" ]] && CUR_VER="unknown"
    fi

    cat > "${STATE_DIR}/version.json" <<JSON
{
  "current_version": "${CUR_VER}",
  "upgrade_state": "idle",
  "auto_update": false,
  "channel": "patch",
  "retention_count": 2,
  "watcher_installed": true
}
JSON
    ok "Seeded version.json with current_version=${CUR_VER}"
fi

echo
ok "${BOLD}Watcher installation complete${NC}"
echo
info "Triggers will be watched at:  ${UPGRADE_DIR}/trigger"
info "Status will be written to:    ${UPGRADE_DIR}/status.json"
info "Build log will be written to: ${UPGRADE_DIR}/build.log"
info "Watcher log:                  ${LOG_DIR}/upgrade_watcher.log"
echo
info "Test the trigger mechanism by opening the Settings tab → Upgrade in the UI."