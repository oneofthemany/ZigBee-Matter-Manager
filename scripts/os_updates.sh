#!/bin/bash
# =============================================================================
# ZMM OS Updates Collector
# =============================================================================
set -u

DATA_DIR="${ZMM_DATA_DIR:-/opt/.zigbee-matter-manager}"
OUT_FILE="${DATA_DIR}/data/os_updates.json"
TRIGGER_DIR="${DATA_DIR}/data/os_updates"
REFRESH_TRIGGER="${TRIGGER_DIR}/refresh"
LOG_FILE="${DATA_DIR}/logs/os_updates.log"
MAX_PKGS=300          # cap the package list in the JSON (counts stay exact)
NET_TIMEOUT=300       # seconds allowed for metadata refresh / list commands
# Overridable so tests can point the script at fixtures.
OSTREE_BOOTED="${ZMM_OSTREE_BOOTED:-/run/ostree-booted}"
OS_RELEASE_FILE="${ZMM_OS_RELEASE:-/etc/os-release}"
# Bodhi's "current" state means released; "pending" covers branched and rawhide,
# which already have mirrors and would otherwise look like an available release.
BODHI_RELEASES_URL="${ZMM_BODHI_URL:-https://bodhi.fedoraproject.org/releases/?exclude_archived=true&rows_per_page=100}"

# Packages whose new version only takes effect from boot. Keep in sync with os_apply.sh.
REBOOT_PKGS_RE='^(kernel(-core|-modules|-modules-core|-modules-extra)?|kernel-rt.*|glibc|linux-firmware.*|microcode_ctl|systemd|systemd-libs|systemd-udev|udev|dbus|dbus-broker|dbus-daemon|dbus-libs|openssl-libs|gnutls|zlib|zlib-ng-compat|ostree|ostree-libs|rpm-ostree|rpm-ostree-libs|selinux-policy|selinux-policy-targeted)$'

reboot_blockers() {   # newline-separated package names -> the ones needing a reboot, one per line
    printf '%s\n' "$1" | grep -E "$REBOOT_PKGS_RE" | sort -u
}

# Keep in sync with os_apply.sh, which recomputes it rather than trusting the JSON.
rebase_target() {   # origin current new -> origin moved to release `new`, or nothing
    local origin="$1" cur="$2" new="$3"
    if [[ "$origin" == *"/${cur}/"* ]]; then
        printf '%s\n' "${origin/\/${cur}\//\/${new}\/}"          # fedora:fedora/44/x86_64/silverblue
    elif [[ "$origin" == *":${cur}" ]]; then
        printf '%s\n' "${origin%:${cur}}:${new}"                  # ...fedora-silverblue:44
    fi
}

mkdir -p "${DATA_DIR}/data" "$TRIGGER_DIR" "${DATA_DIR}/logs" 2>/dev/null || true

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE" 2>/dev/null || true; }

rm -f "$REFRESH_TRIGGER" 2>/dev/null || true

if ! command -v jq >/dev/null 2>&1; then
    log "jq missing — cannot write os_updates.json"
    exit 0
fi

SUDO=""
if [[ "$(id -u)" -ne 0 ]] && command -v sudo >/dev/null 2>&1 \
   && sudo -n true 2>/dev/null; then
    SUDO="sudo -n"
fi

OS_NAME="unknown"
[[ -r "$OS_RELEASE_FILE" ]] && OS_NAME=$(. "$OS_RELEASE_FILE" && echo "${PRETTY_NAME:-unknown}")
KERNEL_RUNNING=$(uname -r 2>/dev/null || echo "")
KERNEL_LATEST=$(ls -1 /lib/modules 2>/dev/null | sort -V | tail -1)
UPTIME=$(awk '{print int($1)}' /proc/uptime 2>/dev/null || echo 0)

PKG_MANAGER=""
ERR=""
SECURITY=0
REBOOT=false
STAGED_VERSION=""     # rpm-ostree: downloaded deployment waiting for a reboot
LIVE_APPLICABLE=""    # rpm-ostree: "true"/"false" for the pending update, "" when none
REBOOT_PKGS=""        # rpm-ostree: packages in the pending update that need a reboot
OSTREE_ORIGIN=""      # rpm-ostree: what the booted deployment follows
PKGS_TSV=""           # name<TAB>current<TAB>candidate

if [[ -f "$OSTREE_BOOTED" ]] && command -v rpm-ostree >/dev/null 2>&1; then
    PKG_MANAGER="rpm-ostree"
    log "checking updates via rpm-ostree"
    # --check refreshes the cached update; status --json says what it is and
    # whether it, or anything else, is already deployed and only needs a reboot.
    timeout "$NET_TIMEOUT" rpm-ostree upgrade --check >/dev/null 2>>"$LOG_FILE"
    RC=$?
    if [[ $RC -ne 0 && $RC -ne 77 ]]; then    # 77 = already up to date
        ERR="rpm-ostree upgrade --check failed (exit $RC)"
        log "$ERR"
    fi
    ST=$(rpm-ostree status --json 2>>"$LOG_FILE")
    if [[ -n "$ST" ]]; then
        BOOTED_VER=$(jq -r '[.deployments[] | select(.booted)][0].version // ""' <<<"$ST")
        # Deployments are newest first; one ahead of the booted one is waiting for a reboot.
        STAGED_VERSION=$(jq -r 'if (.deployments[0].booted | not) then (.deployments[0].version // "new deployment") else "" end' <<<"$ST")
        CACHED_VER=$(jq -r '."cached-update".version // ""' <<<"$ST")
        OSTREE_ORIGIN=$(jq -r '[.deployments[] | select(.booted)][0] | (."container-image-reference" // .origin // "")' <<<"$ST")
        # After apply-live the booted deployment records the commit it now runs.
        LIVE_MATCHES=$(jq -r '([.deployments[] | select(.booted)][0]["live-replaced"] // "") as $l
                              | if $l != "" and $l == .deployments[0].checksum then "yes" else "" end' <<<"$ST")
        [[ -n "$STAGED_VERSION" && -z "$LIVE_MATCHES" ]] && REBOOT=true
        if [[ -n "$CACHED_VER" && "$CACHED_VER" != "$BOOTED_VER" && "$CACHED_VER" != "$STAGED_VERSION" ]]; then
            PKGS_TSV=$(printf "ostree deployment\t%s\t%s\n" "$BOOTED_VER" "$CACHED_VER")
            # Advisory kind 1 is security (libdnf's enum); rpm-ostree lists only those today.
            SECURITY=$(jq '[."cached-update".advisories[]? | select(.[1] == 1)] | length' <<<"$ST")
            CHANGED=$(jq -r '."cached-update"."rpm-diff" // {} | [.upgraded[]?, .downgraded[]?, .removed[]?, .added[]?] | .[][1]' <<<"$ST" 2>/dev/null)
            REBOOT_PKGS=$(reboot_blockers "$CHANGED")
            [[ -z "$REBOOT_PKGS" ]] && LIVE_APPLICABLE=true || LIVE_APPLICABLE=false
        fi
    else
        ERR="${ERR:-rpm-ostree status --json returned nothing}"
        log "$ERR"
    fi
elif command -v dnf >/dev/null 2>&1; then
    PKG_MANAGER="dnf"
    log "checking updates via dnf"
    RAW=$(timeout "$NET_TIMEOUT" $SUDO dnf -q --refresh check-update 2>>"$LOG_FILE")
    RC=$?
    if [[ $RC -ne 0 && $RC -ne 100 ]]; then
        ERR="dnf check-update failed (exit $RC)"
        log "$ERR"
    fi
    PKGS_TSV=$(printf '%s\n' "$RAW" | awk '
        /^Obsoleting/ {exit}
        NF==3 && $1 ~ /\./ {printf "%s\t\t%s\n", $1, $2}')
    SECURITY=$(timeout "$NET_TIMEOUT" $SUDO dnf -q updateinfo list --updates \
        --security 2>/dev/null | grep -c '/' || true)
    if dnf needs-restarting --help >/dev/null 2>&1; then
        $SUDO dnf needs-restarting -r >/dev/null 2>&1 || REBOOT=true
    fi
elif command -v apt-get >/dev/null 2>&1; then
    PKG_MANAGER="apt"
    log "checking updates via apt"
    if [[ -n "$SUDO" || "$(id -u)" -eq 0 ]]; then
        timeout "$NET_TIMEOUT" $SUDO apt-get update -qq 2>>"$LOG_FILE" \
            || { ERR="apt-get update failed (using cached metadata)"; log "$ERR"; }
    else
        ERR="no root/sudo — package list may be stale (apt-get update skipped)"
        log "$ERR"
    fi
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
OS_ID=""
RELEASE_CURRENT=""
RELEASE_AVAILABLE=""
RELEASE_AUTOMATED=false
if [[ -r "$OS_RELEASE_FILE" ]]; then
    OS_ID=$(. "$OS_RELEASE_FILE" && echo "${ID:-}")
    RELEASE_CURRENT=$(. "$OS_RELEASE_FILE" && echo "${VERSION_ID:-}")
fi
if [[ "$OS_ID" == "fedora" && "$RELEASE_CURRENT" =~ ^[0-9]+$ ]] \
   && command -v curl >/dev/null 2>&1; then
    RELEASE_LATEST=$(curl -fsm 20 "$BODHI_RELEASES_URL" 2>>"$LOG_FILE" | jq -r '
        [.releases[]? | select(.id_prefix == "FEDORA" and .state == "current"
                               and (.name | test("^F[0-9]+$"))) | .version | tonumber]
        | max // empty' 2>>"$LOG_FILE")
    if [[ "$RELEASE_LATEST" =~ ^[0-9]+$ ]] && (( RELEASE_LATEST > RELEASE_CURRENT )); then
        # Fedora supports upgrading at most two releases in one step.
        (( RELEASE_LATEST > RELEASE_CURRENT + 2 )) && RELEASE_LATEST=$((RELEASE_CURRENT + 2))
        RELEASE_AVAILABLE="$RELEASE_LATEST"
        if [[ "$PKG_MANAGER" != "rpm-ostree" ]]; then
            RELEASE_AUTOMATED=true
        elif [[ -n "$(rebase_target "$OSTREE_ORIGIN" "$RELEASE_CURRENT" "$RELEASE_LATEST")" ]]; then
            RELEASE_AUTOMATED=true
        else
            log "Fedora $RELEASE_LATEST released, but no rebase target derivable from '$OSTREE_ORIGIN' (manual)"
        fi
        log "OS release upgrade available: Fedora $RELEASE_CURRENT -> $RELEASE_LATEST (automated=$RELEASE_AUTOMATED)"
    fi
elif [[ "$PKG_MANAGER" == "apt" ]] && command -v do-release-upgrade >/dev/null 2>&1; then
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
    --arg staged "$STAGED_VERSION" \
    --arg live "$LIVE_APPLICABLE" \
    --arg rebootpkgs "$REBOOT_PKGS" \
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
      staged_version: (if $staged == "" then null else $staged end),
      live_applicable: (if $live == "" then null else ($live == "true") end),
      reboot_packages: ($rebootpkgs | split("\n") | map(select(. != ""))),
      packages: $packages,
      os_release_current: (if $relcur == "" then null else $relcur end),
      os_release_available: (if $relav == "" then null else $relav end),
      os_release_automated: $relauto,
      error: (if $err == "" then null else $err end)}' \
    > "$TMP" 2>>"$LOG_FILE" && mv -f "$TMP" "$OUT_FILE"

log "done: pm=${PKG_MANAGER:-none} updates=${TOTAL:-0} security=${SECURITY:-0} reboot=$REBOOT err=${ERR:-none}"
exit 0
