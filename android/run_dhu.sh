#!/usr/bin/env bash
# Launch the Android Auto Desktop Head Unit against the connected phone.
#
# The DHU is the ONLY way to see this app's car screen during development:
# Android Auto refuses to load sideloaded template apps (CarAppService) on a
# real head unit no matter what "Unknown sources" is set to — that toggle
# covers media/messaging/parked apps only. See BUILDING.md.
#
# Before this works you must, ON THE PHONE, one time only:
#   1. Open Android Auto settings (Settings > Apps > Android Auto >
#      Additional settings in the app).
#   2. Scroll to the bottom and tap "Version" 10 times, accept the dialog.
#      This unlocks "Developer settings".
#   3. In the top-right overflow menu, tap "Start head unit server".
# Step 3 must be repeated after each reboot; the server does not persist.
# MUST be run from a real terminal. The DHU reads commands from stdin as well as
# drawing its window, so with stdin closed or piped it reads EOF the moment it
# connects and exits 0 — banner printed, no window, no error. That clean exit is
# indistinguishable from success in a log, so check for the window, not the code.
set -euo pipefail

# Must be DHU 2.1 or newer (check source.properties). The 2.0 build that
# sdkmanager installs from the stable channel is from March 2022 and cannot
# complete PREFLIGHT against current Android Auto: TLS negotiates, then both
# ends report a failed read and the session dies. 2.1 is on the beta channel:
#   dl.google.com/android/repository/desktop-head-unit-linux-x64_r02.1.zip
DHU_HOME=/var/home/sean/Android/Sdk/extras/google/auto

if ! adb get-state >/dev/null 2>&1; then
    echo "No device: plug the phone in and check 'adb devices'." >&2
    exit 1
fi

adb forward tcp:5277 tcp:5277 >/dev/null

# 0x149D is 5277. Nothing listening means the head unit server was never
# started — the DHU would connect to the adb forward, print its banner and
# exit silently, which looks exactly like a broken install.
if ! adb shell "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null" \
     | awk '$4=="0A" {print $2}' | grep -qi ":149D"; then
    echo "Head unit server is not running on the phone." >&2
    echo "Android Auto > Developer settings > 'Start head unit server', then rerun." >&2
    exit 1
fi

# Fedora Silverblue ships no libc++, and the base image is immutable, so the
# two libs the DHU binary needs were extracted from the Fedora RPMs into lib/
# rather than layered onto the host with rpm-ostree. Scoped to this one exec
# on purpose: exporting it earlier makes adb itself pick up the bundled
# libc++ and fail every call above with a bare "no device".
exec env LD_LIBRARY_PATH="$DHU_HOME/lib:$DHU_HOME" "$DHU_HOME/desktop-head-unit" "$@"
