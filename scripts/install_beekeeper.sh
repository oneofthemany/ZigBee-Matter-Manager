#!/bin/bash
# =============================================================================
# Beekeeper sidecar installer — the ZMM DNS ad/tracker blocker.
#
# Runs the always-on Beekeeper resolver as a SEPARATE container from the same
# app image (which already has the beekeeper/ package + uvicorn/httpx), as
# `python -m beekeeper`, with its own systemd unit (Restart=always) so a restart
# or upgrade of the main app never drops household DNS.
#
# Uses host networking so the sinkhole can serve the whole LAN on <LAN-IP>:53.
# That coexists with systemd-resolved, which by default listens only on
# 127.0.0.53:53 — Beekeeper binds the host's LAN address instead (see
# docs/beekeeper.md). This script detects the rare full-conflict case and prints
# the one-line resolved fix rather than changing host DNS behind your back.
#
# Idempotent: re-running recreates the container and refreshes the unit.
# Mirrors build.sh's run_manager_container()/install_manager_autostart().
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info() { echo -e "${CYAN}${BOLD}[INFO]${NC} $*"; }
ok()   { echo -e "${GREEN}${BOLD}[ OK ]${NC} $*"; }
warn() { echo -e "${YELLOW}${BOLD}[WARN]${NC} $*"; }
die()  { echo -e "${RED}${BOLD}[ERR ]${NC} $*" >&2; exit 1; }

DATA_DIR="${ZMM_DATA_DIR:-/opt/.zigbee-matter-manager}"
IMAGE_NAME="${ZMM_IMAGE_NAME:-zigbee-matter-manager}"
APP_CONTAINER="${ZMM_CONTAINER_NAME:-zigbee-matter-manager}"
BEEKEEPER_CONTAINER="${APP_CONTAINER}-beekeeper"
CONTROL_PORT="${ZMM_BEEKEEPER_CONTROL_PORT:-8053}"

RUNTIME="$(command -v podman || command -v docker || true)"
[[ -n "$RUNTIME" ]] || die "Neither podman nor docker found on PATH."
RUNTIME_NAME="$(basename "$RUNTIME")"
[[ "$RUNTIME_NAME" == "podman" ]] || warn "Beekeeper is validated on podman; docker is best-effort."

# Resolve the app image ref (prefer an existing tagged image).
APP_IMAGE="${ZMM_APP_IMAGE:-}"
if [[ -z "$APP_IMAGE" ]]; then
    if "$RUNTIME" image exists "${IMAGE_NAME}:latest" 2>/dev/null; then
        APP_IMAGE="${IMAGE_NAME}:latest"
    else
        APP_IMAGE="$("$RUNTIME" images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
            | grep "^${IMAGE_NAME}:" | head -1 || true)"
    fi
fi
[[ -n "$APP_IMAGE" ]] || die "Could not find the ${IMAGE_NAME} image. Install ZMM first (build.sh)."
info "Using app image: ${APP_IMAGE}"

# ── Port-53 sanity: warn if something ALREADY answers on the host's LAN IP ────
LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
if [[ -n "$LAN_IP" ]]; then
    info "Detected primary LAN IP: ${LAN_IP} (Beekeeper will bind ${LAN_IP}:53)"
    if ss -H -lun "sport = :53" 2>/dev/null | awk '{print $5}' | grep -qE "(^|[^0-9.])${LAN_IP//./\\.}:53$|(^|[^0-9.])0\.0\.0\.0:53$|\*:53$"; then
        warn "Something already listens on :53 for ${LAN_IP} (often systemd-resolved's"
        warn "stub bound to 0.0.0.0). If Beekeeper fails to bind, disable ONLY the stub:"
        echo  "    sudo mkdir -p /etc/systemd/resolved.conf.d"
        echo  "    printf '[Resolve]\\nDNSStubListener=no\\n' | sudo tee /etc/systemd/resolved.conf.d/beekeeper.conf"
        echo  "    sudo systemctl restart systemd-resolved"
        echo  "  (revert by deleting that file and restarting systemd-resolved)"
    fi
fi

# ── systemd unit so the sidecar returns on reboot ────────────────────────────
install_autostart() {
    command -v systemctl >/dev/null 2>&1 || return 0
    [[ "$RUNTIME_NAME" == "podman" ]] || return 0
    local unit_file="/etc/systemd/system/${BEEKEEPER_CONTAINER}.service"
    sudo tee "$unit_file" > /dev/null << UNIT
[Unit]
Description=ZMM Beekeeper DNS sinkhole (${BEEKEEPER_CONTAINER})
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Restart=always
RestartSec=10
# The container may already exist (created by this installer); start/stop it by name.
ExecStart=${RUNTIME} start -a ${BEEKEEPER_CONTAINER}
ExecStop=${RUNTIME} stop -t 10 ${BEEKEEPER_CONTAINER}

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable "${BEEKEEPER_CONTAINER}.service" >/dev/null 2>&1 || true
}

# ── (Re)create the container ─────────────────────────────────────────────────
if "$RUNTIME" inspect "$BEEKEEPER_CONTAINER" &>/dev/null; then
    info "Removing existing ${BEEKEEPER_CONTAINER}…"
    "$RUNTIME" rm -f "$BEEKEEPER_CONTAINER" >/dev/null 2>&1 || true
fi

args=(
    --name "$BEEKEEPER_CONTAINER"
    --network host                      # bind the host LAN IP on :53 for the whole network
    --security-opt label=disable
    --no-healthcheck
    --restart always
    --volume "${DATA_DIR}:${DATA_DIR}"
    --env "ZMM_DATA_DIR=${DATA_DIR}"
    --env "ZMM_CONTAINER_NAME=${APP_CONTAINER}"
)

info "Creating Beekeeper sidecar '${BEEKEEPER_CONTAINER}' (host network, control :${CONTROL_PORT})…"
if command -v systemctl >/dev/null 2>&1 && [[ "$RUNTIME_NAME" == "podman" ]]; then
    "$RUNTIME" create "${args[@]}" "$APP_IMAGE" python -m beekeeper >/dev/null
    install_autostart
    if sudo systemctl restart "${BEEKEEPER_CONTAINER}.service"; then
        ok "Beekeeper running under ${BEEKEEPER_CONTAINER}.service (Restart=always)."
    else
        warn "systemctl start failed — starting the container directly."
        "$RUNTIME" start "$BEEKEEPER_CONTAINER" >/dev/null
    fi
else
    "$RUNTIME" run --detach "${args[@]}" "$APP_IMAGE" python -m beekeeper >/dev/null
    ok "Beekeeper sidecar started."
fi

cat <<EOF

${BOLD}Next steps${NC}
  1. Open ZMM → ${BOLD}Beekeeper${NC} tab and flip ${BOLD}Enabled${NC} on.
     (First enable binds ${LAN_IP:-<LAN-IP>}:53 and pulls the blocklists.)
  2. Point your router's DHCP ${BOLD}DNS server${NC} at ${LAN_IP:-<this host's LAN IP>}
     so every device resolves through Beekeeper.
  3. Verify from another machine:  dig @${LAN_IP:-<LAN-IP>} doubleclick.net
     A blocked domain should answer 0.0.0.0 (or NXDOMAIN).

Full guide: docs/beekeeper.md
EOF
