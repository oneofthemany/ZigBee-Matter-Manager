#!/bin/bash
# =============================================================================
# ZMM Beekeeper autostart helper (runs ON THE HOST as root).
# =============================================================================
# The manager creates the Beekeeper container over the runtime socket and can't
# reach the host's service manager, so this installs, removes or checks the
# boot-time service for it. Beekeeper is the LAN's DNS: if it doesn't come back
# after a reboot or outage, every client loses name resolution.
#
# Backends, detected: systemd unit, OpenRC init script, or neither — in which
# case the container's own restart policy (unless-stopped) is all there is, and
# the status says so. Runtime: podman, else docker.
set -u

DATA_DIR="${ZMM_DATA_DIR:-/opt/.zigbee-matter-manager}"
BK_DIR="${DATA_DIR}/data/beekeeper"
TRIGGER="${BK_DIR}/service_action"
STATUS="${BK_DIR}/service_status.json"
LOG="${DATA_DIR}/logs/beekeeper_service.log"
NAME="${ZMM_CONTAINER_NAME:-zigbee-matter-manager}-beekeeper"
SERVICE="zmm-beekeeper"
# Overridable so tests can point at temp dirs.
SYSTEMD_DIR="${ZMM_SYSTEMD_DIR:-/etc/systemd/system}"
SYSTEMD_RUN="${ZMM_SYSTEMD_RUN:-/run/systemd/system}"
INITD_DIR="${ZMM_INITD_DIR:-/etc/init.d}"
OPENRC_RUN="${ZMM_OPENRC_RUN:-/run/openrc}"

mkdir -p "$BK_DIR" "${DATA_DIR}/logs" 2>/dev/null || true
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG" 2>/dev/null || true; }

ACTION="check"
if [[ -f "$TRIGGER" ]]; then
    ACTION="$(tr -d '[:space:]' < "$TRIGGER" 2>/dev/null)"
    rm -f "$TRIGGER" 2>/dev/null || true
fi
case "$ACTION" in install|remove|check) ;; *) log "ignoring unknown action '$ACTION'"; ACTION="check" ;; esac

SUDO=""
if [[ ${EUID:-$(id -u)} -ne 0 ]] && command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    SUDO="sudo -n"
fi
runp() { $SUDO "$@"; }

json_str() { printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"; }

write_status() {   # backend unit installed enabled active conflict detail
    local tmp="${STATUS}.tmp"
    printf '{"backend":%s,"unit":%s,"installed":%s,"enabled":%s,"active":%s,"conflict":%s,"action":%s,"detail":%s,"updated_at":%s}\n' \
        "$(json_str "$1")" "$(json_str "$2")" "$3" "$4" "$5" "$(json_str "$6")" \
        "$(json_str "$ACTION")" "$(json_str "$7")" "$(json_str "$(date -u +%Y-%m-%dT%H:%M:%SZ)")" \
        > "$tmp" 2>>"$LOG" && mv -f "$tmp" "$STATUS"
}

RUNTIME=""
for rt in ${ZMM_RUNTIMES:-podman docker}; do
    if command -v "$rt" >/dev/null 2>&1; then RUNTIME="$(command -v "$rt")"; break; fi
done

BACKEND="none"
if [[ -d "$SYSTEMD_RUN" ]] && command -v systemctl >/dev/null 2>&1; then
    BACKEND="systemd"
elif [[ -d "$OPENRC_RUN" ]] && command -v rc-update >/dev/null 2>&1; then
    BACKEND="openrc"
fi

if [[ -z "$RUNTIME" ]]; then
    write_status "$BACKEND" "" false false false "" "no podman or docker on the host"
    log "$ACTION: no container runtime found"
    exit 0
fi

# Write a file only when its content changes; returns 0 if it changed.
write_if_changed() {   # path mode content
    if [[ -f "$1" ]] && [[ "$(cat "$1")" == "$3" ]]; then return 1; fi
    printf '%s\n' "$3" | runp tee "$1" >/dev/null && runp chmod "$2" "$1"
}

# ── systemd ──────────────────────────────────────────────────────────────────
SD_UNIT="${SYSTEMD_DIR}/${SERVICE}.service"
sd_unit_text() {
    cat <<UNIT
[Unit]
Description=ZMM Beekeeper DNS sidecar (${NAME})
# Written by beekeeper_service.sh when Beekeeper is enabled, removed when it's disabled.
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Restart=always
RestartSec=10
ExecStartPre=-${RUNTIME} stop -t 10 ${NAME}
ExecStart=${RUNTIME} start -a ${NAME}
ExecStop=${RUNTIME} stop -t 10 ${NAME}
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
UNIT
}
# Another unit already supervising the container (e.g. one written by hand):
# two Restart=always units on one container would fight.
sd_conflicts() {
    local f
    for f in "$SYSTEMD_DIR"/*.service; do
        [[ -f "$f" && "$f" != "$SD_UNIT" ]] || continue
        grep -qE "(start|run)[^#]*[[:space:]]${NAME}([[:space:]]|$)" "$f" && basename "$f"
    done | paste -sd, - | sed 's/,/, /g'
}
sd_state() {   # -> "installed enabled active"
    local i=false e=false a=false
    [[ -f "$SD_UNIT" ]] && i=true
    runp systemctl is-enabled --quiet "${SERVICE}.service" 2>/dev/null && e=true
    runp systemctl is-active --quiet "${SERVICE}.service" 2>/dev/null && a=true
    echo "$i $e $a"
}

# ── OpenRC ───────────────────────────────────────────────────────────────────
RC_SCRIPT="${INITD_DIR}/${SERVICE}"
rc_script_text() {
    cat <<SCRIPT
#!/sbin/openrc-run
# Written by beekeeper_service.sh when Beekeeper is enabled, removed when it's disabled.
description="ZMM Beekeeper DNS sidecar (${NAME})"
supervisor=supervise-daemon
command="${RUNTIME}"
command_args="start -a ${NAME}"
respawn_delay=10
respawn_max=0

depend() {
    need net
}

stop_post() {
    ${RUNTIME} stop -t 10 ${NAME} >/dev/null 2>&1 || true
}
SCRIPT
}
rc_conflicts() {
    local f
    for f in "$INITD_DIR"/*; do
        [[ -f "$f" && "$f" != "$RC_SCRIPT" ]] || continue
        grep -qE "(start|run)[^#]*[[:space:]]${NAME}([[:space:]\"]|$)" "$f" && basename "$f"
    done | paste -sd, - | sed 's/,/, /g'
}
rc_state() {
    local i=false e=false a=false
    [[ -f "$RC_SCRIPT" ]] && i=true
    runp rc-update show default 2>/dev/null | grep -qE "^[[:space:]]*${SERVICE}[[:space:]]" && e=true
    runp rc-service "$SERVICE" status >/dev/null 2>&1 && a=true
    echo "$i $e $a"
}

report() {   # detail
    local st unit conflict=""
    case "$BACKEND" in
        systemd) st=$(sd_state); unit="${SERVICE}.service"; conflict=$(sd_conflicts) ;;
        openrc)  st=$(rc_state); unit="$SERVICE"; conflict=$(rc_conflicts) ;;
        *)       st="false false false"; unit="" ;;
    esac
    # shellcheck disable=SC2086
    write_status "$BACKEND" "$unit" $st "$conflict" "$1"
    log "$ACTION ($BACKEND): $1"
}

case "$BACKEND:$ACTION" in
    systemd:install)
        CONFLICT=$(sd_conflicts)
        if [[ -n "$CONFLICT" ]]; then
            report "not installed: ${CONFLICT} already manages ${NAME} — remove it to let ZMM manage autostart, or keep it"
            exit 0
        fi
        CHANGED=false
        write_if_changed "$SD_UNIT" 644 "$(sd_unit_text)" && CHANGED=true
        runp systemctl daemon-reload
        runp systemctl enable "${SERVICE}.service" >/dev/null 2>&1
        if $CHANGED || ! runp systemctl is-active --quiet "${SERVICE}.service"; then
            # Takes over the running container: a stop/start, a few seconds without DNS.
            runp systemctl restart "${SERVICE}.service"
        fi
        report "starts at boot and restarts if it stops"
        ;;
    systemd:remove)
        if [[ -f "$SD_UNIT" ]]; then
            runp systemctl disable --now "${SERVICE}.service" >/dev/null 2>&1
            runp rm -f "$SD_UNIT"
            runp systemctl daemon-reload
        fi
        report "autostart removed"
        ;;
    openrc:install)
        CONFLICT=$(rc_conflicts)
        if [[ -n "$CONFLICT" ]]; then
            report "not installed: ${CONFLICT} already manages ${NAME} — remove it to let ZMM manage autostart, or keep it"
            exit 0
        fi
        CHANGED=false
        write_if_changed "$RC_SCRIPT" 755 "$(rc_script_text)" && CHANGED=true
        runp rc-update add "$SERVICE" default >/dev/null 2>&1
        if $CHANGED || ! runp rc-service "$SERVICE" status >/dev/null 2>&1; then
            runp rc-service "$SERVICE" restart >/dev/null 2>&1
        fi
        report "starts at boot and restarts if it stops"
        ;;
    openrc:remove)
        if [[ -f "$RC_SCRIPT" ]]; then
            runp rc-service "$SERVICE" stop >/dev/null 2>&1
            runp rc-update del "$SERVICE" default >/dev/null 2>&1
            runp rm -f "$RC_SCRIPT"
        fi
        report "autostart removed"
        ;;
    none:install|none:remove)
        report "no systemd or OpenRC on this host — relying on the container's restart policy (unless-stopped)"
        ;;
    *:check)
        report "checked"
        ;;
esac
exit 0
