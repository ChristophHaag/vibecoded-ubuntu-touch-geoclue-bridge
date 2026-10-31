#!/bin/bash
# Deploy the GeoClue2 bridge to the Ubuntu Touch device.
#
#   ./deploy.sh [install]  copy files, then run 'sudo gps.sh' on the device
#                          (one sudo password prompt). Safe to re-run.
#   ./deploy.sh copy       copy files only (no sudo); run gps.sh later
#   ./deploy.sh status     setup status and recent bridge log
#   ./deploy.sh test       wait up to 40 s for a real fix via test-client.py
#   ./deploy.sh uninstall  remove service and D-Bus policy (files are kept)
#
# Also copies ../gps-hidl-workaround/gps-hidl-workaround.sh if that sibling
# directory exists; gps.sh installs it.

set -e

DEVICE="${DEVICE:-phablet@192.168.1.120}"
REMOTE_DIR="/home/phablet/geoclue2-bridge"
WORKAROUND_REMOTE_DIR="/home/phablet/gps-hidl-workaround"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKAROUND="$SCRIPT_DIR/../gps-hidl-workaround/gps-hidl-workaround.sh"

ssh_cmd() {
    ssh -o StrictHostKeyChecking=no "$DEVICE" "$@"
}

ssh_sudo_cmd() {
    # -t so sudo can prompt for the password
    ssh -t -o StrictHostKeyChecking=no "$DEVICE" "$@"
}

copy() {
    echo "=== Copying files to $DEVICE ==="
    ssh_cmd "mkdir -p $REMOTE_DIR"
    scp -q -o StrictHostKeyChecking=no \
        "$SCRIPT_DIR/geoclue2-bridge.py" \
        "$SCRIPT_DIR/org.freedesktop.GeoClue2.conf" \
        "$SCRIPT_DIR/geoclue2-bridge.service" \
        "$SCRIPT_DIR/location-whitelist.txt" \
        "$SCRIPT_DIR/add-location-policy.sh" \
        "$SCRIPT_DIR/patch-apparmor-geoclue2.sh" \
        "$SCRIPT_DIR/gps.sh" \
        "$SCRIPT_DIR/test-client.py" \
        "$SCRIPT_DIR/test-bridge-mock.py" \
        "$SCRIPT_DIR/gps-diag.sh" \
        "$DEVICE:$REMOTE_DIR/"
    ssh_cmd "chmod +x $REMOTE_DIR/*.sh $REMOTE_DIR/*.py"
    if [ -f "$WORKAROUND" ]; then
        ssh_cmd "mkdir -p $WORKAROUND_REMOTE_DIR"
        scp -q -o StrictHostKeyChecking=no "$WORKAROUND" "$DEVICE:$WORKAROUND_REMOTE_DIR/"
        ssh_cmd "chmod +x $WORKAROUND_REMOTE_DIR/gps-hidl-workaround.sh"
    fi
    echo "Copied."
}

install() {
    copy
    echo ""
    ssh_sudo_cmd "sudo $REMOTE_DIR/gps.sh"
}

uninstall() {
    echo "=== Removing GeoClue2 Bridge ==="
    ssh_sudo_cmd "sudo sh -c '
        systemctl disable --now geoclue2-bridge.service 2>/dev/null
        rm -f /etc/systemd/system/geoclue2-bridge.service
        systemctl daemon-reload
        mount -o remount,rw / &&
            rm -f /etc/dbus-1/system.d/org.freedesktop.GeoClue2.conf &&
            mount -o remount,ro /
        systemctl reload dbus'"
    echo "=== Removed (files still at $REMOTE_DIR; the GPS workaround is"
    echo "    separate: sudo $WORKAROUND_REMOTE_DIR/gps-hidl-workaround.sh remove) ==="
}

status() {
    ssh_cmd "$REMOTE_DIR/gps.sh --status" || true
    echo ""
    echo "--- Recent bridge log ---"
    ssh_cmd "journalctl -u geoclue2-bridge -n 20 --no-pager 2>&1" || true
}

test_bridge() {
    ssh_cmd "python3 $REMOTE_DIR/test-client.py 40 2"
}

case "${1:-install}" in
    install)   install ;;
    copy)      copy ;;
    uninstall) uninstall ;;
    status)    status ;;
    test)      test_bridge ;;
    *)
        echo "Usage: $0 [install|copy|status|test|uninstall]"
        exit 1
        ;;
esac
