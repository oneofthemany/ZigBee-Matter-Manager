#!/bin/bash
# =============================================================================
# ZMM OS Updates Collector
#
# Runs ON THE HOST (installed to ${DATA_DIR}/scripts by install_watcher.sh,
# scheduled by zmm-os-updates.timer every 6h + on boot, and re-run on demand
# when the :8001 manager writes ${DATA_DIR}/data/os_updates/refresh — a
# systemd path unit watches that file).
#
# Strictly READ-ONLY: checks the host package manager for pending updates and
# writes ${DATA_DIR}/data/os_updates.json for the manager to surface. It never
# installs, upgrades, or removes anything.
#
# Supports dnf (Fedora/RHEL) and apt (Debian/Ubuntu); anything else reports
# pkg_manager=null with an "unsupported" error so the UI can say so honestly.
# =============================================================================
set -u

DATA_DIR="${ZMM_DATA_DIR:-/opt/.zigbee-matter-manager}"
OUT_FILE="${DATA_DIR}/data/os_updates.json"
TRIGGER_DIR="${DATA_DIR}/data/os_updates"
REFRESH_TRIGGER="${TRIGGER_DIR}/refresh"
LOG_FILE="${DATA_DIR}/logs/os_updates.log"
MAX_PKGS=300          # cap the package list in the JSON (counts stay exact)
NET_TIMEOUT=300       # seconds allowed for metadata refresh / list commands

mkdir -p "${DATA_DIR}/data" "$TRIGGER_DIR" "${DATA_DIR}/logs" 2>/dev/null || true

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE" 2>/dev/null || true; }

# Consume the on-demand trigger (if any) up front, mirroring upgrade.sh's
# consume-first rule so a re-fire during the run isn't lost.
rm -f "$REFRESH_TRIGGER" 2>/dev/null || true

if ! command -v jq >/dev/null 2>&1; then
    log "jq missing — cannot write os_updates.json"
    exit 0
fi

# Non-root best effort: use passwordless sudo when available (apt metadata
# refresh needs root; dnf refreshes to a per-user cache fine without it).
SUDO=""
if [[ "$(id -u)" -ne 0 ]] && command -v sudo >/dev/null 2>&1 \
   && sudo -n true 2>/dev/null; then
    SUDO="sudo -n"
fi

OS_NAME="unknown"
[[ -r /etc/os-release ]] && OS_NAME=$(. /etc/os-release && echo "${PRETTY_NAME:-unknown}")
KERNEL_RUNNING=$(uname -r 2>/dev/null || echo "")
KERNEL_LATEST=$(ls -1 /lib/modules 2>/dev/null | sort -V | tail -1)
UPTIME=$(awk '{print int($1)}' /proc/uptime 2>/dev/null || echo 0)

PKG_MANAGER=""
ERR=""
SECURITY=0
REBOOT=false
PKGS_TSV=""           # name<TAB>current<TAB>candidate

if [[ -f /run/ostree-booted ]] && command -v rpm-ostree >/dev/null 2>&1; then
    # Image-based host (Silverblue / IoT / CoreOS): updates come as one new
    # deployment, not per-package. dnf can't answer here.
    PKG_MANAGER="rpm-ostree"
    log "checking updates via rpm-ostree"
    RAW=$(timeout "$NET_TIMEOUT" rpm-ostree upgrade --check 2>>"$LOG_FILE")
    RC=$?
    if [[ $RC -eq 0 ]]; then
        NEW_VER=$(printf '%s\n' "$RAW" | awk '/Version:/ {print $2; exit}')
        PKGS_TSV=$(printf "ostree deployment\t\t%s\n" "${NEW_VER:-new image}")
        SECURITY=$(printf '%s\n' "$RAW" | grep -c 'SecAdvisories' || true)
    elif [[ $RC -ne 77 ]]; then    # 77 = already up to date
        ERR="rpm-ostree upgrade --check failed (exit $RC)"
        log "$ERR"
    fi
    # A staged-but-not-booted deployment means a reboot finishes the update.
    rpm-ostree status 2>/dev/null | grep -q '(pending)' && REBOOT=true
elif command -v dnf >/dev/null 2>&1; then
    PKG_MANAGER="dnf"
    log "checking updates via dnf"
    # check-update exits 100 when updates exist, 0 when none — both are fine.
    # dnf5 needs root even for read-only checks (system repo lock).
    RAW=$(timeout "$NET_TIMEOUT" $SUDO dnf -q --refresh check-update 2>>"$LOG_FILE")
    RC=$?
    if [[ $RC -ne 0 && $RC -ne 100 ]]; then
        ERR="dnf check-update failed (exit $RC)"
        log "$ERR"
    fi
    # Lines: "name.arch  version  repo"; stop at the "Obsoleting" section.
    PKGS_TSV=$(printf '%s\n' "$RAW" | awk '
        /^Obsoleting/ {exit}
        NF==3 && $1 ~ /\./ {printf "%s\t\t%s\n", $1, $2}')
    SECURITY=$(timeout "$NET_TIMEOUT" $SUDO dnf -q updateinfo list --updates \
        --security 2>/dev/null | grep -c '/' || true)
    # needs-restarting -r exits 1 when a reboot is required (dnf-utils).
    if dnf needs-restarting --help >/dev/null 2>&1; then
        $SUDO dnf needs-restarting -r >/dev/null 2>&1 || REBOOT=true
    fi
elif command -v apt-get >/dev/null 2>&1; then
    PKG_MANAGER="apt"
    log "checking updates via apt"
    # Metadata refresh needs root; without it we still list against the
    # existing (possibly stale) metadata rather than failing outright.
    if [[ -n "$SUDO" || "$(id -u)" -eq 0 ]]; then
        timeout "$NET_TIMEOUT" $SUDO apt-get update -qq 2>>"$LOG_FILE" \
            || { ERR="apt-get update failed (using cached metadata)"; log "$ERR"; }
    else
        ERR="no root/sudo — package list may be stale (apt-get update skipped)"
        log "$ERR"
    fi
    # Lines: "name/suite candidate arch [upgradable from: current]"
    RAW=$(timeout "$NET_TIMEOUT" apt list --upgradable 2>/dev/null | grep upgradable)
    PKGS_TSV=$(printf '%s\n' "$RAW" | awk -F'[/ ]' '
        NF>=3 {cur=""; if (match($0, /upgradable from: [^]]+/))
                   cur=substr($0, RSTART+17, RLENGTH-17);
               printf "%s\t%s\t%s\n", $1, cur, $3}')
    SECURITY=$(printf '%s\n' "$RAW" | grep -ci 'security' || true)
    [[ -f /var/run/reboot-required ]] && REBOOT=true
else
    ERR="unsupported package manager (no dnf or apt found)"
    log "$ERR"
fi

# ── OS release-upgrade availability ─────────────────────────────────────────
# Distro-agnostic within what each distro sanctions:
#   Fedora — probe the mirrorlist for releasever+1 (cheap HTTP HEAD, no dnf
#            metadata download); automated via dnf system-upgrade.
#   Ubuntu — do-release-upgrade -c; automated via do-release-upgrade.
#   Debian — compare the running VERSION_ID with the archive's current
#            stable; DETECTED but flagged manual (Debian ships no official
#            non-interactive release-upgrade tool — it's a sources rewrite).
#   Everything else — null (the UI says nothing rather than guessing).
OS_ID=""
RELEASE_CURRENT=""
RELEASE_AVAILABLE=""
RELEASE_AUTOMATED=false
if [[ -r /etc/os-release ]]; then
    OS_ID=$(. /etc/os-release && echo "${ID:-}")
    RELEASE_CURRENT=$(. /etc/os-release && echo "${VERSION_ID:-}")
fi
if [[ "$OS_ID" == "fedora" && "$RELEASE_CURRENT" =~ ^[0-9]+$ ]] \
   && command -v curl >/dev/null 2>&1; then
    NEXT=$((RELEASE_CURRENT + 1))
    if curl -fsm 20 -o /dev/null \
        "https://mirrors.fedoraproject.org/metalink?repo=fedora-${NEXT}&arch=$(uname -m)" \
        2>>"$LOG_FILE"; then
        RELEASE_AVAILABLE="$NEXT"
        RELEASE_AUTOMATED=true
        log "OS release upgrade available: Fedora $RELEASE_CURRENT -> $NEXT"
    fi
elif [[ "$PKG_MANAGER" == "apt" ]] && command -v do-release-upgrade >/dev/null 2>&1; then
    # Ubuntu (and derivatives that ship ubuntu-release-upgrader)
    DRU=$(timeout 120 do-release-upgrade -c 2>>"$LOG_FILE")
    if [[ $? -eq 0 ]]; then
        RELEASE_AVAILABLE=$(printf '%s\n' "$DRU" \
            | sed -nE "s/.*New release '([^']+)'.*/\1/p" | head -1)
        [[ -z "$RELEASE_AVAILABLE" ]] && RELEASE_AVAILABLE="new"
        RELEASE_AUTOMATED=true
        log "OS release upgrade available: -> $RELEASE_AVAILABLE"
    fi
elif [[ "$OS_ID" == "debian" && "$RELEASE_CURRENT" =~ ^[0-9]+$ ]] \
     && command -v curl >/dev/null 2>&1; then
    STABLE_VER=$(curl -fsm 20 "http://deb.debian.org/debian/dists/stable/Release" \
        2>>"$LOG_FILE" | awk '/^Version:/ {print $2; exit}')
    STABLE_MAJOR="${STABLE_VER%%.*}"
    if [[ "$STABLE_MAJOR" =~ ^[0-9]+$ ]] && (( STABLE_MAJOR > RELEASE_CURRENT )); then
        RELEASE_AVAILABLE="$STABLE_MAJOR"
        RELEASE_AUTOMATED=false   # surfaced in the UI as a manual task
        log "OS release available (manual): Debian $RELEASE_CURRENT -> $STABLE_MAJOR"
    fi
fi

TOTAL=$(printf '%s\n' "$PKGS_TSV" | awk 'NF' | wc -l | tr -d ' ')

PKG_JSON=$(printf '%s\n' "$PKGS_TSV" | awk 'NF' | head -n "$MAX_PKGS" \
    | jq -R 'split("\t") | {name: .[0], current: (.[1] // ""),
                            candidate: (.[2] // "")}' | jq -s '.')
[[ -z "$PKG_JSON" ]] && PKG_JSON="[]"

TMP="${OUT_FILE}.tmp"
jq -n \
    --arg checked_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg os "$OS_NAME" \
    --arg pm "$PKG_MANAGER" \
    --arg kr "$KERNEL_RUNNING" \
    --arg kl "$KERNEL_LATEST" \
    --arg err "$ERR" \
    --arg relcur "$RELEASE_CURRENT" \
    --arg relav "$RELEASE_AVAILABLE" \
    --argjson relauto "$RELEASE_AUTOMATED" \
    --argjson total "${TOTAL:-0}" \
    --argjson security "${SECURITY:-0}" \
    --argjson reboot "$REBOOT" \
    --argjson uptime "${UPTIME:-0}" \
    --argjson packages "$PKG_JSON" \
    '{checked_at: $checked_at,
      os: $os,
      pkg_manager: (if $pm == "" then null else $pm end),
      kernel_running: $kr,
      kernel_latest: $kl,
      kernel_pending: ($kl != "" and $kr != "" and $kr != $kl),
      uptime_seconds: $uptime,
      update_count: $total,
      security_count: $security,
      reboot_required: $reboot,
      packages: $packages,
      os_release_current: (if $relcur == "" then null else $relcur end),
      os_release_available: (if $relav == "" then null else $relav end),
      os_release_automated: $relauto,
      error: (if $err == "" then null else $err end)}' \
    > "$TMP" 2>>"$LOG_FILE" && mv -f "$TMP" "$OUT_FILE"

log "done: pm=${PKG_MANAGER:-none} updates=${TOTAL:-0} security=${SECURITY:-0} reboot=$REBOOT err=${ERR:-none}"
exit 0
