#!/bin/bash
# =============================================================================
# ZMM Coral M.2 driver helper (runs ON THE HOST as root).
#   coral_driver.sh [install|remove|check|load|prebuild]   (or the trigger file)
# =============================================================================
# An M.2 / Mini PCIe Coral is unusable until the host's gasket and apex kernel
# modules are loaded; no mainline kernel ships them and no container can supply
# them. See docs/vision.md §Coral driver.
#
# Modules are built per kernel into a writable state dir and insmod'ed at boot,
# so this works on image-based hosts (rpm-ostree) where DKMS cannot. Built on
# the host when it has headers and a compiler, otherwise in a throwaway
# container. A distro-packaged apex module, where one exists, is used as is.
#
# Boot backends, detected: systemd unit, OpenRC init script, or neither.
set -u

DATA_DIR="${ZMM_DATA_DIR:-/opt/.zigbee-matter-manager}"
CORAL_DIR="${DATA_DIR}/data/coral"
TRIGGER="${CORAL_DIR}/driver_action"
STATUS="${CORAL_DIR}/driver_status.json"
THERMAL="${CORAL_DIR}/thermal"
LOG="${DATA_DIR}/logs/coral_driver.log"
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

# Kernel code that runs as root: one reviewed commit, checked by hash.
SRC_COMMIT="1d519f62d886599fafc40204e72ebc7e14752bd2"
SRC_URL="${ZMM_CORAL_SRC_URL:-https://github.com/KyleGospo/gasket-dkms/archive/${SRC_COMMIT}.tar.gz}"
SRC_SHA256="${ZMM_CORAL_SRC_SHA256:-a5dd26bfa9d3c21c604522ccd79b9e43cb9b311eabcfcef0ba8e006223a5cabc}"

# Overridable so tests can point at temp dirs.
STATE="${ZMM_CORAL_STATE:-/var/lib/zmm-coral}"
SYS="${ZMM_SYSFS_ROOT:-/sys}"
DEV="${ZMM_DEV_ROOT:-/dev}"
MODULES_ROOT="${ZMM_MODULES_ROOT:-/lib/modules}"
OSTREE_ROOT="${ZMM_OSTREE_ROOT:-/ostree}"
OS_RELEASE="${ZMM_OS_RELEASE:-/etc/os-release}"
UDEV_DIR="${ZMM_UDEV_DIR:-/etc/udev/rules.d}"
SYSTEMD_DIR="${ZMM_SYSTEMD_DIR:-/etc/systemd/system}"
SYSTEMD_RUN="${ZMM_SYSTEMD_RUN:-/run/systemd/system}"
INITD_DIR="${ZMM_INITD_DIR:-/etc/init.d}"
OPENRC_RUN="${ZMM_OPENRC_RUN:-/run/openrc}"
KVER="${ZMM_KVER:-$(uname -r)}"

SERVICE="zmm-coral-driver"
SD_UNIT="${SYSTEMD_DIR}/${SERVICE}.service"
RC_SCRIPT="${INITD_DIR}/${SERVICE}"
UDEV_RULE="${UDEV_DIR}/65-zmm-apex.rules"
MOD_DIR="${STATE}/modules"
SRC_DIR="${STATE}/src/${SRC_COMMIT}"

mkdir -p "$CORAL_DIR" "${DATA_DIR}/logs" 2>/dev/null || true
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG" 2>/dev/null || true; }
have() { command -v "$1" >/dev/null 2>&1; }

ACTION="${1:-}"
if [[ -z "$ACTION" ]]; then
    ACTION="check"
    if [[ -f "$TRIGGER" ]]; then
        ACTION="$(tr -d '[:space:]' < "$TRIGGER" 2>/dev/null)"
        rm -f "$TRIGGER" 2>/dev/null || true
    fi
fi
case "$ACTION" in install|remove|check|load|prebuild) ;; *) log "ignoring unknown action '$ACTION'"; ACTION="check" ;; esac

SUDO=""
if [[ ${EUID:-$(id -u)} -ne 0 ]] && have sudo && sudo -n true 2>/dev/null; then
    SUDO="sudo -n"
fi
runp() { $SUDO "$@"; }

# A build takes minutes; a second run must not start one alongside it.
if have flock && runp mkdir -p "$STATE" 2>/dev/null; then
    exec 9>"${CORAL_DIR}/.driver.lock"
    flock -w 1800 9 || { log "$ACTION: another run holds the lock"; exit 0; }
fi

BACKEND="none"
if [[ -d "$SYSTEMD_RUN" ]] && have systemctl; then
    BACKEND="systemd"
elif [[ -d "$OPENRC_RUN" ]] && have rc-update; then
    BACKEND="openrc"
fi

RUNTIME=""
for rt in ${ZMM_RUNTIMES:-podman docker}; do
    if have "$rt"; then RUNTIME="$(command -v "$rt")"; break; fi
done

json_str() { printf '"%s"' "$(printf '%s' "$1" | tr '\n' ' ' | sed 's/\\/\\\\/g; s/"/\\"/g')"; }
json_list() { local out="" x; for x in "$@"; do out+="${out:+,}$(json_str "$x")"; done; printf '[%s]' "$out"; }

card_present() {
    local d
    for d in "$SYS"/bus/pci/devices/*; do
        [[ "$(cat "$d/vendor" 2>/dev/null)" == "0x1ac1" && "$(cat "$d/device" 2>/dev/null)" == "0x089a" ]] && return 0
    done
    return 1
}
loaded() { [[ -d "$SYS/module/apex" ]]; }
node() { local n; for n in "$DEV"/apex_*; do [[ -e "$n" ]] && { basename "$n"; return; }; done; }
built_kernels() { local d; for d in "$MOD_DIR"/*; do [[ -f "$d/apex.ko" ]] && basename "$d"; done; }
# Unsigned modules are refused under Secure Boot; nothing here signs them.
secure_boot() { have mokutil && mokutil --sb-state 2>/dev/null | grep -qi 'enabled'; }

service_state() {   # -> "installed enabled"
    local i=false e=false
    case "$BACKEND" in
        systemd) [[ -f "$SD_UNIT" ]] && i=true
                 runp systemctl is-enabled --quiet "${SERVICE}.service" 2>/dev/null && e=true ;;
        openrc)  [[ -f "$RC_SCRIPT" ]] && i=true
                 runp rc-update show default 2>/dev/null | grep -qE "^[[:space:]]*${SERVICE}[[:space:]]" && e=true ;;
    esac
    echo "$i $e"
}

SOURCE=""
write_status() {   # state detail
    local tmp="${STATUS}.tmp" st l=false sb=false src="$SOURCE"
    st=$(service_state)
    loaded && l=true
    secure_boot && sb=true
    # A guess for the report only: SOURCE itself decides what install does.
    if [[ -z "$src" ]] && $l; then
        [[ -f "${MOD_DIR}/${KVER}/apex.ko" ]] && src="built" || src="distro"
    fi
    # shellcheck disable=SC2046
    printf '{"state":%s,"backend":%s,"kernel":%s,"present":%s,"installed":%s,"enabled":%s,"loaded":%s,"node":%s,"source":%s,"built_for":%s,"secure_boot":%s,"action":%s,"detail":%s,"updated_at":%s}\n' \
        "$(json_str "$1")" "$(json_str "$BACKEND")" "$(json_str "$KVER")" \
        "$(card_present && echo true || echo false)" "${st% *}" "${st#* }" "$l" "$(json_str "$(node)")" \
        "$(json_str "$src")" "$(json_list $(built_kernels))" "$sb" \
        "$(json_str "$ACTION")" "$(json_str "$2")" "$(json_str "$(date -u +%Y-%m-%dT%H:%M:%SZ)")" \
        > "$tmp" 2>>"$LOG" && mv -f "$tmp" "$STATUS"
    log "$ACTION ($BACKEND, $1): $2"
}
finish() { write_status "$1" "$2"; exit 0; }

# ── Source ───────────────────────────────────────────────────────────────────
fetch_source() {
    [[ -f "${SRC_DIR}/src/apex_driver.c" ]] && return 0
    local tmp sum
    tmp="$(mktemp -d)" || return 1
    if ! curl -fsSL --max-time 120 -o "$tmp/src.tgz" "$SRC_URL" 2>>"$LOG"; then
        rm -rf "$tmp"; ERR="couldn't download the driver source (no network?)"; return 1
    fi
    sum="$(sha256sum "$tmp/src.tgz" | cut -d' ' -f1)"
    if [[ "$sum" != "$SRC_SHA256" ]]; then
        rm -rf "$tmp"; ERR="driver source failed its checksum — not building it"; return 1
    fi
    runp mkdir -p "$SRC_DIR" && runp tar -xzf "$tmp/src.tgz" -C "$SRC_DIR" --strip-components=1 2>>"$LOG"
    rm -rf "$tmp"
    [[ -f "${SRC_DIR}/src/apex_driver.c" ]] || { ERR="driver source archive has an unexpected layout"; return 1; }
}

# ── Build ────────────────────────────────────────────────────────────────────
# Runs inside the builder container: headers for exactly $1, then kbuild.
CONTAINER_BUILD='
set -e
KVER="$1"
if command -v dnf >/dev/null 2>&1; then
    ARCH="${KVER##*.}"; NVR="${KVER%.*}"
    dnf -q -y install gcc make kmod elfutils-libelf-devel
    # The repos carry only the newest kernel; Koji keeps every build.
    dnf -q -y install "kernel-devel-${KVER}" || dnf -q -y install \
        "https://kojipkgs.fedoraproject.org/packages/kernel/${NVR%%-*}/${NVR#*-}/${ARCH}/kernel-devel-${KVER}.rpm"
    KDIR="/usr/src/kernels/${KVER}"
else
    export DEBIAN_FRONTEND=noninteractive
    apt-get -qq update
    apt-get -qq -y install build-essential kmod "linux-headers-${KVER}"
    KDIR="/lib/modules/${KVER}/build"
fi
make -C "$KDIR" M=/build modules
'

builder_image() {
    local id ver code
    id="$(. "$OS_RELEASE" 2>/dev/null; echo "${ID:-}")"
    ver="$(. "$OS_RELEASE" 2>/dev/null; echo "${VERSION_ID:-}")"
    code="$(. "$OS_RELEASE" 2>/dev/null; echo "${VERSION_CODENAME:-}")"
    case "$id" in
        fedora) echo "registry.fedoraproject.org/fedora:${ver}" ;;
        ubuntu) echo "docker.io/library/ubuntu:${ver}" ;;
        debian) [[ -n "$code" ]] && echo "docker.io/library/debian:${code}" ;;
    esac
}

build_for() {   # kver
    local kver="$1" out="${MOD_DIR}/$1" work="${STATE}/build/$1" kdir="${MODULES_ROOT}/$1/build" img
    [[ -f "$out/gasket.ko" && -f "$out/apex.ko" ]] && return 0
    fetch_source || return 1
    runp rm -rf "$work"; runp mkdir -p "$work" && runp cp "${SRC_DIR}"/src/* "$work/" || { ERR="can't write to ${STATE}"; return 1; }
    log "building for $kver"
    if [[ -f "$kdir/Makefile" ]] && have make && { have gcc || have cc; }; then
        runp make -C "$kdir" M="$work" modules >>"$LOG" 2>&1
    elif [[ -n "$RUNTIME" ]] && img="$(builder_image)" && [[ -n "$img" ]]; then
        # label=disable: the build dir is under /var/lib, not a container-labelled path.
        runp "$RUNTIME" run --rm --security-opt label=disable -v "$work:/build" "$img" \
            bash -c "$CONTAINER_BUILD" build "$kver" >>"$LOG" 2>&1
    else
        ERR="no way to build for $kver: install its kernel headers, make and gcc on the host, then retry"
        runp rm -rf "$work"; return 1
    fi
    if [[ ! -f "$work/gasket.ko" || ! -f "$work/apex.ko" ]]; then
        ERR="build failed for $kver — see coral_driver.log"; runp rm -rf "$work"; return 1
    fi
    if have modinfo && [[ "$(modinfo -F vermagic "$work/apex.ko" 2>/dev/null | cut -d' ' -f1)" != "$kver" ]]; then
        ERR="built module doesn't match kernel $kver"; runp rm -rf "$work"; return 1
    fi
    runp mkdir -p "$out" && runp cp "$work/gasket.ko" "$work/apex.ko" "$out/"
    have chcon && runp chcon -t modules_object_t "$out"/*.ko 2>/dev/null
    runp rm -rf "$work"
    [[ -f "$out/apex.ko" ]]
}

# The running kernel plus every installed or staged one, so the card still
# works on the first boot after a kernel update.
known_kernels() {
    local d
    { echo "$KVER"
      for d in "$MODULES_ROOT"/* "$OSTREE_ROOT"/deploy/*/deploy/*/usr/lib/modules/*; do
          [[ -d "$d/kernel" ]] && basename "$d"
      done; } | sort -u
}

prebuild() {   # -> names of kernels it couldn't build for, in PRE_FAILED
    local k keep
    PRE_FAILED=""
    keep="$(known_kernels)"
    for k in $keep; do
        [[ "$k" == "$KVER" ]] && continue
        build_for "$k" || PRE_FAILED+="${PRE_FAILED:+, }$k"
    done
    for k in $(built_kernels); do
        grep -qxF "$k" <<<"$keep" || runp rm -rf "${MOD_DIR:?}/$k"
    done
}

# ── Load ─────────────────────────────────────────────────────────────────────
# The driver steps the chip's clock down at each trip point (and the chip cuts
# out at 100 °C on its own); one number moves all three, 5 °C apart.
apply_thermal() {
    local d="$SYS/class/apex/apex_0" t i
    [[ -f "$THERMAL" && -d "$d" ]] || return 0
    t="$(tr -dc '0-9' < "$THERMAL")"
    [[ -n "$t" ]] && (( t >= 50 && t <= 85 )) || return 0
    # The driver rejects a write that would put the points out of order, so
    # go up then back down: one of the two passes is always legal.
    for i in 0 1 2 1 0; do
        echo $(( (t + 5 * i) * 1000 )) | runp tee "$d/trip_point${i}_temp" >/dev/null 2>&1
    done
}

load_modules() {
    local m="${MOD_DIR}/${KVER}" i
    if ! loaded; then
        if have modinfo && modinfo -k "$KVER" apex >/dev/null 2>&1; then
            SOURCE="distro"
            runp modprobe apex 2>>"$LOG" || { ERR="modprobe apex failed — see coral_driver.log"; return 1; }
        else
            SOURCE="built"
            if secure_boot; then
                ERR="Secure Boot is on, and the kernel refuses unsigned modules. Turn it off in the firmware, or sign the modules with an enrolled key."
                return 1
            fi
            build_for "$KVER" || return 1
            [[ -d "$SYS/module/gasket" ]] || runp insmod "$m/gasket.ko" 2>>"$LOG" \
                || { ERR="the kernel refused gasket.ko — see coral_driver.log"; return 1; }
            runp insmod "$m/apex.ko" 2>>"$LOG" || { ERR="the kernel refused apex.ko — see coral_driver.log"; return 1; }
        fi
        for i in 1 2 3 4 5 6 7 8 9 10; do [[ -n "$(node)" ]] && break; sleep "${ZMM_CORAL_WAIT:-0.5}"; done
    fi
    apply_thermal
    [[ -n "$(node)" ]] || { ERR="driver loaded but no /dev/apex_0 appeared"; return 1; }
}

# ── Boot service ─────────────────────────────────────────────────────────────
write_if_changed() {   # path mode content
    if [[ -f "$1" ]] && [[ "$(cat "$1")" == "$3" ]]; then return 1; fi
    printf '%s\n' "$3" | runp tee "$1" >/dev/null && runp chmod "$2" "$1"
}

sd_unit_text() {
    cat <<UNIT
[Unit]
Description=ZMM Coral Edge TPU driver (gasket + apex)
# Written by coral_driver.sh install, removed by coral_driver.sh remove.
# After the network: a new kernel with no prebuilt modules needs a build.
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=${SELF} load
Environment=ZMM_DATA_DIR=${DATA_DIR}
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
TimeoutStartSec=1800

[Install]
WantedBy=multi-user.target
UNIT
}

rc_script_text() {
    cat <<SCRIPT
#!/sbin/openrc-run
# Written by coral_driver.sh install, removed by coral_driver.sh remove.
description="ZMM Coral Edge TPU driver (gasket + apex)"

depend() {
    need net
}

start() {
    ebegin "Loading the Coral driver"
    ZMM_DATA_DIR="${DATA_DIR}" "${SELF}" load
    eend 0
}
SCRIPT
}

install_udev() {
    local rule='SUBSYSTEM=="apex", MODE="0660"'
    # A group lets a non-root container user open the device; root needs neither.
    if ! getent group apex >/dev/null 2>&1; then
        if have groupadd; then runp groupadd -r apex 2>>"$LOG"
        elif have addgroup; then runp addgroup -S apex 2>>"$LOG"; fi
    fi
    getent group apex >/dev/null 2>&1 && rule+=', GROUP="apex"'
    [[ -d "$UDEV_DIR" ]] || return 0
    if write_if_changed "$UDEV_RULE" 644 "$rule" && have udevadm; then
        runp udevadm control --reload 2>>"$LOG"
        runp udevadm trigger --subsystem-match=apex 2>>"$LOG"
    fi
}

install_service() {
    case "$BACKEND" in
        systemd)
            write_if_changed "$SD_UNIT" 644 "$(sd_unit_text)"
            runp systemctl daemon-reload
            runp systemctl enable "${SERVICE}.service" >/dev/null 2>&1 ;;
        openrc)
            write_if_changed "$RC_SCRIPT" 755 "$(rc_script_text)"
            runp rc-update add "$SERVICE" default >/dev/null 2>&1 ;;
    esac
}

remove_service() {
    case "$BACKEND" in
        systemd)
            [[ -f "$SD_UNIT" ]] || return 0
            runp systemctl disable "${SERVICE}.service" >/dev/null 2>&1
            runp rm -f "$SD_UNIT"; runp systemctl daemon-reload ;;
        openrc)
            [[ -f "$RC_SCRIPT" ]] || return 0
            runp rc-update del "$SERVICE" default >/dev/null 2>&1
            runp rm -f "$RC_SCRIPT" ;;
    esac
}

ERR=""
case "$ACTION" in
    install)
        card_present || finish "failed" "no Coral M.2 or Mini PCIe card on the PCI bus (a USB Coral needs no driver)"
        write_status "running" "building and loading the driver — a first build takes a few minutes"
        load_modules || finish "failed" "$ERR"
        # A driver loaded by hand still needs a copy on disk for the next boot.
        if [[ "$SOURCE" != "distro" ]] && ! { have modinfo && modinfo -k "$KVER" apex >/dev/null 2>&1; }; then
            build_for "$KVER" || finish "failed" "$ERR"
            SOURCE="built"
        fi
        install_udev
        install_service
        prebuild
        case "$BACKEND" in
            none) DETAIL="loaded, but this host has no systemd or OpenRC: run '${SELF} load' at boot yourself" ;;
            *)    DETAIL="loaded, and loads at every boot" ;;
        esac
        [[ -n "$PRE_FAILED" ]] && DETAIL+=" (couldn't prebuild for $PRE_FAILED — it will be built on first boot into it)"
        finish "done" "$DETAIL"
        ;;
    load)
        card_present || finish "done" "no Coral card on the PCI bus — nothing to load"
        load_modules || finish "failed" "$ERR"
        finish "done" "loaded"
        ;;
    prebuild)
        # Called after a host update; a no-op unless the driver is installed.
        [[ "$(service_state)" == true* || -n "$(built_kernels)" ]] || exit 0
        prebuild
        DETAIL="modules ready for: $(built_kernels | paste -sd' ' -)"
        [[ -n "$PRE_FAILED" ]] && DETAIL="no build for $PRE_FAILED; $DETAIL"
        finish "done" "$DETAIL"
        ;;
    remove)
        remove_service
        [[ -f "$UDEV_RULE" ]] && runp rm -f "$UDEV_RULE"
        DETAIL="driver removed"
        if loaded; then
            runp rmmod apex 2>>"$LOG" && { runp rmmod gasket 2>>"$LOG" || true; } \
                || DETAIL="boot service removed; the driver is in use and stays loaded until a reboot"
        fi
        [[ -d "$STATE" ]] && runp rm -rf "${STATE:?}/modules" "${STATE:?}/src" "${STATE:?}/build"
        finish "done" "$DETAIL"
        ;;
    check)
        apply_thermal
        finish "done" "checked"
        ;;
esac
exit 0
