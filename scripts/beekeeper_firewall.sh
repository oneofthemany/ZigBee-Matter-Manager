#!/bin/bash
# =============================================================================
# ZMM Beekeeper firewall helper (runs ON THE HOST as root).
# =============================================================================
set -u

DATA_DIR="${ZMM_DATA_DIR:-/opt/.zigbee-matter-manager}"
BK_DIR="${DATA_DIR}/data/beekeeper"
TRIGGER="${BK_DIR}/firewall_action"
STATUS="${BK_DIR}/firewall_status.json"
LOG="${DATA_DIR}/logs/beekeeper_firewall.log"

mkdir -p "$BK_DIR" "${DATA_DIR}/logs" 2>/dev/null || true
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG" 2>/dev/null || true; }

ACTION="check"
if [[ -f "$TRIGGER" ]]; then
    ACTION="$(tr -d '[:space:]' < "$TRIGGER" 2>/dev/null)"
    rm -f "$TRIGGER" 2>/dev/null || true
fi
[[ -z "$ACTION" ]] && ACTION="check"

SUDO=""
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        SUDO="sudo -n"
    fi
fi
runp() { $SUDO "$@"; }

json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

write_status() {   # backend  open(true|false|null)  detail
    local backend="$1" open="$2" detail; detail="$(json_escape "$3")"
    local tmp="${STATUS}.tmp"
    printf '{"backend":"%s","port_53_open":%s,"action":"%s","detail":"%s","updated_at":"%s"}\n' \
        "$backend" "$open" "$ACTION" "$detail" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        > "$tmp" 2>>"$LOG" && mv -f "$tmp" "$STATUS"
}

detect_backend() {
    if command -v firewall-cmd >/dev/null 2>&1 && runp firewall-cmd --state >/dev/null 2>&1; then
        echo firewalld; return; fi
    if command -v ufw >/dev/null 2>&1 && runp ufw status 2>/dev/null | grep -qi 'status: active'; then
        echo ufw; return; fi
    if command -v nft >/dev/null 2>&1 && runp nft list ruleset >/dev/null 2>&1; then
        echo nftables; return; fi
    if command -v iptables >/dev/null 2>&1 && runp iptables -S >/dev/null 2>&1; then
        echo iptables; return; fi
    echo none
}

is_open() {   # returns 0 when 53 appears open for $1
    case "$1" in
        firewalld)
            runp firewall-cmd --list-ports 2>/dev/null | grep -qE '(^| )53/(udp|tcp)' && \
            runp firewall-cmd --list-ports 2>/dev/null | grep -q '53/tcp' && return 0
            runp firewall-cmd --list-services 2>/dev/null | grep -qw dns ;;
        ufw)      runp ufw status 2>/dev/null | grep -qE '(^|[[:space:]])53(/(udp|tcp))?([[:space:]]|$)' ;;
        nftables) runp nft list ruleset 2>/dev/null | grep -qE 'dport (53|\{[^}]*53)' ;;
        iptables) runp iptables -S 2>/dev/null | grep -qE -- '--dport 53 ' ;;
        *) return 1 ;;
    esac
}

open_port() {
    case "$1" in
        firewalld)
            runp firewall-cmd --permanent --add-port=53/udp >>"$LOG" 2>&1
            runp firewall-cmd --permanent --add-port=53/tcp >>"$LOG" 2>&1
            runp firewall-cmd --reload >>"$LOG" 2>&1 ;;
        ufw)
            runp ufw allow 53/udp >>"$LOG" 2>&1
            runp ufw allow 53/tcp >>"$LOG" 2>&1 ;;
        nftables)
            runp nft add rule inet filter input udp dport 53 accept >>"$LOG" 2>&1 || \
                log "nft: could not add udp rule (no inet/filter/input chain?)"
            runp nft add rule inet filter input tcp dport 53 accept >>"$LOG" 2>&1 || \
                log "nft: could not add tcp rule" ;;
        iptables)
            runp iptables -I INPUT -p udp --dport 53 -j ACCEPT >>"$LOG" 2>&1
            runp iptables -I INPUT -p tcp --dport 53 -j ACCEPT >>"$LOG" 2>&1 ;;
    esac
}

BACKEND="$(detect_backend)"
log "action=$ACTION backend=$BACKEND sudo='${SUDO:-root}'"

if [[ "$BACKEND" == "none" ]]; then
    write_status "none" "null" "No supported firewall detected, or no privilege to manage it."
    exit 0
fi

if [[ "$ACTION" == "open" ]]; then
    open_port "$BACKEND"
fi

if is_open "$BACKEND"; then
    write_status "$BACKEND" "true" "Port 53/udp+tcp is open."
    log "result: 53 open ($BACKEND)"
else
    write_status "$BACKEND" "false" "Port 53 is not open — click Open :53 to allow DNS."
    log "result: 53 not open ($BACKEND)"
fi
exit 0
