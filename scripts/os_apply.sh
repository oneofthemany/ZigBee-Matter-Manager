#!/bin/bash
# =============================================================================
# ZMM OS Apply Worker
# =============================================================================
set -u

DATA_DIR="${ZMM_DATA_DIR:-/opt/.zigbee-matter-manager}"
TRIGGER_DIR="${DATA_DIR}/data/os_updates"
APPLY_TRIGGER="${TRIGGER_DIR}/apply"
RELEASE_TRIGGER="${TRIGGER_DIR}/release_upgrade"
STATUS_FILE="${TRIGGER_DIR}/apply_status.json"
LOCK_DIR="${TRIGGER_DIR}/.apply_lock"
LOG_FILE="${DATA_DIR}/logs/os_apply.log"
COLLECTOR="${DATA_DIR}/scripts/os_updates.sh"
NET_TIMEOUT=3600      # a big dnf/apt transaction can legitimately take a while

mkdir -p "$TRIGGER_DIR" "${DATA_DIR}/logs" 2>/dev/null || true

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE" 2>/dev/null || true; }

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

write_status() {   # state action detail
    local state="$1" action="$2" detail="$3"
    local tmp="${STATUS_FILE}.tmp"
    jq -n --arg state "$state" --arg action "$action" --arg detail "$detail" \
          --arg started "$STARTED_AT" \
          --arg updated "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
          '{state: $state, action: $action, detail: $detail,
            started_at: $started, updated_at: $updated}' \
        > "$tmp" 2>>"$LOG_FILE" && mv -f "$tmp" "$STATUS_FILE"
}

if ! command -v jq >/dev/null 2>&1; then
    log "jq missing — cannot run"
    exit 0
fi

# ── Consume triggers up front (consume-first rule, like upgrade.sh) ─────────
ACTION=""
RELEASE_TARGET=""
if [[ -f "$RELEASE_TRIGGER" ]]; then
    ACTION="release_upgrade"
    RELEASE_TARGET=$(head -c 32 "$RELEASE_TRIGGER" 2>/dev/null | tr -cd '0-9.')
    rm -f "$RELEASE_TRIGGER" 2>/dev/null || true
    rm -f "$APPLY_TRIGGER" 2>/dev/null || true   # superset — no point doing both
elif [[ -f "$APPLY_TRIGGER" ]]; then
    ACTION="apply"
    rm -f "$APPLY_TRIGGER" 2>/dev/null || true
else
    exit 0
fi

# ── Single instance ──────────────────────────────────────────────────────────
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    if [[ -n "$(find "$LOCK_DIR" -maxdepth 0 -mmin +120 2>/dev/null)" ]]; then
        log "clearing stale apply lock"
        rmdir "$LOCK_DIR" 2>/dev/null || true
        mkdir "$LOCK_DIR" 2>/dev/null || exit 0
    else
        log "another apply is already running — ignoring $ACTION trigger"
        exit 0
    fi
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

# ── Privileges ───────────────────────────────────────────────────────────────
SUDO=""
if [[ "$(id -u)" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        SUDO="sudo -n"
    else
        MSG="needs root: re-run install_watcher.sh as root (system units) or grant passwordless sudo to $(id -un)"
        log "$ACTION failed — $MSG"
        write_status "failed" "$ACTION" "$MSG"
        exit 0
    fi
fi

run_logged() {
    log "+ $*"
    timeout "$NET_TIMEOUT" "$@" >> "$LOG_FILE" 2>&1
}

PKG_MANAGER=""
if [[ -f /run/ostree-booted ]] && command -v rpm-ostree >/dev/null 2>&1; then
    PKG_MANAGER="rpm-ostree"
elif command -v dnf >/dev/null 2>&1; then
    PKG_MANAGER="dnf"
elif command -v apt-get >/dev/null 2>&1; then
    PKG_MANAGER="apt"
fi

log "── $ACTION requested (pm=${PKG_MANAGER:-none} target=${RELEASE_TARGET:-—}) ──"
write_status "running" "$ACTION" \
    "$([[ $ACTION == apply ]] && echo 'applying package updates' \
       || echo "downloading release upgrade to ${RELEASE_TARGET:-?}")"

RC=1
case "$ACTION:$PKG_MANAGER" in
    apply:dnf)
        run_logged $SUDO dnf -y --refresh upgrade; RC=$?
        ;;
    apply:apt)
        run_logged $SUDO apt-get update && \
        DEBIAN_FRONTEND=noninteractive run_logged $SUDO apt-get -y \
            -o Dpkg::Options::=--force-confdef \
            -o Dpkg::Options::=--force-confold full-upgrade; RC=$?
        ;;
    apply:rpm-ostree)
        run_logged $SUDO rpm-ostree upgrade; RC=$?
        ;;
    release_upgrade:dnf)
        if [[ -z "$RELEASE_TARGET" ]]; then
            write_status "failed" "$ACTION" "no target release in trigger"
            exit 0
        fi
        if ! $SUDO dnf system-upgrade --help >/dev/null 2>&1; then
            run_logged $SUDO dnf -y install dnf5-plugins \
                || run_logged $SUDO dnf -y install dnf-plugin-system-upgrade \
                || true
        fi
        run_logged $SUDO dnf -y system-upgrade download --releasever="$RELEASE_TARGET"
        RC=$?
        if [[ $RC -eq 0 ]]; then
            write_status "rebooting" "$ACTION" \
                "release $RELEASE_TARGET downloaded — rebooting to install; the host will be down for a while"
            log "rebooting into system-upgrade for Fedora $RELEASE_TARGET"
            sync
            if $SUDO dnf offline --help >/dev/null 2>&1; then
                log "using dnf5 offline reboot"
                $SUDO dnf offline reboot >> "$LOG_FILE" 2>&1
            else
                log "using dnf4 system-upgrade reboot"
                $SUDO dnf -y system-upgrade reboot >> "$LOG_FILE" 2>&1
            fi
            exit 0   # (unreachable if the reboot proceeds)
        fi
        ;;
    release_upgrade:apt)
        if command -v do-release-upgrade >/dev/null 2>&1; then
            DEBIAN_FRONTEND=noninteractive run_logged $SUDO do-release-upgrade \
                -f DistUpgradeViewNonInteractive; RC=$?
        else
            write_status "failed" "$ACTION" "do-release-upgrade not available on this host"
            exit 0
        fi
        ;;
    *)
        write_status "failed" "$ACTION" "unsupported package manager (${PKG_MANAGER:-none})"
        exit 0
        ;;
esac

if [[ $RC -eq 0 ]]; then
    log "$ACTION finished OK"
    write_status "done" "$ACTION" "completed — see os_apply.log for details"
else
    log "$ACTION FAILED (exit $RC)"
    write_status "failed" "$ACTION" "exit $RC — see os_apply.log for details"
fi

[[ -x "$COLLECTOR" ]] && ZMM_DATA_DIR="$DATA_DIR" bash "$COLLECTOR" || true
exit 0
