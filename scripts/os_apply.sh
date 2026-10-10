#!/bin/bash
# =============================================================================
# ZMM OS Apply Worker
# =============================================================================
set -u

DATA_DIR="${ZMM_DATA_DIR:-/opt/.zigbee-matter-manager}"
TRIGGER_DIR="${DATA_DIR}/data/os_updates"
APPLY_TRIGGER="${TRIGGER_DIR}/apply"
RELEASE_TRIGGER="${TRIGGER_DIR}/release_upgrade"
REBOOT_TRIGGER="${TRIGGER_DIR}/reboot"
STATUS_FILE="${TRIGGER_DIR}/apply_status.json"
LOCK_DIR="${TRIGGER_DIR}/.apply_lock"
LOG_FILE="${DATA_DIR}/logs/os_apply.log"
COLLECTOR="${DATA_DIR}/scripts/os_updates.sh"
NET_TIMEOUT=3600      # a big dnf/apt transaction can legitimately take a while
OSTREE_BOOTED="${ZMM_OSTREE_BOOTED:-/run/ostree-booted}"
OS_RELEASE_FILE="${ZMM_OS_RELEASE:-/etc/os-release}"

# Packages whose new version only takes effect from boot: the kernel and early
# userspace every process links against. Same set as dnf needs-restarting -r,
# plus ostree/rpm-ostree themselves. Keep in sync with os_updates.sh.
REBOOT_PKGS_RE='^(kernel(-core|-modules|-modules-core|-modules-extra)?|kernel-rt.*|glibc|linux-firmware.*|microcode_ctl|systemd|systemd-libs|systemd-udev|udev|dbus|dbus-broker|dbus-daemon|dbus-libs|openssl-libs|gnutls|zlib|zlib-ng-compat|ostree|ostree-libs|rpm-ostree|rpm-ostree-libs|selinux-policy|selinux-policy-targeted)$'

reboot_blockers() {   # newline-separated package names -> the ones needing a reboot, comma-joined
    printf '%s\n' "$1" | grep -E "$REBOOT_PKGS_RE" | sort -u | paste -sd, - | sed 's/,/, /g'
}

# Keep in sync with os_updates.sh. Recomputed here rather than read from
# os_updates.json, which sits in a directory the containers can write.
rebase_target() {   # origin current new -> origin moved to release `new`, or nothing
    local origin="$1" cur="$2" new="$3"
    if [[ "$origin" == *"/${cur}/"* ]]; then
        printf '%s\n' "${origin/\/${cur}\//\/${new}\/}"
    elif [[ "$origin" == *":${cur}" ]]; then
        printf '%s\n' "${origin%:${cur}}:${new}"
    fi
}

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
elif [[ -f "$REBOOT_TRIGGER" ]]; then
    ACTION="reboot"
    rm -f "$REBOOT_TRIGGER" 2>/dev/null || true
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
if [[ -f "$OSTREE_BOOTED" ]] && command -v rpm-ostree >/dev/null 2>&1; then
    PKG_MANAGER="rpm-ostree"
elif command -v dnf >/dev/null 2>&1; then
    PKG_MANAGER="dnf"
elif command -v apt-get >/dev/null 2>&1; then
    PKG_MANAGER="apt"
fi

log "── $ACTION requested (pm=${PKG_MANAGER:-none} target=${RELEASE_TARGET:-—}) ──"
case "$ACTION" in
    apply)  RUNNING_MSG="applying package updates" ;;
    reboot) RUNNING_MSG="rebooting the host" ;;
    *)      RUNNING_MSG="downloading release upgrade to ${RELEASE_TARGET:-?}" ;;
esac
write_status "running" "$ACTION" "$RUNNING_MSG"

# A staged kernel needs the Coral driver built for it before the reboot into
# it; a no-op where the driver isn't installed.
coral_prebuild() {
    local coral="${DATA_DIR}/scripts/coral_driver.sh"
    [[ -x "$coral" ]] && ZMM_DATA_DIR="$DATA_DIR" bash "$coral" prebuild >> "$LOG_FILE" 2>&1
    return 0
}

reboot_host() {   # detail
    coral_prebuild
    write_status "rebooting" "$ACTION" "$1"
    log "rebooting: $1"
    sync
    $SUDO systemctl reboot >> "$LOG_FILE" 2>&1
    exit 0   # (unreachable if the reboot proceeds)
}

RC=1
DONE_DETAIL=""        # set by a branch that has something better to say than "completed"
case "$ACTION:$PKG_MANAGER" in
    reboot:*)
        reboot_host "rebooting at your request — the host and every container will be down for a few minutes"
        ;;
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
        if [[ $RC -eq 0 ]]; then
            ST=$(rpm-ostree status --json 2>>"$LOG_FILE")
            FROM=$(jq -r '[.deployments[] | select(.booted)][0].checksum // ""' <<<"$ST")
            TO=$(jq -r 'if (.deployments[0].booted | not) then .deployments[0].checksum else "" end' <<<"$ST")
            if [[ -z "$TO" ]]; then
                DONE_DETAIL="already up to date — nothing new to deploy"
            else
                CHANGED=$(rpm-ostree db diff --format=json "$FROM" "$TO" 2>>"$LOG_FILE" | jq -r '.pkgdiff[]?[0]')
                BLOCKERS=$(reboot_blockers "$CHANGED")
                if [[ -n "$BLOCKERS" ]]; then
                    DONE_DETAIL="update staged — reboot to apply (needs a reboot for: $BLOCKERS)"
                    log "not applying live: $BLOCKERS"
                else
                    LIVE=(rpm-ostree apply-live --allow-replacement)
                    rpm-ostree apply-live --help >/dev/null 2>&1 || LIVE=(rpm-ostree ex apply-live --allow-replacement)
                    if run_logged $SUDO "${LIVE[@]}"; then
                        DONE_DETAIL="applied live — no reboot needed"
                    else
                        DONE_DETAIL="update staged — live apply failed, reboot to apply"
                    fi
                fi
            fi
        fi
        ;;
    release_upgrade:rpm-ostree)
        CURRENT=$( [[ -r "$OS_RELEASE_FILE" ]] && . "$OS_RELEASE_FILE" && echo "${VERSION_ID:-}")
        if [[ ! "$RELEASE_TARGET" =~ ^[0-9]+$ || ! "$CURRENT" =~ ^[0-9]+$ ]] \
           || (( RELEASE_TARGET <= CURRENT || RELEASE_TARGET > CURRENT + 2 )); then
            write_status "failed" "$ACTION" "refusing release ${RELEASE_TARGET:-?} from ${CURRENT:-?} (must be one or two releases ahead)"
            exit 0
        fi
        ORIGIN=$(rpm-ostree status --json 2>>"$LOG_FILE" \
            | jq -r '[.deployments[] | select(.booted)][0] | (."container-image-reference" // .origin // "")')
        NEW_REF=$(rebase_target "$ORIGIN" "$CURRENT" "$RELEASE_TARGET")
        if [[ -z "$NEW_REF" ]]; then
            write_status "failed" "$ACTION" "can't work out the release $RELEASE_TARGET equivalent of '$ORIGIN' — rebase manually"
            exit 0
        fi
        run_logged $SUDO rpm-ostree rebase "$NEW_REF"; RC=$?
        if [[ $RC -eq 0 ]]; then
            reboot_host "rebased to $NEW_REF — rebooting into Fedora $RELEASE_TARGET; the host will be down for a few minutes"
        fi
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
    log "$ACTION finished OK${DONE_DETAIL:+ — $DONE_DETAIL}"
    write_status "done" "$ACTION" "${DONE_DETAIL:-completed — see os_apply.log for details}"
else
    log "$ACTION FAILED (exit $RC)"
    write_status "failed" "$ACTION" "exit $RC — see os_apply.log for details"
fi

[[ $RC -eq 0 ]] && coral_prebuild

[[ -x "$COLLECTOR" ]] && ZMM_DATA_DIR="$DATA_DIR" bash "$COLLECTOR" || true
exit 0
