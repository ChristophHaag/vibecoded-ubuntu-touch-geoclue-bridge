#!/bin/bash
# Deploy GeoClue2 bridge to Ubuntu Touch device
# Usage: ./deploy.sh [install|uninstall|status|test]
#
# Requires: SSH access to phablet@192.168.1.120 (no password)
#           sudo access on device for D-Bus policy and systemd
#
# The install step will prompt for the device's sudo password.

set -e

DEVICE="phablet@192.168.1.120"
REMOTE_DIR="/home/phablet/geoclue2-bridge"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

ssh_cmd() {
    ssh -o StrictHostKeyChecking=no "$DEVICE" "$@"
}

ssh_sudo_cmd() {
    # Use -t to allocate a terminal so sudo can prompt for password
    ssh -t -o StrictHostKeyChecking=no "$DEVICE" "$@"
}

install() {
    echo "=== Deploying GeoClue2 Bridge ==="
    echo ""
    echo "This requires sudo on the device (will prompt for password)."
    echo ""

    # Copy files to device
    echo "[1/5] Copying bridge files..."
    ssh_cmd "mkdir -p $REMOTE_DIR"
    scp -o StrictHostKeyChecking=no \
        "$SCRIPT_DIR/geoclue2-bridge.py" \
        "$SCRIPT_DIR/org.freedesktop.GeoClue2.conf" \
        "$SCRIPT_DIR/geoclue2-bridge.service" \
        "$SCRIPT_DIR/location-whitelist.txt" \
        "$SCRIPT_DIR/add-location-policy.sh" \
        "$SCRIPT_DIR/patch-apparmor-geoclue2.sh" \
        "$DEVICE:$REMOTE_DIR/"

    # Install D-Bus policy (requires remounting root rw)
    echo "[2/5] Installing D-Bus policy (needs sudo)..."
    ssh_sudo_cmd "sudo mount -o remount,rw / && \
             sudo cp $REMOTE_DIR/org.freedesktop.GeoClue2.conf /etc/dbus-1/system.d/ && \
             sudo mount -o remount,ro / && \
             echo 'D-Bus policy installed'"

    # Reload D-Bus to pick up new policy
    echo "[3/5] Reloading D-Bus..."
    ssh_sudo_cmd "sudo systemctl reload dbus 2>/dev/null || sudo kill -HUP 1"

    # Install systemd service (writable /etc/systemd/system/)
    echo "[4/5] Installing systemd service..."
    ssh_sudo_cmd "sudo cp $REMOTE_DIR/geoclue2-bridge.service /etc/systemd/system/ && \
             sudo systemctl daemon-reload"

    # Start service
    echo "[5/5] Starting service..."
    ssh_sudo_cmd "sudo systemctl enable geoclue2-bridge.service && \
             sudo systemctl restart geoclue2-bridge.service"

    sleep 2
    echo ""
    echo "=== Deployment complete ==="
    ssh_cmd "systemctl is-active geoclue2-bridge.service"
    echo ""
    echo "To use GPS in Firefox, set in about:config:"
    echo "  geo.provider.use_geoclue = true"
    echo ""
    echo "Check status: ./deploy.sh status"
}

uninstall() {
    echo "=== Removing GeoClue2 Bridge ==="
    ssh_sudo_cmd "sudo systemctl stop geoclue2-bridge.service 2>/dev/null || true"
    ssh_sudo_cmd "sudo systemctl disable geoclue2-bridge.service 2>/dev/null || true"
    ssh_sudo_cmd "sudo rm -f /etc/systemd/system/geoclue2-bridge.service"
    ssh_sudo_cmd "sudo mount -o remount,rw / && \
             sudo rm -f /etc/dbus-1/system.d/org.freedesktop.GeoClue2.conf && \
             sudo mount -o remount,ro /"
    ssh_sudo_cmd "sudo systemctl daemon-reload"
    ssh_sudo_cmd "sudo systemctl reload dbus 2>/dev/null || sudo kill -HUP 1"
    echo "=== Removed (bridge files still at $REMOTE_DIR) ==="
}

status() {
    echo "=== GeoClue2 Bridge Status ==="
    ssh_cmd "systemctl status geoclue2-bridge.service --no-pager 2>&1" || true
    echo ""
    echo "--- Recent logs ---"
    ssh_cmd "journalctl -u geoclue2-bridge -n 20 --no-pager 2>&1" || true
}

test_bridge() {
    echo "=== Testing GeoClue2 Bridge ==="
    echo "Checking if service is running..."
    ssh_cmd "systemctl is-active geoclue2-bridge.service" || {
        echo "Service not running. Start with: ./deploy.sh install"
        exit 1
    }
    echo ""
    echo "Attempting to get location via GeoClue2 API..."
    ssh_cmd "python3 -c \"
import dbus
bus = dbus.SystemBus()
manager = dbus.Interface(
    bus.get_object('org.freedesktop.GeoClue2', '/org/freedesktop/GeoClue2/Manager'),
    'org.freedesktop.GeoClue2.Manager')
client_path = manager.GetClient()
print(f'Client: {client_path}')
client = dbus.Interface(
    bus.get_object('org.freedesktop.GeoClue2', client_path),
    'org.freedesktop.GeoClue2.Client')
props = dbus.Interface(
    bus.get_object('org.freedesktop.GeoClue2', client_path),
    'org.freedesktop.DBus.Properties')
props.Set('org.freedesktop.GeoClue2.Client', 'DesktopId', 'test')
props.Set('org.freedesktop.GeoClue2.Client', 'RequestedAccuracyLevel', dbus.UInt32(8))
client.Start()
print('Client started. GPS fix may take time if device is outdoors.')
print('Bridge is working correctly!')
\"" 2>&1
}

case "${1:-install}" in
    install)   install ;;
    uninstall) uninstall ;;
    status)    status ;;
    test)      test_bridge ;;
    *)
        echo "Usage: $0 [install|uninstall|status|test]"
        exit 1
        ;;
esac
